"""Leader endpoint -> physical pipe attachment and PipeCodeAnchors.

Attachment points are real geometry: the leader's far endpoint and the centers of crossing tick marks that sit on
the leader. Contacted primitives are grouped by vector family (layer|style). Designation rows of a block are mapped
to contacted groups either directly (single row, single group) or through drawing-local system-token / layer-token
compatibility, and the mapping must be a bijection; otherwise the attachment is AMBIGUOUS. Never nearest-distance.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..pipes.ink import is_stroked
from ..geometry.core import GridIndex, Seg, dist, point_seg_distance, stable_id
from ..pdf.extract import RawPage, RawPath
from ..pipes.representation import stroke_family
from ..profile.layers import layer_tokens
from .annotation import AnnotationBlock, Designation
from .. import disciplines as _disciplines
from .leaders import Leader

from .. import rules as _rules


def _R(rule_id, default):
    """Vad regeln står på för den läsning som körs på den här tråden."""
    return _rules.value(rule_id, default)


CONTACT_TOL = 0.6
MARKER_MAX = 3.0          # pt: closed end markers (dots, small circles) of pipes
DASH_GAP_MAX = 8.0        # pt: the widest drawn gap in a dashed run a leader end may land in
NEAR_MISS = 6.0           # pt: how far short of its pipe a leader may stop and still be pointing at it
NEAR_MISS_NEAREST = True  # flera saker inom räckhåll: ta den närmaste om ingen annan är nästan lika nära (h.1)
NEAR_MISS_CONFIDENCE = 0.7   # ett rör som bara nås genom en sådan fångst (VS-Kalkyl SPEC 6.4.3 h.1)
NEAR_ONE = 2.5            # pt: ...and how close the candidates must lie to each other to be one place
EDGES_APART_MIN = 1.5     # pt: a pipe's two drawn edges never lie closer than the pen allows
EDGES_APART_MAX = 8.0     # pt: ...nor further apart than a pipe's outer diameter is ever drawn
EDGES_PARALLEL = 2.0      # degrees: the two edges of one pipe run the same way
TICK_ACROSS_ANGLE = 20.0  # degrees: a tick is drawn across a pipe; a stroke that runs along it does not cross it
TICK_ACROSS = "tick_across"   # a contact the tick's own stroke made, where it crosses a pipe line
SYMBOL_EDGE_TOL = 0.2    # PDF points: export rounding at a symbol's actual contour


@dataclass
class Contact:
    point: tuple[float, float]
    kind: str            # 'end' | 'crossing_tick' | 'end_tick' | 'via_symbol' | 'via_marker' | 'via_fitting'
    family: str
    pid: str
    seg_index: int
    distance: float
    mark_id: str | None = None
    via: str | None = None

    def as_dict(self):
        return {"point": [round(self.point[0], 2), round(self.point[1], 2)], "kind": self.kind, "family": self.family,
                "pid": self.pid, "seg_index": self.seg_index, "distance": round(self.distance, 3), "mark_id": self.mark_id, "via": self.via}


@dataclass
class PipeCodeAnchor:
    anchor_id: str
    page: int
    designation_id: str
    designation: str
    designation_display: str        # with a dimension row folded in, the name the drawing states
    system_token: str
    dn: int | None
    multiplier: int
    block_id: str
    leader_id: str
    leader_paths: list[str]
    endpoint: tuple[float, float]
    state: str                      # VERIFIED_PIPE_ATTACHMENT | AMBIGUOUS_PIPE_ATTACHMENT | NO_PIPE_ATTACHMENT
    reason: str
    contacts: list[Contact] = field(default_factory=list)
    candidate_families: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def pipe_prims(self) -> list[tuple[str, int]]:
        return sorted({(c.pid, c.seg_index) for c in self.contacts})

    def as_dict(self):
        return {"anchor_id": self.anchor_id, "page": self.page, "designation_id": self.designation_id, "designation": self.designation,
                "designation_display": self.designation_display,
                "system": self.system_token, "dn": self.dn, "multiplier": self.multiplier, "block_id": self.block_id,
                "leader_id": self.leader_id, "leader_source_paths": self.leader_paths,
                "leader_endpoint": [round(self.endpoint[0], 2), round(self.endpoint[1], 2)], "state": self.state, "reason": self.reason,
                "contacts": [c.as_dict() for c in self.contacts], "raw_pipe_source_paths": sorted({c.pid for c in self.contacts}),
                "raw_pipe_segments": [f"{p}#{k}" for p, k in self.pipe_prims], "candidate_families": self.candidate_families,
                "evidence": self.evidence}


# ---------------------------------------------------------------------------------------------------------------
# system token <-> layer token compatibility (drawing-derived; wildcard x/X in layer tokens matches digits)
# ---------------------------------------------------------------------------------------------------------------

def layer_system_tokens(page) -> frozenset[str]:
    """Every layer-name token of the page shaped like a system code (letters then digits).

    These are the system names the file writes about itself, and they decide when a shorter token is an
    abbreviation of a designation and when it is a different system altogether."""
    out: set[str] = set()
    for p in page.paths:
        for t in layer_tokens(p.layer or ""):
            TU = t.upper()
            if re.fullmatch(r"[A-ZÅÄÖ]+\d+", TU):
                out.add(TU)
    return frozenset(out)


# how exactly a layer token names a system: the name itself, a numbered pattern, the letters alone, a tail
MATCH_EXACT, MATCH_PATTERN, MATCH_ALPHA, MATCH_CLASS_AND_TAIL, MATCH_CLASS, MATCH_CLASS_PREFIX, MATCH_TAIL = 0, 1, 2, 3, 4, 5, 6

# Lagerklasserna ur BSAB 96 / BH90 som svenska VVS-lager bär i namnet: "V-52BB-..." är tappkallvatten,
# "V-52BC-..." tappvarmvatten, "V-52BD-..." varmvattencirkulation. Det är en nationell klassindelning, samma
# på varje handling som följer den - ingen enskild ritnings vana - och den säger vilket system en penna ritar
# när lagrets övriga tecken inte gör det. Ett system som *börjar* på klassens bokstäver (VVC på ett
# varmvattenlager) hör dit svagare: cirkulationen ritas ofta på varmvattnets lager, men varmvattnet ritas inte
# på cirkulationens.
LAYER_CLASS = {"52BB": "KV", "52BC": "VV", "52BD": "VVC"}


def _layer_class() -> dict:
    """The layer classes of the discipline being read; VVS reads the table above as it stands."""
    return _disciplines.value("attachment.LAYER_CLASS", LAYER_CLASS)


def system_layer_rank(system_token: str, layer: str, spelled_out: frozenset[str] = frozenset()) -> tuple[int, str] | None:
    """The strongest statement the layer name makes about this system: (how exactly it names it, the token).

    A layer that writes the system's own name says more about it than one that only carries its letters, so where
    a drawing has both, the exact one is the layer the designation belongs to.

    spelled_out: the system names the drawing writes in full on some layer of its own. A designation whose system
    is among them is not read as an abbreviation of a shorter token, because the file already says where that
    system lives: a system with a layer of its own is not the shorter system whose name it happens to end with."""
    S = system_token.upper()
    m = re.match(r"([A-ZÅÄÖ]+)(\d*)", S)
    alpha = m.group(1) if m else S
    digits = m.group(2) if m else ""
    toks = layer_tokens(layer)
    # a layer carrying a fully specified token of the same alpha family with OTHER digits is specific to that system
    for T in toks:
        TU = T.upper()
        m2 = re.fullmatch(r"([A-ZÅÄÖ]+)(\d+)", TU)
        if m2 and m2.group(1) == alpha and digits and m2.group(2) != digits:
            return None
    best: tuple[int, str] | None = None
    for T in toks:
        TU = T.upper()
        if len(TU) < 2:
            continue
        r = None
        if TU == S:
            r = MATCH_EXACT
        elif "X" in TU and re.fullmatch(r"[A-ZÅÄÖ0-9]+", TU) and \
                re.fullmatch("".join(r"\d" if c == "X" and i >= 1 else re.escape(c) for i, c in enumerate(TU)), S):
            r = MATCH_PATTERN
        elif TU == alpha and len(alpha) >= 2:
            r = MATCH_ALPHA
        elif TU.replace(".", "") in _layer_class():
            cls = _layer_class()[TU.replace(".", "")]
            if alpha == cls:
                r = MATCH_CLASS
            elif alpha.startswith(cls):
                r = MATCH_CLASS_PREFIX
        # abbreviated system token: the layer token is the tail of the designation's system token with the same
        # digits (KV2 -> V2, VV1 -> V1); a token of another alpha family with other digits was excluded above
        elif len(S) > len(TU) and S.endswith(TU) and re.fullmatch(r"[A-ZÅÄÖ]+\d+", TU) and S not in spelled_out:
            r = MATCH_TAIL
        if r is not None and (best is None or r < best[0]):
            best = (r, T)
    # Two layers of one class, told apart by their tails: V-52BB-FE--V1- and V-52BB-FE--V2- are both cold water,
    # and the class alone ranks them the same for KV1. The tail says which of the two is system 1. So a class
    # match on a layer whose tail also agrees with the designation's digits outranks a class match alone - the
    # class names the system's family, the tail its number, and a layer that says both says more.
    if best is not None and best[0] == MATCH_CLASS and any(
            len(S) > len(T.upper()) and S.endswith(T.upper()) and re.fullmatch(r"[A-ZÅÄÖ]+\d+", T.upper()) for T in toks):
        best = (MATCH_CLASS_AND_TAIL, best[1])
    return best


def system_layer_match(system_token: str, layer: str, spelled_out: frozenset[str] = frozenset()) -> str | None:
    """Return the matching layer token if the layer name structurally carries the designation's system token."""
    best = system_layer_rank(system_token, layer, spelled_out)
    return best[1] if best else None


def contact_points(ld: Leader) -> list[tuple[tuple[float, float], str, str | None]]:
    pts: list[tuple[tuple[float, float], str, str | None]] = []
    if ld.end_marks:
        for m in ld.end_marks:
            pts.append((ld.end, "end_tick", m.mid))
    else:
        pts.append((ld.end, "end", None))
    for m in ld.crossing_marks:
        pts.append((((m.bbox[0] + m.bbox[2]) / 2, (m.bbox[1] + m.bbox[3]) / 2), "crossing_tick", m.mid))
    return pts


class GeometryIndex:
    """Index over stroke segments of candidate (non-annotation) families."""

    def __init__(self, page: RawPage, exclude_families: set[str], exclude_pids: set[str]):
        self.idx = GridIndex(cell=12.0)
        self.items: list[tuple[RawPath, int, Seg]] = []
        self.tol = _R("semantics.attachment.CONTACT_TOL", CONTACT_TOL)
        # closed small symbols (riser marks, end circles, fittings) are indexed from ALL stroke paths: a circle
        # read as a text glyph ('O', '0') is still the symbol a leader may point at
        self.symbols: list[RawPath] = []
        self.symbol_idx = GridIndex(cell=12.0)
        for p in sorted(page.paths, key=lambda p: p.pid):
            if is_stroked(p) and _is_closed_symbol(p):
                self.symbols.append(p)
                self.symbol_idx.insert(len(self.symbols) - 1, p.bbox)
                # Symbols stay available as bridges, even when their pen also
                # draws pipe. Their perimeter is not itself a pipe contact.
                continue
            # Samma kontrakt som topologin (pipes/ink.py): en fylld form utan penna är en gräns, inte ett
            # streck, och en hänvisningslinje som slutar på en fylld bokstav är inte anknuten till ett rör.
            if not is_stroked(p) or family_of(p) in exclude_families or p.pid in exclude_pids:
                continue
            for k, s in enumerate(p.segs):
                # CAD exports can leave zero-length strokes at a fitting's centre.
                # They carry no run geometry. Treating one as a direct pipe hit
                # prevents the symbol bridge from reaching the pipe at its rim,
                # including during the first vote for the drawing's pipe pens.
                if s.length <= 1e-6:
                    continue
                self.items.append((p, k, s))
                self.idx.insert(len(self.items) - 1, s.bbox())

    def symbols_near(self, x: float, y: float, r: float) -> list[RawPath]:
        return [self.symbols[i] for i in sorted(set(self.symbol_idx.query_point(x, y, r)))]

    def paths_near(self, x: float, y: float, r: float) -> list[RawPath]:
        seen: dict[str, RawPath] = {}
        for i in self.idx.query_point(x, y, r):
            p = self.items[i][0]
            seen.setdefault(p.pid, p)
        return [seen[k] for k in sorted(seen)]

    def hits(self, x: float, y: float, tol: float | None = None, skip_pids: set[str] | None = None) -> list[tuple[RawPath, int, float]]:
        out = []
        tol = self.tol if tol is None else tol
        for i in self.idx.query_point(x, y, tol + 3):
            p, k, s = self.items[i]
            if skip_pids and p.pid in skip_pids:
                continue
            d, t = point_seg_distance(x, y, s)
            if d <= tol + min(0.5 * p.width, 0.5):
                out.append((p, k, d))
        out.sort(key=lambda h: (h[0].pid, h[1]))
        return out


def family_of(p: RawPath) -> str:
    from ..pipes.representation import stroke_family
    return stroke_family(p.layer, p.width, p.color)


def _symbol_hits(symbol: RawPath, gidx: "GeometryIndex", skip: set[str]) -> list[tuple[RawPath, int, float]]:
    """Segments that run through a closed symbol a leader points at.

    A leader ending inside a small circle or square is pointing at what that symbol sits on, and the line it
    marks runs through the symbol - it need not pass within a hair of the leader's last point. The symbol's own
    size is the reach: nothing outside it is contacted."""
    cx = (symbol.bbox[0] + symbol.bbox[2]) / 2
    cy = (symbol.bbox[1] + symbol.bbox[3]) / 2
    r = 0.5 * max(symbol.bbox[2] - symbol.bbox[0], symbol.bbox[3] - symbol.bbox[1])
    from shapely.geometry import LineString
    area = _symbol_area(symbol)
    out: list[tuple[RawPath, int, float]] = []
    for i in gidx.idx.query_point(cx, cy, r + 1.0):
        p, k, sg = gidx.items[i]
        if p.pid in skip or p.pid == symbol.pid:
            continue
        d, _ = point_seg_distance(cx, cy, sg)
        line = LineString([(sg.x0, sg.y0), (sg.x1, sg.y1)])
        if area.distance(line) <= SYMBOL_EDGE_TOL:
            out.append((p, k, d))
    out.sort(key=lambda t: (t[0].pid, t[1]))
    return out


def _symbol_area(symbol: RawPath):
    """The drawn contour, rather than a circular reach around its bounding box."""
    from shapely.geometry import Polygon
    points = [(s.x0, s.y0) for s in symbol.segs]
    points.append((symbol.segs[-1].x1, symbol.segs[-1].y1))
    return Polygon(points).buffer(0)


def _dash_gap_hits(pt: tuple[float, float], gidx: GeometryIndex, pipe_families: set[str] | None,
                   skip: set[str]) -> list[tuple[RawPath, int, float]]:
    """Segments of a dashed run whose drawn gap the point falls into.

    A dashed pipe is ink and gaps; where the leader lands is where the draughtsman aimed, not where the dash
    pattern happens to be. Two ends of the same family, a short gap apart, collinear with each other and with the
    point between them, are one run interrupted by its own pattern - so the point touches that run.
    """
    ends: list[tuple[RawPath, int, Seg, tuple[float, float]]] = []
    for i in gidx.idx.query_point(pt[0], pt[1], _R("semantics.attachment.DASH_GAP_MAX", DASH_GAP_MAX) + 2):
        p, k, sg = gidx.items[i]
        if p.pid in skip or (pipe_families is not None and family_of(p) not in pipe_families):
            continue
        if sg.length < 1e-6:
            continue
        for e in ((sg.x0, sg.y0), (sg.x1, sg.y1)):
            if dist(e, pt) <= _R("semantics.attachment.DASH_GAP_MAX", DASH_GAP_MAX):
                ends.append((p, k, sg, e))
    best: dict[str, tuple[float, tuple, tuple]] = {}
    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            a, b = ends[i], ends[j]
            if family_of(a[0]) != family_of(b[0]) or (a[0].pid == b[0].pid and a[1] == b[1]):
                continue
            gap = dist(a[3], b[3])
            if gap < 1e-6 or gap > _R("semantics.attachment.DASH_GAP_MAX", DASH_GAP_MAX):
                continue
            ux, uy = (b[3][0] - a[3][0]) / gap, (b[3][1] - a[3][1]) / gap
            t = (pt[0] - a[3][0]) * ux + (pt[1] - a[3][1]) * uy
            if t < -0.5 or t > gap + 0.5:
                continue                                    # the point is not inside the gap
            if abs(-(pt[1] - a[3][1]) * ux + (pt[0] - a[3][0]) * uy) > _R("semantics.attachment.CONTACT_TOL", CONTACT_TOL) + 0.5 * max(a[0].width, b[0].width):
                continue                                    # the point is beside the run, not on it
            if _angle_to(a[2], ux, uy) > 5.0 or _angle_to(b[2], ux, uy) > 5.0:
                continue                                    # the two dashes do not continue one straight line
            fk = family_of(a[0])
            if fk not in best or gap < best[fk][0]:
                best[fk] = (gap, a, b)
    out: list[tuple[RawPath, int, float]] = []
    for fk in sorted(best):
        gap, a, b = best[fk]
        out.append((a[0], a[1], dist(a[3], pt)))
        out.append((b[0], b[1], dist(b[3], pt)))
    return out


def _angle_to(sg: Seg, ux: float, uy: float) -> float:
    """Angle in degrees between a segment's line and a direction, folded into [0, 90]."""
    a = math.degrees(math.atan2(sg.y1 - sg.y0, sg.x1 - sg.x0) - math.atan2(uy, ux)) % 180.0
    return min(a, 180.0 - a)


def _near_miss_hits(pt: tuple[float, float], gidx: "GeometryIndex", pipe_families: set[str] | None,
                    skip: set[str], skip_families: set[str] | None = None) -> list[tuple[RawPath, int, float]]:
    """The run a leader stopped a hair short of - but only where there is no doubt which run that is.

    Hand-drawn and hand-edited sheets carry leaders that end a fraction of a millimetre off the line they point
    at. For the eye it is the same thing; for a reading that requires contact it is a label that reaches
    nothing, and the run it names goes unmeasured. So the reach is widened - and the widening is where every
    takeoff that guesses goes wrong, because in a bundle the second-nearest pipe is a hair further away than the
    nearest and belongs to another system entirely.

    What separates the two cases is not distance, it is doubt. Everything within the wider reach is gathered and
    the closest point on each is worked out; if those points sit on top of each other, there is one thing there
    and the leader can only have meant it. If they are apart, there are two things there and the drawing has not
    said which - and no amount of arithmetic on the difference in distance turns that into an answer.
    """
    near: list[tuple[RawPath, int, float, tuple[float, float]]] = []
    for i in gidx.idx.query_point(pt[0], pt[1], _R("semantics.attachment.NEAR_MISS", NEAR_MISS) + 1.0):
        p, k, sg = gidx.items[i]
        if p.pid in skip:
            continue
        fam = family_of(p)
        if pipe_families is not None and fam not in pipe_families:
            continue
        if skip_families and fam in skip_families:
            continue          # the leader's own pen: its tick and its arrow are not the line it points at
        d, t = point_seg_distance(pt[0], pt[1], sg)
        if d <= _R("semantics.attachment.NEAR_MISS", NEAR_MISS):
            near.append((p, k, d, (sg.x0 + t * (sg.x1 - sg.x0), sg.y0 + t * (sg.y1 - sg.y0))))
    if not near:
        return []
    one = _R("semantics.attachment.NEAR_ONE", NEAR_ONE)
    if _R("semantics.attachment.NEAR_MISS_NEAREST", NEAR_MISS_NEAREST):
        # VS-Kalkyl SPEC 6.4.3 h.1: närmaste röret inom räckhåll - om inget annat ligger nästan lika nära
        nearest = min(near, key=lambda t: (t[2], t[0].pid, t[1]))
        same = [t for t in near if math.hypot(t[3][0] - nearest[3][0], t[3][1] - nearest[3][1]) <= one]
        others = [t for t in near if t not in same and t[0].pid != nearest[0].pid]   # samma ritade linje är ingen rival
        ratio = _R("semantics.proximity.AMBIGUITY_RATIO", 1.5)
        if any(t[2] <= ratio * max(nearest[2], 0.1) for t in others):
            return []               # två saker nästan lika nära: ritningen har inte sagt vilken
        return sorted(((p, k, d) for p, k, d, _ in same), key=lambda t: (t[0].pid, t[1]))
    for a in near:
        for b in near:
            if math.hypot(a[3][0] - b[3][0], a[3][1] - b[3][1]) > one:
                return []                   # more than one drawn thing within reach: the sheet has not said which
    return sorted(((p, k, d) for p, k, d, _ in near), key=lambda t: (t[0].pid, t[1]))


def _between_edges_hits(pt: tuple[float, float], gidx: "GeometryIndex", pipe_families: set[str] | None,
                        skip: set[str]) -> list[tuple[RawPath, int, float]]:
    """The pipe a leader ends inside: a run drawn as its two edges, with the leader stopping between them.

    On a sheet drawn to scale a pipe is drawn as its outline, and the leader points into its middle: it stops on
    the centre line, a couple of points from either edge, and touches neither. The near-miss reach refuses that
    on purpose - two things lie within reach, one on each side - and two single-line runs drawn side by side look
    the same from the leader's end. What tells them apart is the drawing itself: a pipe's outline is one stroke,
    its two edges joined round its ends, and two runs are two. So only an end between two long parallel edges of
    ONE drawn path, a pipe's width apart and nothing nearer on either side, has found its pipe; the nearer edge is
    the contact, and the measurement gives a double line's second edge's metres to the first.

    Runs only once the sheet's pipe pens are known: while they are still being elected, a wall drawn as two lines
    would look the same."""
    if not pipe_families:
        return []
    reach = _R("semantics.attachment.EDGES_APART_MAX", EDGES_APART_MAX)
    near: list[tuple] = []      # path, segment, distance, direction, offset across it, where along it, its length
    for i in gidx.idx.query_point(pt[0], pt[1], reach + 1.0):
        p, k, sg = gidx.items[i]
        if p.pid in skip or family_of(p) not in pipe_families:
            continue
        d, t = point_seg_distance(pt[0], pt[1], sg)
        if d > reach or sg.length < 1e-6:
            continue
        ang = math.atan2(sg.y1 - sg.y0, sg.x1 - sg.x0) % math.pi
        nx, ny = -math.sin(ang), math.cos(ang)
        near.append((p, k, d, ang, (pt[0] - sg.x0) * nx + (pt[1] - sg.y0) * ny, t, sg.length))
    if not near:
        return []
    near.sort(key=lambda c: c[2])
    first = near[0]
    # the nearest thing on the other side of the end, measured across the first edge's direction
    nx, ny = -math.sin(first[3]), math.cos(first[3])
    side = 1.0 if first[4] > 0 else -1.0
    opposite = None
    for c in near[1:]:
        sg = c[0].segs[c[1]]
        if ((pt[0] - sg.x0) * nx + (pt[1] - sg.y0) * ny) * side < 0:
            opposite = c
            break
    if opposite is None:
        return []
    a, b = first, opposite
    turn = abs(a[3] - b[3])
    if min(turn, math.pi - turn) > math.radians(_R("semantics.attachment.EDGES_PARALLEL", EDGES_PARALLEL)):
        return []                          # not two edges of one run: they do not run the same way
    apart = a[2] + b[2]
    if not (_R("semantics.attachment.EDGES_APART_MIN", EDGES_APART_MIN) <= apart <= reach):
        return []
    if a[0].pid != b[0].pid:
        return []                          # two drawn paths: two runs side by side, and the sheet has not said which
    if not all(0.0 < c[5] < 1.0 and c[6] >= _R("semantics.attachment.BUNDLE_MIN_RUN", BUNDLE_MIN_RUN) for c in (a, b)):
        return []                          # an end, a cap or a tick is not an edge running past the leader's end
    return [(a[0], a[1], a[2])]


def _tick_across(ld: Leader, mid: str | None, pt: tuple[float, float], gidx: "GeometryIndex",
                 pipe_families: set[str] | None, skip: set[str]) -> list[tuple[RawPath, int, float, tuple[float, float]]]:
    """The two pipe lines one of a leader's ticks is drawn across, and where it crosses each.

    The tick is the drawing's own mark of the pipe a label means, drawn across it. A pipe drawn to scale is two
    lines, and so are two pipes side by side: a tick drawn across both marks both, wherever the leader itself
    happens to stop - on one of them or between them. Reduced to a single point, the tick reached one line at
    most, and where that point touched neither it was taken for a collector whose ends reached several pipes,
    and the label was left to no pipe at all.

    Exactly two parallel lines, or nothing: one is what the point finds already, three or more is a bundle,
    where every row has its own tick, and two that meet under the tick are a corner. Runs only once the sheet's
    pipe pens are known."""
    mark = next((m for m in ld.end_marks + ld.crossing_marks if m.mid == mid), None)
    if not pipe_families or mark is None:
        return []
    tol = _R("semantics.attachment.CONTACT_TOL", CONTACT_TOL)
    least = math.sin(math.radians(TICK_ACROSS_ANGLE))
    own = skip | set(getattr(mark, "path_ids", ()))      # the tick is drawn across the pipe, never as it
    found: dict[tuple[str, int], tuple[RawPath, int, tuple[float, float]]] = {}
    for ts in getattr(mark, "segs", ()):
        tl = ts.length
        if tl < 1e-6:
            continue
        ux, uy = (ts.x1 - ts.x0) / tl, (ts.y1 - ts.y0) / tl
        x0, y0, x1, y1 = ts.bbox()
        for i in gidx.idx.query((x0 - tol, y0 - tol, x1 + tol, y1 + tol)):
            p, k, sg = gidx.items[i]
            if p.pid in own or family_of(p) not in pipe_families or sg.length < 1e-6:
                continue
            vx, vy = (sg.x1 - sg.x0) / sg.length, (sg.y1 - sg.y0) / sg.length
            sin = ux * vy - uy * vx
            if abs(sin) < least:
                continue                     # runs along the tick: not crossed by it
            dx, dy = sg.x0 - ts.x0, sg.y0 - ts.y0
            along_tick = (dx * vy - dy * vx) / sin
            along_pipe = (dx * uy - dy * ux) / sin
            if -tol <= along_tick <= tl + tol and 0.0 <= along_pipe <= sg.length:
                found.setdefault((p.pid, k), (p, k, (sg.x0 + along_pipe * vx, sg.y0 + along_pipe * vy)))
    # the same line split at a node is one line
    deg = _R("semantics.attachment.COLLINEAR_DEG", COLLINEAR_DEG)
    gap = _R("semantics.attachment.COLLINEAR_OFF", COLLINEAR_OFF)
    lines: list[tuple[float, float, list]] = []
    for p, k, cp in found.values():
        sg = p.segs[k]
        a = sg.angle
        th = math.radians(a)
        off = sg.x0 * -math.sin(th) + sg.y0 * math.cos(th)
        for la, lo, hits in lines:
            if min(abs(a - la), 180.0 - abs(a - la)) <= deg and abs(off - lo) <= gap:
                hits.append((p, k, cp))
                break
        else:
            lines.append((a, off, [(p, k, cp)]))
    if len(lines) != 2:
        return []
    # two parallel lines: a pipe's two edges, or two pipes side by side. Two lines meeting under the tick - a
    # radiator's connection turning into its stub - are a corner, and the leader's own end says which it means.
    da = abs(lines[0][0] - lines[1][0])
    if min(da, 180.0 - da) > _R("semantics.attachment.EDGES_PARALLEL", EDGES_PARALLEL):
        return []
    return [(p, k, point_seg_distance(pt[0], pt[1], p.segs[k])[0], cp)
            for _, _, hits in lines for p, k, cp in hits]


def _paired_symbol_ports(points, gidx, pipe_families, skip):
    """Two explicit circles terminating two parallel, spatially distinct pipes.

    A radiator connection symbol can overprint a vertical stem with both pipe
    pens. Centre hits then choose that stem instead of the two horizontal runs
    that actually end on the circles. A pair is established only when each
    indicated circle has one distinct port perpendicular to their centre axis.
    """
    if not any(mid and mid.startswith("explicit_symbol:") for _, _, mid in points):
        return {}
    symbols = {}
    for pt, _, _ in points:
        symbol = _enclosing_symbol(pt, gidx, pipe_families, skip)
        if symbol is not None:
            symbols[symbol.pid] = symbol
    if len(symbols) != 2:
        return {}
    pair = sorted(symbols.values(), key=lambda p:p.pid)
    centers = [((p.bbox[0]+p.bbox[2])/2, (p.bbox[1]+p.bbox[3])/2) for p in pair]
    dx, dy = centers[1][0]-centers[0][0], centers[1][1]-centers[0][1]
    span = math.hypot(dx,dy)
    if not .5 < span <= BUNDLE_SPAN:
        return {}
    dx,dy = dx/span,dy/span
    ports = {}
    for symbol, center in zip(pair, centers):
        # This exception addresses the overprinted stem only. With no exact
        # stem hit, retain the ordinary contact/ambiguity rules at the symbol.
        stem_hits = [(p,k) for p,k,d in gidx.hits(*center, skip_pids=skip | {symbol.pid})
                     if d <= SYMBOL_EDGE_TOL and p.segs[k].length >= BUNDLE_MIN_RUN
                     and (pipe_families is None or family_of(p) in pipe_families)
                     and abs(((p.segs[k].x1-p.segs[k].x0)*dx
                              +(p.segs[k].y1-p.segs[k].y0)*dy)/p.segs[k].length) >= math.cos(math.radians(6))]
        if not stem_hits:
            return {}
        ends = []
        for p,k,d,ep in _pipe_ends_at_marker(symbol,gidx,pipe_families,skip):
            sg = p.segs[k]
            if sg.length < BUNDLE_MIN_RUN:
                continue
            if abs(((sg.x1-sg.x0)*dx+(sg.y1-sg.y0)*dy)/sg.length) <= math.sin(math.radians(6)):
                ends.append((p,k,d,ep))
        if len(ends) != 1:
            return {}
        ports[symbol.pid] = ends
    a,b = [ports[s.pid][0] for s in pair]
    # Each circle must identify its own run, rather than two ends of one line.
    if (a[0].pid,a[1]) == (b[0].pid,b[1]):
        return {}
    ea,eb = a[3],b[3]
    if abs((eb[0]-ea[0])*dx+(eb[1]-ea[1])*dy) < .5:
        return {}
    return ports


def leader_contacts(ld: Leader, gidx: GeometryIndex, pipe_families: set[str] | None, all_paths: dict[str, RawPath],
                    ticks_across: bool = True) -> list[Contact]:
    """Contacts at each attachment point.

    A leader whose end lies inside a small closed symbol (riser mark, end circle, fitting) points at that symbol:
    pipe geometry through the symbol or ending at the symbol group is a 'via_symbol' contact (a weak seed - the
    label describes the symbol's object; DN ticks on the line outrank it). Otherwise the end / tick points give
    direct contacts; tiny markers at the end bridge to pipe ends (via_marker); small symbols touched by the leader
    bridge to pipes touching them (via_fitting).

    ticks_across: whether a tick drawn across two pipe lines marks both (_tick_across). A page read from an image
    has its ticks and lines traced out of pixels, and there a tick that crosses two traced lines did more harm than
    good: on the raster corpus it took 1.4 and 2.4 points of right length off two sheets."""
    out: list[Contact] = []
    skip = set(ld.path_ids)
    seen: set[tuple[str, int]] = set()
    points = contact_points(ld)
    # One leader can pass through two riser centres before terminating. Both
    # are explicitly indicated; nearby circles off the leader are not. This
    # recovers the two pipes of a supply/return pair without copying an
    # identity to arbitrary parallel geometry.
    leader_segs = [Seg(*a, *b) for a, b in zip(ld.points, ld.points[1:])]
    for mark in gidx.symbols_near(*ld.end, 34.0):
        size = max(mark.bbox[2]-mark.bbox[0], mark.bbox[3]-mark.bbox[1])
        center = ((mark.bbox[0]+mark.bbox[2])/2, (mark.bbox[1]+mark.bbox[3])/2)
        if (mark.pid not in skip and size <= MARKER_MAX
                and dist(center, ld.end) <= 34.0 and dist(center, ld.end) > .3
                and any(point_seg_distance(*center, s)[0] <= .2 for s in leader_segs)):
            points.append((center, 'end', 'explicit_symbol:' + mark.pid))
    has_inline_symbols = any(mid and mid.startswith('explicit_symbol:') for _, _, mid in points)
    paired_ports = _paired_symbol_ports(points, gidx, pipe_families, skip)
    for (pt, kind, mid) in points:
        symbol = _enclosing_symbol(pt, gidx, pipe_families, skip)
        hits = gidx.hits(pt[0], pt[1], skip_pids=skip)
        direct = []
        others = []
        for p, k, d in hits:
            fk = family_of(p)
            if pipe_families is None or fk in pipe_families:
                direct.append((p, k, d))
            else:
                others.append((p, k, d))
        if symbol is not None:
            if has_inline_symbols and (pt == ld.end or (mid and mid.startswith('explicit_symbol:'))):
                mid = 'explicit_symbol:' + symbol.pid
            # The small circle terminates a riser. A nearby thick stroke can
            # overlap its interior without being the indicated pipe. Preserve
            # an exact centre contact; otherwise use a run ending on the rim
            # before considering geometry passing through the symbol.
            exact = [(p, k, d) for p, k, d in direct if d <= SYMBOL_EDGE_TOL]
            ends = _pipe_ends_at_marker(symbol, gidx, pipe_families, skip)
            if symbol.pid in paired_ports:
                direct = [(p,k,d) for p,k,d,ep in paired_ports[symbol.pid]]
            elif not exact and ends:
                direct = [(p, k, d) for p, k, d, ep in ends]
            elif exact:
                direct = exact
            # Rör ledarens ände ett rör inne i symbolen är det röret det ritaren pekade ut, och då frågas
            # symbolen inte om vad den mer håller ihop. Det som ytterligare går genom en kopplingscirkel, eller
            # slutar vid den, är rör som kopplas mot varandra där - och en koppling mellan två rör är inte en
            # hänvisning. Bara när änden inte rör något ritat alls är symbolen den enda vägen till ett rör, och
            # då står kandidaterna kvar för att avgöras, inte för att tas allihop.
            through = [] if direct else [(p, k, d) for (p, k, d) in _symbol_hits(symbol, gidx, skip)
                                         if pipe_families is None or family_of(p) in pipe_families]
            for p, k, d in direct + [t for t in through if (t[0].pid, t[1]) not in {(q.pid, kk) for q, kk, _ in direct}]:
                if (p.pid, k) in seen:
                    continue
                seen.add((p.pid, k))
                # The bridge has already established which raw segment the symbol
                # touches. Seed that segment at its projected contact, not at the
                # symbol's centre: the centre can be several points off the pipe
                # and would fail the downstream seed's contact tolerance.
                sg = p.segs[k]
                _, t = point_seg_distance(pt[0], pt[1], sg)
                contact = (sg.x0 + t * (sg.x1 - sg.x0), sg.y0 + t * (sg.y1 - sg.y0))
                out.append(Contact(point=contact, kind="via_symbol", family=family_of(p), pid=p.pid, seg_index=k, distance=d, mark_id=mid, via=symbol.pid))
            # a run drawn up to the symbol rather than through it: whatever ENDS at the marker is what the
            # leader points at. This has to hold while the pipe families are still being voted for, or a drawing
            # whose runs all stop at a connection circle never votes for the pen it draws them with.
            for p in ([] if direct else [symbol] if has_inline_symbols else _marker_cluster([symbol], gidx, pipe_families, skip)):
                for q, kk, de, ep in _pipe_ends_at_marker(p, gidx, pipe_families, skip):
                    if (q.pid, kk) not in seen:
                        seen.add((q.pid, kk))
                        out.append(Contact(point=ep, kind="via_symbol", family=family_of(q), pid=q.pid, seg_index=kk, distance=de, mark_id=mid, via=p.pid))
            continue
        if kind in ("end_tick", "crossing_tick"):
            # A tick drawn across two pipes marks both - the one the leader meets as well as the other.
            across = _tick_across(ld, mid, pt, gidx, pipe_families, skip) if ticks_across else []
            for p, k, d, cp in across:
                if (p.pid, k) in seen:
                    continue
                seen.add((p.pid, k))
                out.append(Contact(point=cp, kind=kind, family=family_of(p), pid=p.pid, seg_index=k,
                                   distance=d, mark_id=mid, via=TICK_ACROSS))
            if across:
                continue
        # En samlingslinje - ett kort, rakt, öppet streck på ledarens EGEN penna som ledaren landar på - är
        # ingen kontakt utan en väg. I den första läsningen, där pennorna ännu röstas fram, räknas varje träff,
        # och då röstade ledaren på samlingslinjens skrivpenna som rör medan pennan strecket faktiskt leder
        # till aldrig fick en röst. Så strecket tas bort ur de direkta träffarna och dess ändar frågas i
        # stället - bara när ledaren inte rörde något annat ritat, så en verklig kontakt aldrig byts bort.
        own = stroke_family(ld.layer, ld.width, ld.color)
        via_own = [(p, k, d) for p, k, d in direct if family_of(p) == own and _collector_shaped(p, pt)]
        real = [(p, k, d) for p, k, d in direct if (p.pid, k) not in {(q.pid, kk) for q, kk, _ in via_own}]
        if not real and via_own:
            bridged = 0
            for p, _, _ in via_own:
                for q, kk, dd, ep in _collector_far_ends(p, pt, gidx, pipe_families, own, skip):
                    if (q.pid, kk) in seen:
                        continue
                    seen.add((q.pid, kk))
                    bridged += 1
                    out.append(Contact(point=ep, kind="via_collector", family=family_of(q), pid=q.pid,
                                       seg_index=kk, distance=dd, mark_id=mid, via=p.pid))
            if bridged:
                continue
            # Strecket leder ingenstans på en annan penna: då är det ingen samlingslinje utan det ritade
            # ledaren rörde vid - på ett blad ritat med EN penna är det själva röret. Kontakten står som förut.
            real = direct
        for p, k, d in real:
            if (p.pid, k) in seen:
                continue
            seen.add((p.pid, k))
            out.append(Contact(point=pt, kind=kind, family=family_of(p), pid=p.pid, seg_index=k, distance=d, mark_id=mid))
        if not direct and pipe_families and others:
            # marker bridge: a dot/tiny marker at the leader end sitting at a pipe END (micro gap). Touching tiny
            # markers form one cluster (stacked end markers of parallel pipes): every pipe end at any marker of
            # the cluster is a contact.
            n_before = len(out)
            for p in _marker_cluster([p for p, _, _ in others if max(p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1]) <= _R("semantics.attachment.MARKER_MAX", MARKER_MAX)], gidx, pipe_families, skip):
                for q, kk, de, ep in _pipe_ends_at_marker(p, gidx, pipe_families, skip):
                    if (q.pid, kk) not in seen:
                        seen.add((q.pid, kk))
                        out.append(Contact(point=ep, kind="via_marker", family=family_of(q), pid=q.pid, seg_index=kk, distance=de, mark_id=mid, via=p.pid))
            if len(out) > n_before:
                continue
            # fitting bridge: small symbol touched by the leader; pipe primitives touching the symbol
            for p, k, d in others:
                w = p.bbox[2] - p.bbox[0]; h = p.bbox[3] - p.bbox[1]
                if max(w, h) > 20:
                    continue
                for s in p.segs:
                    for ep in ((s.x0, s.y0), (s.x1, s.y1), s.mid):
                        for q, kk, dd in gidx.hits(ep[0], ep[1], tol=1.5, skip_pids=skip | {p.pid}):
                            if family_of(q) in pipe_families and (q.pid, kk) not in seen:
                                seen.add((q.pid, kk))
                                out.append(Contact(point=(ep[0], ep[1]), kind="via_fitting", family=family_of(q), pid=q.pid, seg_index=kk, distance=dd, mark_id=mid, via=p.pid))
    if not out:
        # Samlingslinjen. Ritaren drar flera hänvisningslinjer till ETT streck, och det strecket vidare till
        # röret - en linje på skrivpennan som samlar etiketterna. Ledaren tar slut där den landar på strecket,
        # och strecket är ingen rörfamilj, så läsningen sa "linjen rör inget rör" om ett rör som ligger femtio
        # punkter bort med en ritad linje hela vägen dit. Två etiketter som slutar i exakt samma punkt är hur
        # det ser ut.
        #
        # Bara ett rakt, kort, öppet streck (ett eller två segment, under COLLECTOR_MAX) som ledaren landar
        # PÅ - inte ett tecken, inte en ram, inte en lång linje - och bara där ingenting annat alls hittades.
        # Också under röstningen (rörfamiljerna okända): i den andra läsningen står skrivpennorna utanför
        # indexet, så samlingslinjen syns varken som direkt träff eller som "annat" - och utan den här vägen
        # fick pennan strecket leder till aldrig sin röst, och hela systemet stod utan meter.
        # Kontakten tas i strecktes egna ändar, med samma tolerans som beslagsbryggan. Det är en svag kontakt:
        # ett markeringsstreck på röret vinner alltid över den.
        own = stroke_family(ld.layer, ld.width, ld.color)
        for (pt, kind, mid) in contact_points(ld):
            for p in _collectors_at(pt, own, skip, all_paths):
                for q, kk, dd, ep in _collector_far_ends(p, pt, gidx, pipe_families, own, skip):
                    if (q.pid, kk) in seen:
                        continue
                    seen.add((q.pid, kk))
                    out.append(Contact(point=ep, kind="via_collector", family=family_of(q), pid=q.pid,
                                       seg_index=kk, distance=dd, mark_id=mid, via=p.pid))
    if pipe_families:
        # An explicit crossing tick describes its own contact, even when another
        # tick on the same bundle leader already landed on ink. A dash gap at one
        # marked contact must not disappear merely because another pipe is solid
        # at its contact. Unmarked endpoints retain the whole-leader fallback.
        had_contacts = bool(out)
        for (pt, kind, mid) in contact_points(ld):
            if had_contacts and (not mid or kind not in ('crossing_tick', 'end_tick')
                                 or any(c.mark_id == mid for c in out)):
                continue
            if had_contacts and any(family_of(p) in pipe_families
                                    for p, _, _ in gidx.hits(*pt, skip_pids=skip)):
                continue  # an earlier tick may already have emitted this primitive
            for q, kk, dd in _dash_gap_hits(pt, gidx, pipe_families, skip):
                if (q.pid, kk) in seen:
                    continue
                seen.add((q.pid, kk))
                out.append(Contact(point=pt, kind=kind, family=family_of(q), pid=q.pid, seg_index=kk, distance=dd, mark_id=mid))
    if not out:
        # And last of all: the leader stopped just short of the line it points at, with nothing else in reach.
        #
        # This used to run only once the sheet's pipe pens were known, which is a circle: the pens are elected
        # from what the leaders touched, and on a sheet where every leader stops a couple of points short -
        # the drainage plans end their leaders at a small circle on the pipe - nothing was ever touched, no pen
        # was ever elected, and the whole sheet measured nothing. So it runs in the first reading too, where
        # every drawn pen is still a candidate. The leader's own pen is left out of the reach: the tick it ends
        # with is the leader's, not the line it means.
        own_fams = {stroke_family(ld.layer, ld.width, ld.color)}
        for pid in ld.path_ids:
            q = all_paths.get(pid)
            if q is not None:
                own_fams.add(family_of(q))
        for (pt, kind, mid) in contact_points(ld):
            for q, kk, dd in _near_miss_hits(pt, gidx, pipe_families, skip, own_fams):
                if (q.pid, kk) in seen:
                    continue
                seen.add((q.pid, kk))
                out.append(Contact(point=pt, kind="near_miss", family=family_of(q), pid=q.pid, seg_index=kk,
                                   distance=dd, mark_id=mid))
    if not out:
        # ...or it stopped inside the pipe it points at, between the two edges a pipe drawn to scale has.
        for (pt, kind, mid) in contact_points(ld):
            for q, kk, dd in _between_edges_hits(pt, gidx, pipe_families, skip):
                if (q.pid, kk) in seen:
                    continue
                seen.add((q.pid, kk))
                sg = q.segs[kk]           # seeded on the edge itself, not in the middle of the pipe
                _, t = point_seg_distance(pt[0], pt[1], sg)
                out.append(Contact(point=(sg.x0 + t * (sg.x1 - sg.x0), sg.y0 + t * (sg.y1 - sg.y0)),
                                   kind="between_edges", family=family_of(q), pid=q.pid, seg_index=kk,
                                   distance=dd, mark_id=mid))
    out.sort(key=lambda c: (c.pid, c.seg_index))
    return out


WEAK_KINDS = ("via_symbol", "via_marker", "via_fitting", "via_collector")
COLLECTOR_MAX = 90.0      # pt: en samlingslinje är kort; en lång linje på skrivpennan är något annat
SYMBOL_MAX = 20.0         # pt: closed symbols a leader may point at (riser marks, end circles, fittings)


def _is_closed_symbol(p: RawPath) -> bool:
    if p.kind != "s" or len(p.segs) < 3:
        return False
    size = max(p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1])
    if size < 1.0 or size > _R("semantics.attachment.SYMBOL_MAX", SYMBOL_MAX):
        return False
    a = (p.segs[0].x0, p.segs[0].y0); b = (p.segs[-1].x1, p.segs[-1].y1)
    return dist(a, b) <= 0.25


def _enclosing_symbol(pt: tuple[float, float], gidx: GeometryIndex, pipe_families: set[str] | None, skip: set[str]) -> RawPath | None:
    """Smallest closed non-pipe symbol whose box strictly contains the point (the leader points at the symbol)."""
    best = None
    for p in gidx.symbols_near(pt[0], pt[1], 1.0):
        if p.pid in skip:
            continue
        w = p.bbox[2] - p.bbox[0]; h = p.bbox[3] - p.bbox[1]
        m = 0.1 * max(w, h)
        if p.bbox[0] + m <= pt[0] <= p.bbox[2] - m and p.bbox[1] + m <= pt[1] <= p.bbox[3] - m:
            size = max(w, h)
            if best is None or size < best[0] or (size == best[0] and p.pid < best[1].pid):
                best = (size, p)
    return best[1] if best else None


def _collector_shaped(p: RawPath, pt: tuple[float, float]) -> bool:
    """Ett kort, rakt, öppet streck som punkten ligger PÅ - så ser en samlingslinje ut, och inget annat."""
    if p.kind != "s" or not (1 <= len(p.segs) <= 2) or _is_closed_symbol(p):
        return False
    L = sum(sg.length for sg in p.segs)
    if L < 4.0 or L > _R("semantics.attachment.COLLECTOR_MAX", COLLECTOR_MAX):
        return False
    tol = _R("semantics.attachment.CONTACT_TOL", CONTACT_TOL)
    return any(point_seg_distance(pt[0], pt[1], sg)[0] <= tol + 0.5 * p.width for sg in p.segs)


class PathsByPlace(dict):
    """The sheet's paths by id, with a grid over their boxes built the first time a place is asked about.

    The collector search asks, for every leader end, which paths' boxes hold the point. Walking the whole sheet
    for each of them grew as leaders times paths: on a dense sheet with a thousand labels and a hundred thousand
    strokes that was most of the reading's time. The grid answers the same question from the paths near the point.
    """

    def near(self, x: float, y: float, r: float) -> list[tuple[str, RawPath]]:
        idx = self.__dict__.get("_grid")
        if idx is None or idx[1] != len(self):
            keys = list(self.keys())
            grid = GridIndex(cell=24.0)
            for i, k in enumerate(keys):
                grid.insert(i, self[k].bbox)
            idx = (grid, len(self), keys)
            self.__dict__["_grid"] = idx
        grid, _, keys = idx
        return [(keys[i], self[keys[i]]) for i in grid.query_point(x, y, r) if keys[i] in self]


def _paths_near(all_paths: dict[str, RawPath], x: float, y: float) -> list[tuple[str, RawPath]]:
    """Paths whose box could hold the point within a collector's one-point margin; every path when not indexed."""
    if isinstance(all_paths, PathsByPlace):
        return all_paths.near(x, y, 1.5)
    return list(all_paths.items())


def _collectors_at(pt: tuple[float, float], own: str, skip: set[str],
                   all_paths: dict[str, RawPath]) -> list[RawPath]:
    """Samlingslinjer på ledarens egen penna vid punkten, sökta bland bladets alla vägar.

    Inte i indexet: skrivpennorna är just de familjer indexet lämnar utanför i den andra läsningen, och
    samlingslinjen är ritad med en skrivpenna.
    """
    # Proven ställs billigast först. Rutan är fyra jämförelser och avvisar nästan varje väg på bladet; familjen
    # bygger en sträng av lager, bredd och färg. Ställdes familjen först byggdes den strängen för varje väg på
    # bladet en gång per ledarände - på ett blad med sjuttiofemtusen vägar tog läsningen aldrig slut. Svaret är
    # detsamma, bara ordningen är annan.
    #
    # Ordningen på svaret hör till svaret och inte till sökningen: listan sorteras när den är klar, i stället
    # för att bladets alla nycklar sorteras om vid varje anrop.
    x, y = pt
    out = []
    for pid, p in _paths_near(all_paths, x, y):
        if pid in skip:
            continue
        if not (p.bbox[0] - 1.0 <= x <= p.bbox[2] + 1.0 and p.bbox[1] - 1.0 <= y <= p.bbox[3] + 1.0):
            continue
        if family_of(p) != own:
            continue
        if _collector_shaped(p, pt):
            out.append(p)
    out.sort(key=lambda q: q.pid)
    return out


def _collector_far_ends(p: RawPath, pt: tuple[float, float], gidx: GeometryIndex, pipe_families: set[str] | None,
                        own: str, skip: set[str]) -> list[tuple[RawPath, int, float, tuple[float, float]]]:
    """Vad samlingslinjens ändar rör vid: ritad geometri på en annan penna än ledarens egen.

    Med rörfamiljerna kända bara de; under röstningen vilken annan penna som helst - det är så strecket får
    rösta på pennan det faktiskt leder till.
    """
    tol = _R("semantics.attachment.CONTACT_TOL", CONTACT_TOL)
    out: list[tuple[RawPath, int, float, tuple[float, float]]] = []
    for ex, ey in ((p.segs[0].x0, p.segs[0].y0), (p.segs[-1].x1, p.segs[-1].y1)):
        if dist((ex, ey), pt) <= tol:
            continue                                     # änden ledaren kom ifrån
        for q, kk, dd in gidx.hits(ex, ey, tol=1.5, skip_pids=skip | {p.pid}):
            fam = family_of(q)
            if fam == own:
                continue
            if pipe_families is not None and fam not in pipe_families:
                continue
            out.append((q, kk, dd, (ex, ey)))
    return out


def _pipe_ends_at_marker(p: RawPath, gidx: GeometryIndex, pipe_families: set[str] | None, skip: set[str]) -> list[tuple[RawPath, int, float, tuple[float, float]]]:
    """Pipe primitives whose END lies at the marker's edge (within half its size + 1 pt of its centre), with the
    end point (the contact point on the pipe)."""
    mx, my = (p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2
    size = max(p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1])
    R = max(2.5, 0.5 * size + 1.0)
    ends = []
    area = _symbol_area(p) if _is_closed_symbol(p) else None
    for q, kk, dd in gidx.hits(mx, my, tol=R, skip_pids=skip | {p.pid}):
        if pipe_families is not None and family_of(q) not in pipe_families:
            continue
        sg = q.segs[kk]
        cands = [((sg.x0, sg.y0), dist((mx, my), (sg.x0, sg.y0))), ((sg.x1, sg.y1), dist((mx, my), (sg.x1, sg.y1)))]
        ep, de = min(cands, key=lambda t: t[1])
        if de <= R:
            if area is not None:
                from shapely.geometry import Point
                if (area.boundary if size <= MARKER_MAX else area).distance(Point(ep)) > SYMBOL_EDGE_TOL:
                    continue
            ends.append((q, kk, de, ep))
    ends.sort(key=lambda t: (t[0].pid, t[1]))
    return ends


def _marker_cluster(seeds: list[RawPath], gidx: GeometryIndex, pipe_families: set[str] | None, skip: set[str], limit: int = 16) -> list[RawPath]:
    cluster: dict[str, RawPath] = {}
    frontier: list[RawPath] = []
    for p in sorted(seeds, key=lambda p: p.pid):
        if p.pid not in cluster:
            cluster[p.pid] = p
            frontier.append(p)
    while frontier and len(cluster) < limit:
        p = frontier.pop(0)
        mx, my = (p.bbox[0] + p.bbox[2]) / 2, (p.bbox[1] + p.bbox[3]) / 2
        size = max(p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1])
        neighbours = [q for q, _, _ in gidx.hits(mx, my, tol=2.5 + size, skip_pids=skip)] + gidx.symbols_near(mx, my, 2.5 + size)
        for q in sorted(neighbours, key=lambda q: q.pid):
            if q.pid in cluster or q.pid in skip or (pipe_families and family_of(q) in pipe_families) or not is_stroked(q):
                continue
            qsize = max(q.bbox[2] - q.bbox[0], q.bbox[3] - q.bbox[1])
            if qsize > _R("semantics.attachment.MARKER_MAX", MARKER_MAX):
                continue
            gx = max(0.0, max(p.bbox[0], q.bbox[0]) - min(p.bbox[2], q.bbox[2]))
            gy = max(0.0, max(p.bbox[1], q.bbox[1]) - min(p.bbox[3], q.bbox[3]))
            if max(gx, gy) <= max(0.5, 0.35 * max(size, qsize)):
                cluster[q.pid] = q
                frontier.append(q)
    return [cluster[k] for k in sorted(cluster)]



# A bundle is drawn tight - the runs have to be told apart by eye, so they sit a few points from each other and
# not a few tens. Beyond this the lines near a leader endpoint are separate runs that happen to be parallel.
BUNDLE_SPAN = 34.0
# Hur lång en sträcka måste vara för att kunna vara ett rör i en bunt. Mätt på ritningar där en linje slutar vid
# ett stråk: markeringsstrecken tvärs rören är tre punkter, hörnrundningarnas rester under en, och de verkliga
# rören hundratals. Åtta punkter ligger långt över det ena och långt under det andra.
BUNDLE_MIN_RUN = 8.0


def parallel_runs(contacts: list[Contact], paths: dict) -> list[list[Contact]] | None:
    """The contacts split into the distinct parallel runs they sit on, ordered across the bundle.

    A bundle is several runs drawn side by side and named by one stacked label. Returns None when the contacts
    are not a bundle - not parallel, or spread too far to be one - because then there is nothing to order.
    """
    segs = []
    for c in contacts:
        p = paths.get(c.pid)
        if p is None or c.seg_index >= len(p.segs):
            return None
        s = p.segs[c.seg_index]
        if s.length < 1e-6:
            return None
        segs.append((c, s))
    if len(segs) < 2:
        return None
    # Markeringsstrecket är inte röret. En hänvisningslinje som slutar vid ett stråk lämnar ett litet snedstreck
    # tvärs varje rör den menar - tre punkter långt, och ritat på tvären. Räknat som en av linjerna i bunten
    # pekar det åt fel håll, och eftersom riktningen tas från den första kontakten avgjorde ett sådant streck
    # vad hela bunten ansågs luta åt. Då stämde ingen av de verkliga rören med den riktningen och bunten fanns
    # inte. Samma sak med de bråkdelar av punkter som en exporterad hörnrundning lämnar efter sig.
    #
    # Så bunten söks bland de sträckor som kan vara rör. Finns färre än två sådana finns ingen bunt att läsa.
    runs_only = [(c, s) for c, s in segs if s.length >= _R("semantics.attachment.BUNDLE_MIN_RUN", BUNDLE_MIN_RUN)]
    if len(runs_only) >= 2:
        segs = runs_only
    segs.sort(key=lambda z: -z[1].length)      # den längsta sträckan ger buntens riktning, inte den första
    angs = [math.degrees(math.atan2(s.y1 - s.y0, s.x1 - s.x0)) % 180 for _, s in segs]
    a0 = angs[0]
    if any(min(abs(a - a0), 180 - abs(a - a0)) > 6.0 for a in angs):
        return None                       # not one bundle: the lines run different ways
    th = math.radians(a0)
    nx, ny = -math.sin(th), math.cos(th)
    off = [((s.x0 + s.x1) / 2 * nx + (s.y0 + s.y1) / 2 * ny) for _, s in segs]
    if max(off) - min(off) > _R("semantics.attachment.BUNDLE_SPAN", BUNDLE_SPAN):
        return None                       # too far apart to be drawn as one bundle
    runs: list[tuple[float, list[Contact]]] = []
    for (c, _), o in sorted(zip(segs, off), key=lambda z: z[1]):
        if runs and abs(o - runs[-1][0]) <= 1.2:      # the same run, touched twice
            runs[-1][1].append(c)
        else:
            runs.append((o, [c]))
    return [r for _, r in runs]

def bundle_at(contacts: list[Contact], gidx: GeometryIndex, want: int, skip: set[str],
              paths: dict, families: set[str] | None = None) -> list[list[Contact]] | None:
    """De parallella rören vid kontaktpunkten, när en etikett namnger fler än linjen råkade träffa.

    En ritare som drar fram och retur bredvid varandra skriver beteckningen två gånger på två rader och drar EN
    hänvisningslinje till paret. Linjens ände landar på det ena röret. Läsningen såg då en etikett med två rader
    som pekar på ett enda rör, kunde inte avgöra vilken rad som gällde, och kallade hela fallet tvetydigt - så
    både fram och retur blev omätta. Samma sak när KV, VV och VVC går i samma stråk under en etikett med tre
    rader.

    Så här letas partnern upp: bland det bladet ritar med de pennor hänvisningslinjen själv rörde vid
    (`families`; utan den listan: samma penna som första kontakten), parallellt inom sex grader, och inom
    buntens bredd tvärs linjen. Ett stråk med KV, VV och VVC ligger på två lager - kallvatten på ett,
    varmvatten och cirkulation på ett annat - och en linje som slutar med ett streck över var och en av dem
    har rört båda pennorna; en partner får då vara på vilken som helst av dem. Och antalet måste stämma exakt -
    hittas inte lika många rör som etiketten har rader avgörs ingenting och fallet står kvar som tvetydigt.
    Det är skillnaden mot att gissa: två rader och två rör är ett par, två rader och tre rör är en fråga
    ritningen inte har svarat på.
    """
    if not contacts or want < 2:
        return None
    # riktningen tas från den längsta sträckan linjen rörde vid, av samma skäl som ovan: ett markeringsstreck
    # tvärs röret får inte avgöra vad bunten anses luta åt
    cand = []
    for c in contacts:
        p = paths.get(c.pid)
        if p is not None and c.seg_index < len(p.segs) and p.segs[c.seg_index].length > 1e-6:
            cand.append((c, p.segs[c.seg_index]))
    if not cand:
        return None
    base = max(cand, key=lambda z: z[1].length)
    c0, s0 = base
    a0 = math.degrees(math.atan2(s0.y1 - s0.y0, s0.x1 - s0.x0)) % 180
    th = math.radians(a0)
    nx, ny = -math.sin(th), math.cos(th)
    span = _R("semantics.attachment.BUNDLE_SPAN", BUNDLE_SPAN)

    def across(sg):
        return (sg.x0 + sg.x1) / 2 * nx + (sg.y0 + sg.y1) / 2 * ny

    o0 = across(s0)
    found: dict[int, Contact] = {}
    allowed = set(families) if families else {c0.family}
    for p, k, d in gidx.hits(c0.point[0], c0.point[1], tol=span, skip_pids=skip):
        if family_of(p) not in allowed or k >= len(p.segs):
            continue
        sg = p.segs[k]
        if sg.length < _R("semantics.attachment.BUNDLE_MIN_RUN", BUNDLE_MIN_RUN):
            continue                       # ett märke eller en hörnrest, inte ett rör i bunten
        a = math.degrees(math.atan2(sg.y1 - sg.y0, sg.x1 - sg.x0)) % 180
        if min(abs(a - a0), 180 - abs(a - a0)) > 6.0:
            continue
        off = across(sg)
        if abs(off - o0) > span:
            continue
        key = round(off / 1.2)
        if key not in found or d < found[key].distance:
            found[key] = Contact(point=c0.point, kind="bundle_partner", family=family_of(p), pid=p.pid,
                                 seg_index=k, distance=d, via=c0.pid)
    if len(found) != want:
        return None
    return [[found[k]] for k in sorted(found)]


COLLINEAR_DEG = 6.0       # grader: två sträckor ligger på samma linje när de lutar likadant...
COLLINEAR_OFF = 1.2       # pt: ...och ligger på samma ställe i sidled


def _distinct_runs(cs: list[Contact], paths: dict | None) -> int:
    """Hur många skilda rör kontakterna sitter på.

    Två kontakter hör till samma rör när deras sträckor ligger på samma linje. En brygga mitt på ett stråk rör
    samma rör två gånger, en gång åt vardera hållet, och det är ett rör. Lutar sträckorna åt olika håll, eller
    ligger de bredvid varandra med ett mellanrum, är det flera rör som möts i punkten.
    """
    if not cs:
        return 0
    if paths is None:
        return 1
    lines: list[tuple[float, float]] = []
    loose = 0
    for c in cs:
        p = paths.get(c.pid)
        if p is None or c.seg_index >= len(p.segs):
            loose += 1
            continue
        sg = p.segs[c.seg_index]
        if sg.length < 1e-6:
            continue
        a = math.degrees(math.atan2(sg.y1 - sg.y0, sg.x1 - sg.x0)) % 180.0
        th = math.radians(a)
        off = (sg.x0 + sg.x1) / 2 * -math.sin(th) + (sg.y0 + sg.y1) / 2 * math.cos(th)
        deg = _R("semantics.attachment.COLLINEAR_DEG", COLLINEAR_DEG)
        gap = _R("semantics.attachment.COLLINEAR_OFF", COLLINEAR_OFF)
        if not any(min(abs(a - a2), 180.0 - abs(a - a2)) <= deg and abs(off - o2) <= gap
                   for a2, o2 in lines):
            lines.append((a, off))
    return (len(lines) + loose) or 1


def _bridge_spread(cs: list[Contact], paths: dict | None) -> int:
    """Antalet rör en SVAG brygga lämnar ifrån sig - 0 när kontakterna inte är en brygga alls.

    En `end`- eller `crossing_tick`-kontakt är ledaren själv: den slutar på röret, och det röret är det ritaren
    pekade ut. De svaga sorterna är bryggor - ledaren slutade i ett märke, en armatur, en symbol eller på en
    samlingslinje, och läsningen gick vidare därifrån. Där flera rör kopplas ihop i just den punkten lämnar
    bryggan dem allihop, och då är det rör-mot-rör-kontakten som ger namnet, inte hänvisningslinjen.
    """
    if not cs or not all(c.kind in WEAK_KINDS for c in cs):
        return 0
    n = _distinct_runs(cs, paths)
    # Två sträckor som möts i punkten är en böj. Ett rör som svänger är ett rör, och ritningen säger ingenting
    # annat: en vinkel med två armar ser likadan ut vare sig röret fortsätter runt hörnet eller två rör kopplas
    # ihop där. Tre eller fler armar är något annat - där grenar sig eller möts ledningar, och vilken av dem
    # etiketten menar går inte att läsa ur att de råkar röra varandra.
    return n if n >= 3 else 1


def _captured(cs: list[Contact]) -> dict:
    """En ledare som bara når sitt rör genom att fångas strax före det: konfidens NEAR_MISS_CONFIDENCE (h.1)."""
    if not cs or any(c.kind != "near_miss" for c in cs):
        return {}
    return {"leader_end_captured": True,
            "confidence": _R("semantics.attachment.NEAR_MISS_CONFIDENCE", NEAR_MISS_CONFIDENCE),
            "capture_distance_mm": round(min(c.distance for c in cs) * 25.4 / 72.0, 2)}


def resolve_block(block: AnnotationBlock, rows: list[Designation], ld: Leader, contacts: list[Contact],
                  system_tokens_in_drawing: set[str], spelled_out: frozenset[str] = frozenset(),
                  paths: dict | None = None, gidx: GeometryIndex | None = None) -> list[PipeCodeAnchor]:
    """Map designation rows of a block to contacted vector-family groups (bijection required)."""
    groups: dict[str, list[Contact]] = defaultdict(list)
    for c in contacts:
        groups[c.family].append(c)
    gkeys = sorted(groups)
    anchors: list[PipeCodeAnchor] = []

    def mk(d: Designation, state: str, reason: str, cs: list[Contact], extra: dict | None = None) -> PipeCodeAnchor:
        aid = stable_id("anc", d.page, d.did, ld.lid)
        return PipeCodeAnchor(anchor_id=aid, page=d.page, designation_id=d.did, designation=d.text,
                              designation_display=d.display_text, system_token=d.system_token,
                              dn=d.dn, multiplier=d.multiplier, block_id=block.bid, leader_id=ld.lid, leader_paths=ld.path_ids,
                              endpoint=ld.end, state=state, reason=reason, contacts=cs, candidate_families=gkeys,
                              evidence={"n_rows": len(rows), "n_groups": len(gkeys), "leader_family": ld.family,
                                        "row_index": d.row_index, **_captured(cs), **(extra or {})})

    if not gkeys:
        return [mk(d, "NO_PIPE_ATTACHMENT", "leader_endpoint_touches_no_pipe_geometry", []) for d in rows]
    # token matches
    # a row belongs to the group whose layer names its system most exactly: where the drawing gives a system a
    # layer of its own AND a shared one, the row is on the layer that names it, and the shared layer is left to
    # the rows that have nothing more exact - which is what tells two rows of one alpha family apart
    match: dict[str, list[str]] = {}
    ranks: dict[str, dict[str, int]] = {}
    for d in rows:
        rk = {g: system_layer_rank(d.system_token, g.split("|s|")[0], spelled_out) for g in gkeys}
        rk = {g: v[0] for g, v in rk.items() if v is not None}
        ranks[d.did] = rk
        match[d.did] = [g for g in gkeys if g in rk and rk[g] == min(rk.values())] if rk else []
    # En grupp som flera rader gör anspråk på: varmvattnets lager bär både varmvattnet och cirkulationen, och
    # blocket namnger båda. Ritar lagret lika många parallella linjer där som raderna är, är det en bunt inom
    # gruppen - raderna får sina linjer i väntan på att bladet avgör vilken som är vilken (elimineringen), som
    # varje annan bunt. Är linjerna färre delar raderna en linje och ingen får den; är de fler tar den rad
    # vars lager namnger den bäst gruppen, men bara om linjerna då räcker till en var - annars är det en fråga.
    within_group: dict[str, list] = {}
    if len(rows) > 1:
        for g in gkeys:
            claims = {d.did: ranks[d.did][g] for d in rows if g in match[d.did]}
            if len(claims) <= 1:
                continue
            lines = len({(c.pid, c.seg_index) for c in groups[g]})
            best = min(claims.values())
            best_rows = [did for did, v in claims.items() if v == best]
            if len(claims) == lines and paths is not None:
                runs = parallel_runs(groups[g], paths)
                if runs is not None and len(runs) == len(claims):
                    within_group[g] = runs
                    continue
            if len(best_rows) == 1 and lines == 1:
                for did, v in claims.items():
                    if v != best:
                        match[did] = [x for x in match[did] if x != g]
    def bridge_question(d: Designation, cs: list[Contact]) -> PipeCodeAnchor | None:
        """En ensam rad vars brygga lämnar flera rör är en fråga, inte ett svar.

        Ritaren drog en linje till ETT rör. Slutade linjen i en kopplingspunkt där flera rör möts kan
        läsningen inte veta vilket av dem raden menar - och att ta dem allihop är att låta rören namnge
        varandra. Kandidaterna står kvar i ankaret, så den som granskar, en lärdom eller läsarpanelen kan
        avgöra saken; det som inte får hända är att den avgörs tyst.
        """
        n = _bridge_spread(cs, paths)
        if n <= 1:
            return None
        return mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", "weak_bridge_reaches_several_pipes", cs,
                  {"bridge_kinds": sorted({c.kind for c in cs}), "n_runs": n})

    if len(rows) == 1:
        d = rows[0]
        # Each small circle whose centre the leader explicitly crosses is a
        # separate pointer. A single designation names all such pointers only
        # when each circle itself has an unambiguous pipe contact. Merely
        # touching a cluster of symbols is insufficient.
        pointers: dict[str, list[Contact]] = defaultdict(list)
        for c in contacts:
            if c.mark_id and c.mark_id.startswith('explicit_symbol:'):
                pointers[c.mark_id].append(c)
        if (len(pointers) >= 2 and sum(map(len, pointers.values())) == len(contacts)
                and all(len({c.family for c in cs}) == 1 and _bridge_spread(cs, paths) <= 1
                        for cs in pointers.values())
                and not any(_system_conflict(d, g, system_tokens_in_drawing, spelled_out) for g in gkeys)):
            return [mk(d, "VERIFIED_PIPE_ATTACHMENT", "single_row_explicit_symbol_pointers", contacts,
                       {"symbol_pointers": sorted(pointers)})]
        if len(gkeys) == 1:
            g = gkeys[0]
            conflict = _system_conflict(d, g, system_tokens_in_drawing, spelled_out)
            if conflict:
                return [mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", f"system_conflict:{conflict}", groups[g])]
            q = bridge_question(d, groups[g])
            if q is not None:
                return [q]
            return [mk(d, "VERIFIED_PIPE_ATTACHMENT", "single_row_single_group", groups[g], {"layer_token": match[d.did][0].split('|s|')[0] if match[d.did] else None})]
        if len(match[d.did]) == 1:
            g = match[d.did][0]
            q = bridge_question(d, groups[g])
            if q is not None:
                return [q]
            return [mk(d, "VERIFIED_PIPE_ATTACHMENT", "single_row_layer_token_match", groups[g], {"layer_token_match": g})]
        # A leader whose every contact is where its tick crosses a pipe line has said which pipes it means: all of
        # them, whatever pen each is drawn with.
        if (all(c.via == TICK_ACROSS for c in contacts)
                and not any(_system_conflict(d, g, system_tokens_in_drawing, spelled_out) for g in gkeys)):
            return [mk(d, "VERIFIED_PIPE_ATTACHMENT", "single_row_ticks_across_pipes", contacts)]
        return [mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", "several_vector_families_at_leader_no_token_discrimination", [c for g in gkeys for c in groups[g]])]
    # multi-row block: bijection through token matches; fallback: unique bijection by parallel-line count
    # (a 'k x' multiplier must equal the number of distinct parallel primitives of a group)
    owner: dict[str, list[str]] = defaultdict(list)
    for d in rows:
        for g in match[d.did]:
            owner[g].append(d.did)
    if all(not match[d.did] for d in rows) and len(gkeys) == len(rows):
        counts = {g: len({(c.pid, c.seg_index) for c in groups[g]}) for g in gkeys}
        by_count: dict[int, list[str]] = defaultdict(list)
        for g, n in counts.items():
            by_count[n].append(g)
        row_counts = Counter(d.multiplier for d in rows)
        if all(len(by_count.get(m, [])) == k for m, k in row_counts.items()) and sum(row_counts.values()) == len(gkeys):
            out = []
            for d in rows:
                if row_counts[d.multiplier] == 1:
                    g = by_count[d.multiplier][0]
                    out.append(mk(d, "VERIFIED_PIPE_ATTACHMENT", "multi_row_parallel_count_bijection", groups[g], {"count_match": d.multiplier}))
                else:
                    out.append(mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", "multi_row_equal_counts_no_discrimination", [c for g in by_count[d.multiplier] for c in groups[g]]))
            return out
    # Nothing in the layer names tells the rows apart, but the drawing may still: the runs of a bundle are
    # separate lines, and one of them is often named on its own somewhere else on the sheet. Hand the ordered
    # runs on and let the sheet settle it once ownership is known.
    #
    # The test is whether the layer names settle any row at all, not whether they say anything at all. It used
    # to be the second, and a single row whose system merely appeared in the shared layer's name was enough to
    # close this door on the whole block - so a stacked label over a bundle of drawn lines fell straight through
    # to "several codes on one run" and lost its metres with the lines sitting right there. It is not the first
    # either: a block where the layers do settle some rows keeps them, because a row the drawing has already
    # named is worth more than a place in an ordering.
    settles_nothing = not any(len(match[d.did]) == 1 and len(owner[match[d.did][0]]) == 1 for d in rows)
    if paths is not None and settles_nothing:
        runs = parallel_runs([c for g in gkeys for c in groups[g]], paths)
        if (runs is None or len(runs) != len(rows)) and gidx is not None:
            # linjen nådde färre rör än etiketten namnger: se efter om resten går parallellt bredvid
            runs = bundle_at([c for g in gkeys for c in groups[g]], gidx, len(rows), set(ld.path_ids), paths, set(gkeys)) or runs
        if runs is not None and len(runs) == len(rows):
            order = sorted(rows, key=lambda d: d.row_index)
            # Bär varje rad samma beteckning finns ingenting att avgöra: ritaren har skrivit ut att alla rören i
            # bunten är det röret. Det är fram och retur av samma värme- eller kylledning, skrivet två gånger
            # över paret, eller tre likadana rader över tre rör i ett stråk. Läsningen kallade det tvetydigt och
            # väntade på ett utpekande som aldrig kunde komma - det finns inget att peka ut när svaret är samma
            # oavsett vilken rad som gäller vilket rör. Tio meter stråk är då tjugo meter rör, som ritningen
            # säger, och inte noll.
            if len({(d.text or "").strip().upper() for d in order}) == 1:
                return [mk(d, "VERIFIED_PIPE_ATTACHMENT", "every_row_of_the_bundle_names_the_same_run", runs[i],
                           {"bundle": {"pos": i, "n": len(order), "all_rows_agree": True}})
                        for i, d in enumerate(order)]
            return [mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", "multi_row_bundle_awaiting_elimination",
                       [c for r in runs for c in r],
                       {"bundle": {"pos": i, "n": len(order),
                                   "runs": [[[c.pid, c.seg_index] for c in r] for r in runs]}})
                    for i, d in enumerate(order)]
    for d in rows:
        ms = match[d.did]
        if len(ms) == 1 and ms[0] in within_group:
            runs = within_group[ms[0]]
            order = sorted([r for r in rows if match[r.did] == [ms[0]]], key=lambda r: r.row_index)
            i = order.index(d) if d in order else 0
            anchors.append(mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", "multi_row_bundle_awaiting_elimination", [c for r in runs for c in r],
                              {"bundle": {"pos": i, "n": len(order), "runs": [[[c.pid, c.seg_index] for c in r] for r in runs], "within_layer": ms[0].split("|s|")[0]}}))
            continue
        if len(ms) == 1 and len(owner[ms[0]]) == 1:
            # samma fråga som för en ensam rad: lagret pekar ut gruppen, men gruppen kan vara en kopplingspunkt
            q = bridge_question(d, groups[ms[0]])
            anchors.append(q if q is not None else
                           mk(d, "VERIFIED_PIPE_ATTACHMENT", "multi_row_layer_token_bijection", groups[ms[0]], {"layer_token_match": ms[0]}))
        elif len(ms) == 0:
            # The label names more systems than the drawing draws lines here: several pipes drawn as one run and
            # named together. Their lengths cannot be split between the codes without inventing a rule, so the
            # run is not owned - but the contacts are recorded, so the case is visible and can be pointed at
            # rather than quietly costing the sheet its metres. An ambiguous anchor seeds no identity.
            shared = len(gkeys) < len(rows)
            anchors.append(mk(d, "AMBIGUOUS_PIPE_ATTACHMENT" if shared else "NO_PIPE_ATTACHMENT",
                              "multi_row_label_shares_one_run" if shared else "multi_row_no_compatible_layer_group",
                              [c for g in gkeys for c in groups[g]] if shared else [],
                              {"codes_sharing_the_run": sorted({r.display_text or r.text for r in rows})} if shared else None))
        else:
            anchors.append(mk(d, "AMBIGUOUS_PIPE_ATTACHMENT", "multi_row_token_match_not_unique", [c for g in ms for c in groups[g]]))
    return anchors


def _system_conflict(d: Designation, family: str, tokens: set[str], spelled_out: frozenset[str] = frozenset()) -> str | None:
    """A conflict exists when the layer carries a system token matching ANOTHER designation family but not this one."""
    layer = family.split("|s|")[0]
    # lagrets klass säger vilket system pennan ritar; en beteckning av en annan familj på den pennan är en
    # konflikt även om ett kortare tecken i namnet råkar passa (KV1 på ett varmvattenlager vars namn slutar på V1)
    m0 = re.match(r"([A-ZÅÄÖ]+)", d.system_token.upper())
    alpha = m0.group(1) if m0 else d.system_token.upper()
    for T in layer_tokens(layer):
        cls = _layer_class().get(T.upper().replace(".", ""))
        if cls and not alpha.startswith(cls):
            return f"layer_class_{T.upper()}_is_{cls}_not_{alpha}"
    if system_layer_match(d.system_token, layer, spelled_out):
        return None
    for t in sorted(tokens):
        if t != d.system_token and system_layer_match(t, layer, spelled_out):
            m = re.match(r"([A-ZÅÄÖ]+)", t.upper()); m2 = re.match(r"([A-ZÅÄÖ]+)", d.system_token.upper())
            if m and m2 and m.group(1) != m2.group(1):
                return f"layer_matches_{t}_not_{d.system_token}"
    return None
