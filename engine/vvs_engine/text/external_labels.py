"""Etiketter som en annan läsare redan läst, in i motorns textrader.

VS-Kalkyls T5 läser etiketterna med två oberoende läsare (vision och OCR) och läser om där de inte är överens.
Den här modulen låter motorn använda de läsningarna utan att något annat i motorn ändras:

- Motorns egna rader läses som vanligt. Varje extern etikett tar de motorrader den täcker. Raderna ersätts av en
  rad med etikettens *text*, medan motorns *geometri* behålls: läsriktning, texthöjd och tvärsled kommer från
  motorns rader, och deras streck (`provenance`) följer med, så att det som är text aldrig blir rör.
- Läsarens låda är ungefärlig och ofta något för stor. Där en etikett ligger på ett rör skulle en för stor
  låda göra rörlinjen till etikettens understrykning, och röret försvinna ur rörgrafen. Lådan dras därför åt till
  bläcket: de streck (och textobjekt) på sidan som ligger *helt* inne i läsarens låda. Ett rör som går genom
  lådan ligger aldrig helt inne i den.
- En etikett som motorn inte läste alls blir en ny rad i bläckets låda. Finns inget bläck i lådan används den inte.
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
from dataclasses import dataclass, field, replace
from typing import Any

from ..geometry.core import GridIndex, bbox_union, stable_id
from .model import Glyph, TextRow, project, row_axes

ARTIFACT_SCHEMA = "vvs_engine.external_labels/1"

# En motorrad hör till en etikett när så stor del av den mindre av de två lådorna ligger i den andra.
# Etikettens låda är något större än tecknen (läsaren ritar den med marginal), motorns är tät runt strecken.
MATCH_OVERLAP = 0.5
# Under denna konfidens (läsarens egen skala 0-1) är etiketten osäker: den används, men det den namnger granskas.
MIN_CONFIDENCE = 0.8
# Lodrät etikett utan motorrad: lådan är så här många gånger högre än bred.
VERTICAL_ASPECT = 1.5
# Ett tecknens streck är högst så här långt, räknat i den åtdragna lådans korta sida (texthöjden). En
# understrykning eller en ledarlinje är lika lång som etiketten och räknas inte som bläck: kom den med skulle
# raden täcka sin egen understrykning, och ledarlinjen som börjar där skulle inte längre hittas.
GLYPH_STROKE_MAX = 1.5


@dataclass(frozen=True)
class ExternalLabel:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float
    flags: tuple[str, ...] = ()
    method: str = ""
    min_confidence: float = MIN_CONFIDENCE    # läsarens egen tröskel (etikettfilens _meta.min_read_confidence)

    @property
    def uncertain(self) -> bool:
        return self.confidence < self.min_confidence or "needs_review" in self.flags


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
    no_ink: int = 0                  # inget streck helt inne i lådan och ingen motorrad: etiketten användes inte
    symbols: int = 0                 # utan bokstäver och siffror (`=`, `-`): tecken, inte text – användes inte
    examples: list[dict[str, Any]] = field(default_factory=list)
    uncertain_rids: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {"artifact_schema": ARTIFACT_SCHEMA, "page": self.page, "labels": self.labels,
                "replaced_lines": self.replaced_lines, "unchanged": self.unchanged, "changed": self.changed,
                "added": self.added, "uncertain": self.uncertain, "no_ink": self.no_ink, "symbols": self.symbols,
                "examples": self.examples[:200]}


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
        threshold = float((data.get("_meta") or {}).get("min_read_confidence") or MIN_CONFIDENCE)
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
                                            str(r.get("method") or ""), threshold))
            by_page[int(key)] = labels
        meta = data.get("_meta") or {}
        return cls(by_page, hashlib.sha256(raw).hexdigest(), {k: meta.get(k) for k in ("models", "model", "ocr")})

    def without(self, pages) -> ExternalLabels:
        """Samma fil utan de angivna bladen (t.ex. blad som vreds innan de lästes)."""
        return ExternalLabels({p: v for p, v in self.by_page.items() if p not in set(pages)}, self.sha256,
                              {**self.meta, "dropped_pages": sorted(set(pages))})

    def apply(self, lines: list[TextRow], page: int, raw_page: Any = None) -> tuple[list[TextRow], LabelReport]:
        ink = Ink.of(raw_page) if raw_page is not None else None
        return apply_external_labels(lines, self.by_page.get(page, []), page, ink)


class Ink:
    """Sidans streck och textobjekt som lådor, för att dra åt en läsares låda till tecknen den omger."""

    def __init__(self, boxes: list[tuple[float, float, float, float]]):
        self.boxes = boxes
        self.index = GridIndex(cell=24.0)
        for i, b in enumerate(boxes):
            self.index.insert(i, b)

    @classmethod
    def of(cls, raw_page: Any) -> Ink:
        boxes = []
        for p in getattr(raw_page, "paths", []):
            for sg in p.segs:
                boxes.append((min(sg.x0, sg.x1), min(sg.y0, sg.y1), max(sg.x0, sg.x1), max(sg.y0, sg.y1)))
        for sp in getattr(raw_page, "spans", []):
            boxes.append(tuple(sp.bbox))
        return cls(boxes)

    def inside(self, box: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
        """Lådan runt de tecken-stora streck som ligger helt inne i `box`, eller None."""
        found = [self.boxes[i] for i in self.index.query(box)
                 if box[0] <= self.boxes[i][0] and self.boxes[i][2] <= box[2]
                 and box[1] <= self.boxes[i][1] and self.boxes[i][3] <= box[3]]
        if not found:
            return None
        rough = bbox_union(found)
        side = min(rough[2] - rough[0], rough[3] - rough[1])
        longest = GLYPH_STROKE_MAX * max(side, 1e-6)
        strokes = [b for b in found if max(b[2] - b[0], b[3] - b[1]) <= longest]
        return bbox_union(strokes) if strokes else None


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
    """En rad med etikettens text i motorns geometri. `label.bbox` är redan åtdragen till bläcket."""
    box = label.bbox
    if mine:
        angle = mine[0].angle
        height = statistics.median(r.height for r in mine)
        source, layer, font = mine[0].source, mine[0].layer, mine[0].font
    else:
        w, h = box[2] - box[0], box[3] - box[1]
        angle = -90.0 if h > VERTICAL_ASPECT * w else 0.0
        height = w if angle else h
        source, layer, font = default_source, "", ""
    d, n = row_axes(angle)
    # längs raden: från det tidigaste till det senaste av etiketten och motorns rader; tvärs: motorns om den finns
    along = [_span(box, d)] + [_span(r.bbox, d) for r in mine]
    a0, a1 = min(s[0] for s in along), max(s[1] for s in along)
    across = [_span(r.bbox, n) for r in mine] or [_span(box, n)]
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
                   layer=layer, font=font, family="external", provenance=prov, unknown_chars=0,
                   protected=not label.uncertain)


def _one_per_place(labels: list[ExternalLabel]) -> list[ExternalLabel]:
    """En läsning per ställe: en etikett på samma ställe som en längre, vars text den är en del av, är ett
    fragment av den (text som skars av en bits kant) och används inte. Två stackade rader med samma text skulle
    annars se ut som två etiketter som ska ha var sitt rör."""
    order = sorted(labels, key=lambda lb: (-len(_norm(lb.text)), -lb.confidence, lb.bbox))
    kept: list[ExternalLabel] = []
    for lb in order:
        t = _norm(lb.text)
        if any(_overlap_of_smaller(k.bbox, lb.bbox) >= MATCH_OVERLAP and t in _norm(k.text) for k in kept):
            continue
        kept.append(lb)
    return sorted(kept, key=lambda lb: (round(lb.bbox[1], 1), round(lb.bbox[0], 1), lb.text))


def apply_external_labels(lines: list[TextRow], labels: list[ExternalLabel], page: int,
                          ink: Ink | None = None) -> tuple[list[TextRow], LabelReport]:
    """Motorns rader med de externa etiketternas text. Ordningen är deterministisk (lådornas läge)."""
    report = LabelReport(page=page, labels=len(labels))
    if not labels:
        return lines, report
    labels = sorted(labels, key=lambda lb: (round(lb.bbox[1], 1), round(lb.bbox[0], 1), lb.text))
    # lådorna dras åt till bläcket före allt annat, så att en för stor låda varken tar en grannrad eller ett rör
    inked: list[bool] = []
    if ink is not None:
        tight = [ink.inside(lb.bbox) for lb in labels]
        inked = [t is not None for t in tight]
        labels = [replace(lb, bbox=t) if t is not None else lb for lb, t in zip(labels, tight)]
    # `=` mellan en kod och dess beskrivning i bladets lista, ett ensamt `-`: tecken, inte ord. Motorn läser dem
    # inte som text, och en sådan rad mellan kod och beskrivning gör att listan inte känns igen.
    words = [lb for lb in labels if any(c.isalnum() for c in lb.text)]
    report.symbols = len(labels) - len(words)
    labels = _one_per_place(words)
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
        if mine and _norm(" ".join(r.text for r in mine)) == _norm(lb.text):
            # motorn läste redan samma text: dess rad står kvar orörd, med sin egen geometri - men en säker
            # extern läsning av samma text skyddar den mot grammatikens omläsning
            for r in mine:
                r.protected = not lb.uncertain
            out.extend(mine)
            report.unchanged += 1
            if lb.uncertain:
                report.uncertain += 1
                report.uncertain_rids.update(r.rid for r in mine)
            continue
        if inked and not inked[j] and not mine:
            report.no_ink += 1          # varken motorn eller bläcket bär den här texten
            continue
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
