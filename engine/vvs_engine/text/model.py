"""Text model shared between searchable PDF text and vector stroke/outline glyphs."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..geometry.core import bbox_union, stable_id


@dataclass
class Glyph:
    gid: str
    char: str                      # recognized character or '?'
    bbox: tuple[float, float, float, float]
    source: str                    # 'text' (searchable) | 'stroke' (vector strokes) | 'outline' (filled outlines)
    layer: str = ""
    family_id: str | None = None
    score: float = 1.0
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    path_ids: list[str] = field(default_factory=list)
    span_id: str | None = None

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def h(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def w(self) -> float:
        return self.bbox[2] - self.bbox[0]


@dataclass
class TextRow:
    rid: str
    page: int
    text: str
    glyphs: list[Glyph]
    bbox: tuple[float, float, float, float]
    angle: float                   # degrees, reading direction (0 = left-to-right horizontal)
    height: float                  # typical glyph height (pt)
    source: str                    # 'text' | 'stroke' | 'outline'
    layer: str = ""
    font: str = ""
    family: str = ""               # text/glyph family id
    provenance: list[str] = field(default_factory=list)   # path ids / span ids
    unknown_chars: int = 0
    # text en extern läsare läst med konfidens över sin tröskel (text/external_labels.py): grammatiken får inte
    # skriva om den – inga tvillingbyten (2/Z, 0/O …), ingen komplettering av avskurna ord
    protected: bool = False

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    def as_dict(self) -> dict[str, Any]:
        return {"rid": self.rid, "page": self.page, "text": self.text, "bbox": [round(v, 2) for v in self.bbox],
                "angle": round(self.angle, 1), "height": round(self.height, 2), "source": self.source, "layer": self.layer,
                "font": self.font, "family": self.family, "unknown_chars": self.unknown_chars,
                "glyphs": [{"c": g.char, "bbox": [round(v, 2) for v in g.bbox], "family": g.family_id, "score": round(g.score, 3)} for g in self.glyphs],
                "provenance": self.provenance[:200]}


def make_row(page: int, glyphs: list[Glyph], angle: float, source: str, layer: str = "", font: str = "", family: str = "") -> TextRow:
    """Build a row from glyphs already ordered in reading direction."""
    text = "".join(g.char for g in glyphs)
    bbox = bbox_union([g.bbox for g in glyphs])
    hs = sorted(g.h for g in glyphs if g.char != " ")
    height = hs[len(hs) // 2] if hs else 0.0
    prov: list[str] = []
    for g in glyphs:
        prov.extend(g.path_ids)
        if g.span_id:
            prov.append(g.span_id)
    rid = stable_id("row", page, text, f"{bbox[0]:.1f}", f"{bbox[1]:.1f}", f"{angle:.0f}", source)
    return TextRow(rid=rid, page=page, text=text, glyphs=glyphs, bbox=bbox, angle=angle, height=height, source=source,
                   layer=layer, font=font, family=family, provenance=sorted(set(prov)), unknown_chars=sum(1 for g in glyphs if g.char == "?"))


def row_axes(angle: float) -> tuple[tuple[float, float], tuple[float, float]]:
    a = math.radians(angle)
    d = (math.cos(a), math.sin(a))
    n = (-d[1], d[0])
    return d, n


def project(pt: tuple[float, float], axis: tuple[float, float]) -> float:
    return pt[0] * axis[0] + pt[1] * axis[1]
