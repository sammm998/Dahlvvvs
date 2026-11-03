"""Etiketter som en annan läsare redan läst, in i motorns textrader.

VS-Kalkyls T5 läser etiketterna med två oberoende läsare (vision och OCR) och läser om där de inte är överens.
Den här modulen låter motorn använda de läsningarna utan att något annat i motorn ändras:

- Motorns egna rader läses som vanligt. Varje extern etikett tar de motorrader den täcker. Raderna ersätts av en
  rad med etikettens *text*, medan motorns *geometri* behålls: läsriktning, texthöjd och tvärsled kommer från
  motorns rader, och deras streck (`provenance`) följer med, så att det som är text aldrig blir rör.
- En etikett som motorn inte läste alls blir en ny rad i etikettens låda.
- Tecknen fördelas jämnt längs raden, ett per tecken, så att text och glyfer alltid stämmer.
- En etikett som läsaren själv flaggat (under `MIN_CONFIDENCE`, eller `needs_review`) används, men raden märks,
  och rör som får sitt namn från den hamnar i REVIEW-nivån (se pipeline). En osäker läsning ger aldrig
  bekräftade meter.

Filen är den som `python -m app.drawings.labels` i VS-Kalkyl skriver: `{sidindex: {"rows": [{"text", "bbox",
"read_confidence", "flags", ...}]}, "_meta": {...}}`, med sidindex 0-baserat och lådor i PDF-punkter med y nedåt
- samma koordinater som motorn.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Any

from ..geometry.core import bbox_union, stable_id
from .model import Glyph, TextRow, project, row_axes

ARTIFACT_SCHEMA = "vvs_engine.external_labels/1"

# En motorrad hör till en etikett när så stor del av den mindre av de två lådorna ligger i den andra.
# Etikettens låda är något större än tecknen (läsaren ritar den med marginal), motorns är tät runt strecken.
MATCH_OVERLAP = 0.5
# Under denna konfidens (läsarens egen skala 0-1) är etiketten osäker: den används, men det den namnger granskas.
MIN_CONFIDENCE = 0.8
# Lodrät etikett utan motorrad: lådan är så här många gånger högre än bred.
VERTICAL_ASPECT = 1.5


@dataclass(frozen=True)
class ExternalLabel:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float
    flags: tuple[str, ...] = ()
    method: str = ""

    @property
    def uncertain(self) -> bool:
        return self.confidence < MIN_CONFIDENCE or "needs_review" in self.flags


@dataclass
class LabelReport:
    """Vad etikettfilen gjorde med ett blad."""

    page: int
    labels: int = 0
    replaced_lines: int = 0          # motorrader vars text byttes
    unchanged: int = 0               # motorn hade redan läst samma text
    changed: int = 0                 # motorn hade läst annat (eller bara en del)
    added: int = 0                   # etiketter motorn inte läste alls
    uncertain: int = 0               # flaggade av läsaren
    examples: list[dict[str, Any]] = field(default_factory=list)
    uncertain_rids: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {"artifact_schema": ARTIFACT_SCHEMA, "page": self.page, "labels": self.labels,
                "replaced_lines": self.replaced_lines, "unchanged": self.unchanged, "changed": self.changed,
                "added": self.added, "uncertain": self.uncertain, "examples": self.examples[:200]}


class ExternalLabels:
    """Etikettfilen för en handling. `sha256` ingår i körningens frysmanifest."""

    def __init__(self, by_page: dict[int, list[ExternalLabel]], sha256: str, meta: dict[str, Any]):
        self.by_page = by_page
        self.sha256 = sha256
        self.meta = meta

    @classmethod
    def load(cls, path: str) -> ExternalLabels:
        raw = open(path, "rb").read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: etikettfilen ska vara ett objekt per sidindex")
        by_page: dict[int, list[ExternalLabel]] = {}
        for key, sheet in data.items():
            if key.startswith("_"):
                continue
            rows = (sheet or {}).get("rows")
            if not isinstance(rows, list):
                raise ValueError(f"{path}: sida {key} saknar 'rows'")
            labels = []
            for r in rows:
                text = str(r.get("text") or "").strip()
                box = r.get("bbox")
                if not text or not isinstance(box, list) or len(box) != 4:
                    continue
                labels.append(ExternalLabel(text, (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                                            float(r.get("read_confidence", 0.0)), tuple(r.get("flags") or ()),
                                            str(r.get("method") or "")))
            by_page[int(key)] = labels
        meta = data.get("_meta") or {}
        return cls(by_page, hashlib.sha256(raw).hexdigest(), {k: meta.get(k) for k in ("models", "model", "ocr")})

    def without(self, pages) -> ExternalLabels:
        """Samma fil utan de angivna bladen (t.ex. blad som vreds innan de lästes)."""
        return ExternalLabels({p: v for p, v in self.by_page.items() if p not in set(pages)}, self.sha256,
                              {**self.meta, "dropped_pages": sorted(set(pages))})

    def apply(self, lines: list[TextRow], page: int) -> tuple[list[TextRow], LabelReport]:
        return apply_external_labels(lines, self.by_page.get(page, []), page)


def _overlap_of_smaller(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


def _norm(text: str) -> str:
    return " ".join(text.upper().split())


def _span(box, axis) -> tuple[float, float]:
    ps = [project(c, axis) for c in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3]))]
    return min(ps), max(ps)


def _line_for(label: ExternalLabel, mine: list[TextRow], page: int, default_source: str) -> TextRow:
    """En rad med etikettens text i motorns geometri."""
    if mine:
        angle = mine[0].angle
        height = statistics.median(r.height for r in mine)
        source, layer, font = mine[0].source, mine[0].layer, mine[0].font
    else:
        w, h = label.bbox[2] - label.bbox[0], label.bbox[3] - label.bbox[1]
        angle = -90.0 if h > VERTICAL_ASPECT * w else 0.0
        height = w if angle else h
        source, layer, font = default_source, "", ""
    d, n = row_axes(angle)
    # längs raden: från det tidigaste till det senaste av etiketten och motorns rader; tvärs: motorns om den finns
    along = [_span(label.bbox, d)] + [_span(r.bbox, d) for r in mine]
    a0, a1 = min(s[0] for s in along), max(s[1] for s in along)
    across = [_span(r.bbox, n) for r in mine] or [_span(label.bbox, n)]
    c0, c1 = min(s[0] for s in across), max(s[1] for s in across)
    chars = list(label.text)
    step = (a1 - a0) / max(len(chars), 1)
    old = [g for r in mine for g in r.glyphs if g.char != " "]
    glyphs: list[Glyph] = []
    for i, ch in enumerate(chars):
        s0, s1 = a0 + i * step, a0 + (i + 1) * step
        corners = [(s * d[0] + t * n[0], s * d[1] + t * n[1]) for s in (s0, s1) for t in (c0, c1)]
        box = (min(p[0] for p in corners), min(p[1] for p in corners), max(p[0] for p in corners),
               max(p[1] for p in corners))
        glyphs.append(Glyph(gid=stable_id("xg", page, label.text, i, round(a0, 1)), char=ch, bbox=box,
                            source=source, layer=layer, family_id="external", score=label.confidence))
    # motorns streck följer med till närmaste nya tecken, så att de förblir text
    for g in old:
        if not glyphs:
            break
        k = min(range(len(glyphs)), key=lambda j: abs(project((glyphs[j].cx, glyphs[j].cy), d)
                                                      - project((g.cx, g.cy), d)))
        glyphs[k].path_ids.extend(g.path_ids)
    prov = sorted({p for r in mine for p in r.provenance})
    rid = stable_id("xl", page, label.text, *[round(v, 1) for v in label.bbox])
    return TextRow(rid=rid, page=page, text=label.text, glyphs=glyphs,
                   bbox=bbox_union([g.bbox for g in glyphs]), angle=angle, height=height, source=source,
                   layer=layer, font=font, family="external", provenance=prov, unknown_chars=0)


def apply_external_labels(lines: list[TextRow], labels: list[ExternalLabel],
                          page: int) -> tuple[list[TextRow], LabelReport]:
    """Motorns rader med de externa etiketternas text. Ordningen är deterministisk (lådornas läge)."""
    report = LabelReport(page=page, labels=len(labels))
    if not labels:
        return lines, report
    labels = sorted(labels, key=lambda lb: (round(lb.bbox[1], 1), round(lb.bbox[0], 1), lb.text))
    # varje motorrad till den etikett den överlappar mest
    owner: dict[int, int] = {}
    for i, ln in enumerate(lines):
        best, score = None, MATCH_OVERLAP
        for j, lb in enumerate(labels):
            s = _overlap_of_smaller(ln.bbox, lb.bbox)
            if s >= score and (best is None or s > score):
                best, score = j, s
        if best is not None:
            owner[i] = best
    taken: dict[int, list[TextRow]] = {}
    for i, j in owner.items():
        taken.setdefault(j, []).append(lines[i])
    sources = [ln.source for ln in lines]
    default_source = max(set(sources), key=sources.count) if sources else "stroke"
    out = [ln for i, ln in enumerate(lines) if i not in owner]
    for j, lb in enumerate(labels):
        mine = taken.get(j, [])
        new = _line_for(lb, mine, page, default_source)
        out.append(new)
        if mine:
            report.replaced_lines += len(mine)
            before = " ".join(r.text for r in mine)
            if _norm(before) == _norm(lb.text):
                report.unchanged += 1
            else:
                report.changed += 1
                report.examples.append({"engine": before, "external": lb.text, "bbox": [round(v, 1) for v in lb.bbox],
                                        "confidence": lb.confidence})
        else:
            report.added += 1
        if lb.uncertain:
            report.uncertain += 1
            report.uncertain_rids.add(new.rid)
    out.sort(key=lambda r: r.rid)
    return out, report
