"""Entreprenadgräns: var på ett rör kontraktet slutar (VS-Kalkyl SPEC 6.4.3 h.3, h.6).

En not som `ENTR. GRÄNS` med en kort ledare till ett rör säger att röret bortom punkten hör till någon annans
entreprenad - oftast en ledning som går ut ur huset mot en anslutningspunkt. Den sträckan ska varken räknas som
bekräftad eller som otilldelad: den är en egen kategori, "utanför entreprenadgräns".

Noten känns igen på sin text (BOUNDARY_WORDS, utan mellanslag, punkter och prickar över bokstäverna). Ledaren
följs som en kedja av streck som sitter ihop, från noten och bort, och dess fria ände närmast ett rör är
gränspunkten - om röret ligger inom BOUNDARY_REACH. Motorns vanliga ledarsökning används inte: en gränsnots
ledare är ofta kortare än texten, och det är just vad den sökningen kräver för att ta en linje som ledare.

Vilken sida av punkten som är utanför avgörs i ägandet (pipes/ownership.py): den sida där ingen egen etikett
sitter, när den andra sidan har en. Annars avgörs ingenting, och gränsen rapporteras.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from ..geometry.core import point_seg_distance
from ..rules import value as _rule

BOUNDARY_WORDS = ("ENTRGRANS", "ENTREPRENADGRANS", "GRANSFORENTREPRENAD", "ENTREPRENADGRANSEN")
START_REACH = 0.5         # texthöjder: så nära notens låda börjar dess ledare (understrykningen ligger under texten)
JOIN_TOL = 1.0            # pt: två streck vars ändar ligger så nära sitter ihop
MAX_STROKES = 6           # en ledare är några få streck; fler än så är något annat ritat
BOUNDARY_REACH = 6.0      # pt: så långt från röret får ledaren sluta (samma som en fångad ledarände, h.1)
NOTE_RADIUS = 200.0       # pt: streck längre bort från noten än så kan inte vara dess ledare (sökningens gräns)


def _R(rule_id: str, default):
    return _rule(rule_id, default)


@dataclass
class ContractBoundary:
    """En punkt på ett rör där entreprenaden slutar."""
    page: int
    note: str
    note_bbox: tuple[float, float, float, float]
    point: tuple[float, float]                 # punkten på röret
    family: str | None                         # rörets familj; None = ledaren nådde inget rör
    prim_id: int | None
    distance: float | None                     # pt från ledarens ände till röret


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text.upper())
    return "".join(c for c in s if c.isalpha() and not unicodedata.combining(c))


def is_boundary_note(text: str) -> bool:
    n = _norm(text)
    return any(n.startswith(w) for w in BOUNDARY_WORDS)


def _near_box(pt, box, reach) -> bool:
    x0, y0, x1, y1 = box
    return x0 - reach <= pt[0] <= x1 + reach and y0 - reach <= pt[1] <= y1 + reach


def _inside(s, box, m: float = 0.5) -> bool:
    return _near_box((s.x0, s.y0), box, m) and _near_box((s.x1, s.y1), box, m)


def _leader_ends(box, strokes, height: float) -> list[tuple[float, float]]:
    """Fria ändar på kedjan av streck som börjar vid notens låda, utanför lådan. Streck inne i lådan är text."""
    reach = _R("semantics.boundary.START_REACH", START_REACH) * height
    strokes = [s for s in strokes if not _inside(s, box)]
    join = _R("semantics.boundary.JOIN_TOL", JOIN_TOL)
    most = int(_R("semantics.boundary.MAX_STROKES", MAX_STROKES))
    start = [i for i, s in enumerate(strokes)
             if _near_box((s.x0, s.y0), box, reach) or _near_box((s.x1, s.y1), box, reach)]
    chain = set(start)
    frontier = list(start)
    while frontier and len(chain) <= most:
        i = frontier.pop()
        s = strokes[i]
        for j, t in enumerate(strokes):
            if j in chain:
                continue
            if any(math.hypot(p[0] - q[0], p[1] - q[1]) <= join
                   for p in ((s.x0, s.y0), (s.x1, s.y1)) for q in ((t.x0, t.y0), (t.x1, t.y1))):
                chain.add(j)
                frontier.append(j)
    if len(chain) > most:
        return []                      # kedjan växer in i ritningen: det är ingen ledare
    ends = []
    pts = [p for i in chain for p in ((strokes[i].x0, strokes[i].y0), (strokes[i].x1, strokes[i].y1))]
    for p in pts:
        shared = sum(1 for q in pts if math.hypot(p[0] - q[0], p[1] - q[1]) <= join)
        if shared == 1 and not _near_box(p, box, reach):
            ends.append(p)
    return ends


def find_contract_boundaries(page_index: int, blocks, strokes, graphs: dict) -> list[ContractBoundary]:
    """Gränspunkterna på bladets rör, en per gränsnot.

    strokes: bladets streck som inte är rör (Seg), där ledarna finns. graphs: rörfamiljernas grafer."""
    reach = _R("semantics.boundary.BOUNDARY_REACH", BOUNDARY_REACH)
    out: list[ContractBoundary] = []
    for b in sorted(blocks, key=lambda b: b.bid):
        text = " ".join(getattr(r.line, "text", "") for r in b.rows)
        if not is_boundary_note(text):
            continue
        r = NOTE_RADIUS
        near = [s for s in strokes
                if s.bbox()[0] <= b.bbox[2] + r and s.bbox()[2] >= b.bbox[0] - r
                and s.bbox()[1] <= b.bbox[3] + r and s.bbox()[3] >= b.bbox[1] - r]
        best: tuple[float, str, int, tuple[float, float]] | None = None
        height = b.height or (b.bbox[3] - b.bbox[1])
        for end in _leader_ends(b.bbox, near, height):
            for fk, g in graphs.items():
                for pid, prim in g.prims.items():
                    d, t = point_seg_distance(end[0], end[1], prim.seg)
                    if d <= reach and (best is None or d < best[0]):
                        sg = prim.seg
                        best = (d, fk, pid, (sg.x0 + t * (sg.x1 - sg.x0), sg.y0 + t * (sg.y1 - sg.y0)))
        if best is None:
            out.append(ContractBoundary(page_index, text, tuple(b.bbox), (b.bbox[0], b.bbox[3]), None, None, None))
        else:
            d, fk, pid, pt = best
            out.append(ContractBoundary(page_index, text, tuple(b.bbox), pt, fk, pid, d))
    return out
