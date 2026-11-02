"""Measurement and quantity aggregation. Meters only with verified scale; vertical only with explicit evidence."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import math

from ..pipes.ownership import OwnershipResult, PhysicalPipe, DECLARED_REASON
from ..pipes.confidence import tier, REVIEW, INFERRED
from .scale import ScaleResult

# A pipe of some size is drawn as two lines - its two edges - a few points apart, and a label with a tick on
# each edge names them both. Counting both edges is counting the pipe twice. Two runs of one identity and one
# pen that lie side by side, this close, for most of the shorter one's length, are one pipe: the longer edge
# carries the metres, the other is its second edge and carries none. Two pipes of the same name that only run
# side by side for a stretch stay two pipes.
DOUBLE_LINE_MAX = 8.0       # pt: never further apart than this, whatever the size
DOUBLE_LINE_MIN = 1.5       # pt: never closer than the pen itself allows
DOUBLE_LINE_FACTOR = 1.6    # the edges lie the pipe's outer diameter apart, at the sheet's scale, give or take
DOUBLE_LINE_SHARE = 0.6     # share of the shorter run that has to lie alongside the longer
DOUBLE_LINE_DRAWABLE = 2.5  # pt: a pipe narrower than this on the sheet (0.9 mm) is never drawn as two lines
DUPLICATE_LINE_MAX = 0.75   # pt: lines this close are one line drawn twice
EDGES_PARALLEL = 2.0        # degrees: the two edges of one pipe drawn in one stroke run the same way
TWIN_REASON = "second_edge_of_a_double_line"
# Outer diameter in mm for a nominal size - the distance the two drawn edges of a double-line pipe lie apart.
# A DN16 pipe is one point wide at 1:50: it cannot be drawn as two lines, and two DN16 lines a few points
# apart are two pipes - the connection pipes from a distributor run in bundles like that, and they are all real.
DN_TO_DY_MM = {10: 12.0, 12: 15.0, 15: 18.0, 16: 18.0, 20: 22.0, 22: 22.0, 25: 28.0, 28: 28.0, 32: 35.0, 35: 35.0,
               40: 42.0, 42: 42.0, 50: 54.0, 54: 54.0, 65: 76.1, 80: 88.9, 100: 114.3, 125: 139.7, 150: 168.3,
               200: 219.1}


def double_line_gap(dn: int | None, mpp: float | None) -> float | None:
    """How far apart the two edges of a pipe of this size lie on this sheet - or None when the size or the
    scale is unknown, in which case nothing is folded."""
    if dn is None or not mpp:
        return None
    dy_pt = (DN_TO_DY_MM.get(int(dn), float(dn)) / 1000.0) / mpp
    if dy_pt < _R("measure.measure.DOUBLE_LINE_DRAWABLE", DOUBLE_LINE_DRAWABLE):
        # Too thin on this sheet to be drawn as two lines: what lies alongside is either the same line drawn
        # twice, on top of itself, or another pipe. Measured on eleven reference sheets the folded pairs of such
        # sizes were coincident (0-0.5 pt apart) wherever the reference drew one line for both, and 1.3-3.6 pt
        # apart wherever it measured two pipes - 436 pt of real pipe the old 1.5 pt floor had folded away.
        return _R("measure.measure.DUPLICATE_LINE_MAX", DUPLICATE_LINE_MAX)
    # A duct's two edges lie its own width apart, wider than any pipe's: in the widened reading of "Alla" the run's
    # own size decides how far apart its edges may lie, and the cap for pipes does not hold there
    from .. import disciplines
    if disciplines.value("measure.DOUBLE_LINE_BY_SIZE_ONLY", False):
        return max(DOUBLE_LINE_MIN, DOUBLE_LINE_FACTOR * dy_pt)
    return min(_R("measure.measure.DOUBLE_LINE_MAX", DOUBLE_LINE_MAX), max(DOUBLE_LINE_MIN, DOUBLE_LINE_FACTOR * dy_pt))


def _R(rule_id: str, default: float) -> float:
    try:
        from ..rules import value
        return float(value(rule_id, default))
    except Exception:
        return default


def _segments(p: PhysicalPipe) -> list[tuple[float, float, float, float]]:
    out = []
    for poly in p.points or []:
        for (x0, y0), (x1, y1) in zip(poly, poly[1:]):
            if (x1 - x0) ** 2 + (y1 - y0) ** 2 > 1e-6:
                out.append((x0, y0, x1, y1))
    return out


def _overlap_pt(seg, others, dmax: float) -> float:
    """Hur långt av segmentet som verkligen går bredvid den andra sträckan, mätt i punkter.

    Provet var förut "ligger segmentets mittpunkt nära?", och svaret på det beror på var segmentgränserna
    råkar hamna: samma sträcka uppdelad i mindre bitar gav ett annat svar. Här klipps segmentet i stället mot
    varje granne, och de klippta bitarna slås ihop till en union - så två ritningar med samma geometri men
    olika uppdelning ger samma längd.

    Bara det som är nästan parallellt räknas: en linje som korsar en annan går bredvid den i en punkt, inte i
    en sträcka, och en dubbellinjes två kanter följs åt.
    """
    x0, y0, x1, y1 = seg
    L = math.hypot(x1 - x0, y1 - y0)
    if L <= 1e-9:
        return 0.0
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    ang = math.atan2(y1 - y0, x1 - x0)
    spans: list[tuple[float, float]] = []
    for ox0, oy0, ox1, oy1 in others:
        dx, dy = ox1 - ox0, oy1 - oy0
        ol = math.hypot(dx, dy)
        if ol <= 1e-9:
            continue
        da = abs(ang - math.atan2(dy, dx)) % math.pi
        if min(da, math.pi - da) > math.radians(4.0):
            continue
        # grannens ändar projicerade på det här segmentets riktning, och deras avstånd i sidled
        t0 = (ox0 - x0) * ux + (oy0 - y0) * uy
        t1 = (ox1 - x0) * ux + (oy1 - y0) * uy
        n0 = abs(-(ox0 - x0) * uy + (oy0 - y0) * ux)
        n1 = abs(-(ox1 - x0) * uy + (oy1 - y0) * ux)
        if min(n0, n1) > dmax:
            continue                     # hela grannen ligger för långt åt sidan
        lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        lo, hi = max(0.0, lo), min(L, hi)
        if hi - lo > 1e-9:
            spans.append((lo, hi))
    if not spans:
        return 0.0
    spans.sort()
    total, cur_lo, cur_hi = 0.0, spans[0][0], spans[0][1]
    for lo, hi in spans[1:]:
        if lo > cur_hi:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    return total + (cur_hi - cur_lo)


def twin_edges(pipes: list[PhysicalPipe], mpp: float | None = None) -> dict[str, str]:
    """physical_pipe_id -> the pipe it is the second edge of. Se `twin_overlap_pt`; den här formen bär bara
    vem som är vems andra kant, för den som inte behöver längden."""
    return {pid: partner for pid, (partner, _) in twin_overlap_pt(pipes, mpp).items()}


def twin_overlap_pt(pipes: list[PhysicalPipe], mpp: float | None = None) -> dict[str, tuple[str, float]]:
    """physical_pipe_id -> (röret det är andra kanten av, hur många punkter som verkligen går bredvid).

    Sträckor jämförs inom en penna och en identitet, på det avstånd ett rör av den storleken har mellan sina
    kanter på det här bladet; den längre sträckan behåller metrarna. En sträcka som är någons andra kant är
    aldrig någon annans första.

    Längden följer med, för den behövs: två rör som följs åt en bit och sedan går skilda vägar är två rör, och
    bara den gemensamma biten är ritad två gånger. Utan längden lämnade den kortare ifrån sig allt den hade,
    och den fria delen försvann ur mängden utan att synas någonstans.
    """
    share = DOUBLE_LINE_SHARE
    groups: dict[tuple[str, str], list[PhysicalPipe]] = defaultdict(list)
    for p in pipes:
        groups[(p.family, p.identity.key)].append(p)
    out: dict[str, tuple[str, float]] = {}
    for key, grp in groups.items():
        if len(grp) < 2:
            continue
        dmax = double_line_gap(grp[0].identity.dn, mpp)
        if dmax is None:
            continue
        segs = {p.physical_pipe_id: _segments(p) for p in grp}
        length = {p.physical_pipe_id: sum(math.hypot(x1 - x0, y1 - y0) for x0, y0, x1, y1 in segs[p.physical_pipe_id]) for p in grp}
        boxes = {}
        for p in grp:
            ss = segs[p.physical_pipe_id]
            if ss:
                boxes[p.physical_pipe_id] = (min(min(a[0], a[2]) for a in ss), min(min(a[1], a[3]) for a in ss),
                                             max(max(a[0], a[2]) for a in ss), max(max(a[1], a[3]) for a in ss))
        by_len = sorted(grp, key=lambda p: (-length[p.physical_pipe_id], p.physical_pipe_id))
        for i, b in enumerate(by_len):
            bid = b.physical_pipe_id
            if bid in out or not segs[bid] or length[bid] <= 0:
                continue
            for a in by_len[:i]:
                aid = a.physical_pipe_id
                if aid in out or aid not in boxes or bid not in boxes:
                    continue
                A, B = boxes[aid], boxes[bid]
                if A[0] - dmax > B[2] or A[2] + dmax < B[0] or A[1] - dmax > B[3] or A[3] + dmax < B[1]:
                    continue
                beside = sum(_overlap_pt(sg, segs[aid], dmax) for sg in segs[bid])
                if beside >= share * length[bid]:
                    out[bid] = (aid, min(beside, length[bid]))
                    break
    return out


def own_second_edge_pt(p: PhysicalPipe, mpp: float | None) -> float:
    """Hur många punkter av en sträcka som är dess egen andra kant.

    Ett rör ritat i skala ritas som sina två kanter, och ofta i ett enda streck: längs den ena kanten, runt
    änden och tillbaka längs den andra. Då är båda kanterna samma sträcka, och jämförelsen mellan sträckor
    (twin_overlap_pt) ser dem aldrig - ett sådant rör mättes två gånger. Det som går bredvid sträckan själv, på
    det avstånd ett rör av den storleken har mellan sina kanter, går bredvid parvis: hälften är den andra
    kanten. En sträcka som inte vänder tillbaka längs sig själv har ingen sådan del, och mäts som förut."""
    dmax = double_line_gap(p.identity.dn, mpp)
    if dmax is None:
        return 0.0
    segs = _segments(p)
    if len(segs) < 2:
        return 0.0
    cell = max(32.0, 4.0 * dmax)

    def cells(x0, y0, x1, y1):
        for gx in range(int((min(x0, x1) - dmax) // cell), int((max(x0, x1) + dmax) // cell) + 1):
            for gy in range(int((min(y0, y1) - dmax) // cell), int((max(y0, y1) + dmax) // cell) + 1):
                yield gx, gy
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, sg in enumerate(segs):
        for c in cells(*sg):
            grid[c].append(i)
    beside = 0.0
    for i, sg in enumerate(segs):
        near: set[int] = set()
        for c in cells(*sg):
            near.update(grid.get(c, ()))
        near.discard(i)
        beside += _within_pt(sg, [segs[j] for j in sorted(near)], dmax)
    return beside / 2.0


def _within_pt(seg, others, dmax: float) -> float:
    """Hur långt av segmentet som har en annan sträcka inom dmax i sidled, just där.

    Två grenar som lämnar samma nod med några graders vinkel ligger intill varandra vid noden och isär längre ner.
    Bara den del där avståndet verkligen är ett rörs bredd eller mindre går bredvid - inte hela längden för att
    ena änden råkar ligga nära."""
    x0, y0, x1, y1 = seg
    L = math.hypot(x1 - x0, y1 - y0)
    if L <= 1e-9:
        return 0.0
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    ang = math.atan2(y1 - y0, x1 - x0)
    spans: list[tuple[float, float]] = []
    for ox0, oy0, ox1, oy1 in others:
        if math.hypot(ox1 - ox0, oy1 - oy0) <= 1e-9:
            continue
        da = abs(ang - math.atan2(oy1 - oy0, ox1 - ox0)) % math.pi
        if min(da, math.pi - da) > math.radians(EDGES_PARALLEL):
            continue                         # a pipe's two edges run the same way; a branch leaves at an angle
        t0 = (ox0 - x0) * ux + (oy0 - y0) * uy
        t1 = (ox1 - x0) * ux + (oy1 - y0) * uy
        n0 = -(ox0 - x0) * uy + (oy0 - y0) * ux
        n1 = -(ox1 - x0) * uy + (oy1 - y0) * ux
        if abs(t1 - t0) <= 1e-9:
            continue
        lo, hi = min(t0, t1), max(t0, t1)
        k = (n1 - n0) / (t1 - t0)            # how the distance across changes along the segment
        if abs(k) <= 1e-12:
            if abs(n0) > dmax:
                continue
        else:
            ta, tb = t0 + (-dmax - n0) / k, t0 + (dmax - n0) / k
            lo, hi = max(lo, min(ta, tb)), min(hi, max(ta, tb))
        lo, hi = max(0.0, lo), min(L, hi)
        if hi - lo > 1e-9:
            spans.append((lo, hi))
    if not spans:
        return 0.0
    spans.sort()
    total, cur_lo, cur_hi = 0.0, spans[0][0], spans[0][1]
    for lo, hi in spans[1:]:
        if lo > cur_hi:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    return total + (cur_hi - cur_lo)


@dataclass
class PipeMeasure:
    pipe: PhysicalPipe
    horizontal_pdf_units: float
    horizontal_m: float | None
    vertical_m: float | None
    vertical_evidence: dict | None
    total_m: float | None
    state: str
    reasons: list[str] = field(default_factory=list)
    hatched_pdf_units: float = 0.0
    hatched_m: float | None = None      # part of the horizontal length running inside a hatched area
    twin_of: str | None = None          # the run this one is the second drawn edge of: its metres are counted there
    twin_pdf_units: float = 0.0
    own_edge_pdf_units: float = 0.0     # the run's own second edge, drawn in the same stroke: counted once


# Vad bladet självt har avgjort om sin storlek. En meter mätt under något annat är ett förslag: den redovisas
# med sitt tal, för den som granskar behöver se vad stocken gav, men den får inte heta bekräftad. Skillnaden
# mellan de två är hela skillnaden mellan ett mått och en gissning på tusen gånger fel.
SETTLED_SCALE = ("VERIFIED", "TEXT_ONLY", "BAR_ONLY", "DIMENSIONS_ONLY")
# Avgjord är inte bekräftad. Bara VERIFIED - bladets påstådda skala bekräftad av en oberoende mätning på bladet
# inom 1 % - ger raden CONFIRMED. En skala som bladet själv säger men ingenting mätt (TEXT_ONLY), eller som bara
# mätts på ett sätt (BAR_ONLY, DIMENSIONS_ONLY), ger mätta meter med raden SCALE_UNVERIFIED.
UNVERIFIED_ROW_STATE = "SCALE_UNVERIFIED"
UNSETTLED_ROW_STATE = {"CONFLICT": "SCALE_UNSETTLED", "FROM_THE_SET": "SCALE_FROM_THE_SET",
                       "GIVEN_BY_HAND": "SCALE_GIVEN_BY_HAND", "FROM_PIPES": "SCALE_FROM_PIPES",
                       "TEXT_ONLY": UNVERIFIED_ROW_STATE, "BAR_ONLY": UNVERIFIED_ROW_STATE,
                       "DIMENSIONS_ONLY": UNVERIFIED_ROW_STATE}
HATCHED_ONLY = "IN_HATCHED_AREA"        # hela stråket ligger inne i en skraffering: ritat, men inte det här bladets
FROM_IMAGE = "READ_FROM_IMAGE"          # mätt på ett blad läst ur en bild: uppmätt, men bekräftad först av en person


def scale_standing(scale: ScaleResult) -> str | None:
    """Radens tillstånd när skalan inte är verifierad - annars None, och raden får heta bekräftad."""
    if scale.state == "VERIFIED" or scale.meters_per_pt is None:
        return None
    return UNSETTLED_ROW_STATE.get(scale.state)


def measure_pipes(own: OwnershipResult, scale: ScaleResult, elevations: dict[str, list[dict]],
                  hatched_pt: dict[str, float] | None = None, from_image: bool = False) -> list[PipeMeasure]:
    """elevations: anchor_id -> list of {tag, value} elevation annotations attached to the anchor's label unit.
    hatched_pt: physical_pipe_id -> length (pdf units) of the pipe inside hatched areas.
    from_image: the sheet was read from pixels (raster/), so what would be CONFIRMED is READ_FROM_IMAGE."""
    out: list[PipeMeasure] = []
    mpp = scale.meters_per_pt if scale.state in ("VERIFIED", "TEXT_ONLY", "BAR_ONLY", "CONFLICT", "FROM_THE_SET",
                                                "GIVEN_BY_HAND", "DIMENSIONS_ONLY", "FROM_PIPES") and scale.meters_per_pt else None
    # A sheet whose scale evidence disagrees still gets measured - the geometric bar is the better witness and
    # the reason for choosing it is recorded - but every metre that comes out of it carries the conflict, so no
    # single run can be read as confidently measured when the sheet's own scale is unsettled.
    scale_note = (f"scale_{scale.state.lower()}:{scale.reason}"
                  if scale.state not in ("VERIFIED",) and mpp is not None else None)
    standing = scale_standing(scale)
    twins = twin_overlap_pt(own.pipes, mpp)
    for p in own.pipes:
        # hatched length is measured on drawn primitives; scale it by the bridged-gap share of the run
        factor = (p.length_pt / p.raw_length_pt) if p.raw_length_pt > 0 else 1.0
        hpt = min((hatched_pt or {}).get(p.physical_pipe_id, 0.0) * factor, p.length_pt)
        hpu = p.length_pt - hpt          # horizontal quantity = drawn length outside hatched (wall) areas
        twin = twins.get(p.physical_pipe_id)
        twin_of = twin[0] if twin else None
        twin_pu = 0.0
        if twin is not None:
            # Den andra kanten av en dubbellinje lämnar sina meter till den första - men bara den del som
            # verkligen går bredvid. Det som går sin egen väg är ett eget rör och stannar i mängden.
            twin_pu = min(hpu, twin[1] * factor)
            hpu -= twin_pu
        own_pu = 0.0
        if twin is None:
            # ...och ett rör vars två kanter är samma streck lämnar den ena kantens meter till den andra.
            own_pu = min(hpu, own_second_edge_pt(p, mpp) * factor)
            hpu -= own_pu
        hm = hpu * mpp if mpp is not None else None
        reasons = []
        if twin_of is not None:
            reasons.append(f"{TWIN_REASON}:{twin_of}")
        elif own_pu > 0.0:
            reasons.append(f"{TWIN_REASON}:{p.physical_pipe_id}")
        if mpp is None:
            reasons.append("no_verified_scale")
        elif scale_note:
            reasons.append(scale_note)
        vert, vev = _vertical(p, elevations)
        total = (hm + (vert or 0.0)) if hm is not None else None
        # Utan skala finns ingen meter. Raden hette förut EJ STÖDD STIL, vilket pekade på fel sak: stilen gick
        # att läsa, det var storleken bladet inte sa. NO_SCALE säger vad som saknas, och därmed vad som behövs.
        # Ett stråk som ritats helt inne i en skraffering är inte det här bladets. Skrafferingen är hur ritningen
        # säger att en del inte redovisas här - en angränsande byggnadsdel, ett annat entreprenadskede - och den
        # som mängdar mäter inte i den: på de fem blad där mängdarens egna mätlinjer finns ligger 0,1-1,2 procent
        # av hennes meter inne i skraffering. Raden ska då inte stå som bekräftad med noll meter, som om röret
        # letats efter och inte funnits. Den ska säga var metrarna tog vägen.
        state = ("NO_SCALE" if hm is None else
                 HATCHED_ONLY if hpu <= 0.0 < p.length_pt else
                 standing or (FROM_IMAGE if from_image else "CONFIRMED"))
        if p.frontier_reasons:
            reasons.extend(p.frontier_reasons)
        out.append(PipeMeasure(pipe=p, horizontal_pdf_units=hpu, horizontal_m=hm, vertical_m=vert, vertical_evidence=vev,
                               total_m=total, state=state, reasons=reasons, hatched_pdf_units=hpt,
                               hatched_m=(hpt * mpp if mpp is not None else None),
                               twin_of=twin_of, twin_pdf_units=twin_pu, own_edge_pdf_units=own_pu))
    return out


def _vertical(p: PhysicalPipe, elevations: dict[str, list[dict]]):
    """Vertical length only from explicit evidence: two elevation annotations with the same tag on the pipe's
    supporting anchors (top/bottom levels). Otherwise UNKNOWN (None).

    The unit has to come from the drawing too. A level written with a decimal separator is metres, a whole
    thousand or more is millimetres, and a bare small integer says neither - reading that as metres turns two
    levels 50 mm apart into fifty metres of pipe. A tag whose levels do not agree on one unit is not evidence,
    so the vertical stays UNKNOWN rather than being guessed at either scale.

    What this reads is the span between the highest and lowest level on the run. A pipe that goes down and up
    again passes the same levels twice and its true vertical length is larger; nothing in two levels says that
    it did, so the span is what can be defended and the rest is not claimed.
    """
    vals: dict[str, list[tuple[str, float, str | None]]] = defaultdict(list)
    for aid in (p.elevation_anchor_ids if p.elevation_anchor_ids is not None else p.anchor_ids):
        for e in elevations.get(aid, []):
            vals[e["tag"]].append((aid, e["value"], e.get("unit")))
    for tag, lst in sorted(vals.items()):
        uniq = sorted({v for _, v, _ in lst})
        if len(uniq) < 2:
            continue
        units = {u for _, _, u in lst}
        if len(units) != 1 or None in units:
            continue                  # the sheet did not say what these numbers are; no vertical is claimed
        unit = units.pop()
        diff = (max(uniq) - min(uniq)) / (1000.0 if unit == "mm" else 1.0)
        return round(diff, 3), {"kind": "elevation_difference_between_anchors", "tag": tag, "unit": unit,
                                "values": uniq, "anchors": sorted({a for a, _, _ in lst})}
    return None, None


def aggregate(measures: list[PipeMeasure], ambiguous_pt: dict[str, float], mpp: float | None,
              risers: dict[str, list[dict]] | None = None, label_counts: dict[str, int] | None = None,
              label_risers: dict[str, list[dict]] | None = None) -> list[dict[str, Any]]:
    """label_counts: identity key -> number of verified labels on the drawing, which is what a reader counts;
    physical_pipe_count is the number of connected runs the network resolves into, which is usually smaller."""
    rows: dict[str, dict[str, Any]] = {}
    for m in measures:
        k = m.pipe.identity.key
        r = rows.setdefault(k, {"designation": m.pipe.identity.display, "base": m.pipe.identity.base, "dn": m.pipe.identity.dn, "system": m.pipe.identity.system,
                                "physical_pipe_count": 0, "confirmed_horizontal_m": 0.0, "confirmed_vertical_m": 0.0,
                                "confirmed_total_m": 0.0, "horizontal_pdf_units": 0.0, "ambiguous_m": 0.0, "review_m": 0.0, "vertical_known": False,
                                "in_hatched_area_m": 0.0, "declared_m": 0.0, "double_line_m": 0.0, "state": "CONFIRMED", "pipe_ids": []})
        if m.twin_of is not None:
            # the second edge of a double line: its metres are the partner's, and it is not another run
            r["double_line_m"] += m.twin_pdf_units * mpp if mpp else 0.0
            r["pipe_ids"].append(m.pipe.physical_pipe_id)
            continue
        r["physical_pipe_count"] += 1
        r["double_line_m"] += getattr(m, "own_edge_pdf_units", 0.0) * mpp if mpp else 0.0
        if m.horizontal_m is not None and DECLARED_REASON in (m.pipe.evidence or []):
            r["declared_m"] += m.horizontal_m       # named by the sheet's written rule, not by a label
        r["horizontal_pdf_units"] += m.horizontal_pdf_units
        r["pipe_ids"].append(m.pipe.physical_pipe_id)
        if m.horizontal_m is not None:
            r["confirmed_horizontal_m"] += m.horizontal_m
            r["confirmed_total_m"] += m.horizontal_m
            level = tier(m.pipe.evidence, bool(getattr(m.pipe, "needs_review", False)), getattr(m.pipe, "flags", ()))
            if level == REVIEW:
                r["review_m"] += m.horizontal_m     # counted, and marked: the best reading, not a confirmed one
            elif level == INFERRED:
                r["inferred_m"] = r.get("inferred_m", 0.0) + m.horizontal_m   # named by the drawing's logic
            r["in_hatched_area_m"] += m.hatched_m or 0.0      # excluded from the horizontal quantity
            r["hatched_only_pipes"] = r.get("hatched_only_pipes", 0) + (1 if m.state == HATCHED_ONLY else 0)
            if (m.state in UNSETTLED_ROW_STATE.values() or m.state == FROM_IMAGE) and r["state"] == "CONFIRMED":
                r["state"] = m.state          # mätt under en skala bladet inte avgjort: ett förslag, inte ett besked
        else:
            r["state"] = "NO_SCALE"
        if m.vertical_m is not None:
            r["confirmed_vertical_m"] += m.vertical_m
            r["confirmed_total_m"] += m.vertical_m
            r["vertical_known"] = True
    for k, pt in ambiguous_pt.items():
        r = rows.setdefault(k, {"designation": k.split("|DN")[0], "base": k.split("|DN")[0], "dn": _dn_from_key(k), "system": "", "physical_pipe_count": 0,
                                "confirmed_horizontal_m": 0.0, "confirmed_vertical_m": 0.0, "confirmed_total_m": 0.0,
                                "horizontal_pdf_units": 0.0, "ambiguous_m": 0.0, "review_m": 0.0, "vertical_known": False, "state": "AMBIGUOUS", "pipe_ids": []})
        r["ambiguous_m"] += pt * mpp if mpp else 0.0
        # geometry the reading could not give to anyone exists whether or not there is a scale to measure it in;
        # tracking it in the drawing's own units keeps a scaleless sheet from reporting the ambiguity away
        r["ambiguous_pdf_units"] = r.get("ambiguous_pdf_units", 0.0) + pt
        if r["physical_pipe_count"] == 0:
            r["state"] = "AMBIGUOUS"
    for k, n in (label_counts or {}).items():
        if k in rows:
            rows[k]["label_count"] = n
    for r in rows.values():
        r.setdefault("label_count", 0)          # a declared run has metres and no label: zero, not missing
    # Both riser readings make a row of their own. A designation the sheet writes only over a riser - a stack
    # that drops away out of the plan, drawn as a point and named with its dimension on the row below - has no
    # horizontal run to be aggregated from, and left to the measured rows alone it would not be reported at all.
    for k, lst in (label_risers or {}).items():
        r = rows.setdefault(k, _empty_row(k))
        r["riser_count_from_labels"] = len(lst)
    for k, lst in (risers or {}).items():
        r = rows.setdefault(k, _empty_row(k))
        r["riser_count"] = len(lst)
    _fold_vent_rows(rows)
    out = []
    for k in sorted(rows):
        r = rows[k]
        r.setdefault("riser_count", 0)
        r.setdefault("riser_count_from_labels", len((label_risers or {}).get(k, [])))
        r.setdefault("label_count", (label_counts or {}).get(k, 0))
        r.setdefault("in_hatched_area_m", 0.0)
        r.setdefault("double_line_m", 0.0)
        r.setdefault("ambiguous_pdf_units", 0.0)
        r.setdefault("review_m", 0.0)
        r.setdefault("inferred_m", 0.0)
        if r["physical_pipe_count"] == 0 and r["ambiguous_pdf_units"] == 0 \
                and max(r["riser_count"], r["riser_count_from_labels"]) > 0:
            r["state"] = "RISER_LABELS_ONLY"
        # ...och en rad vars alla stråk ligger inne i skrafferingen säger det, i stället för att visa en nolla
        if r["state"] == "CONFIRMED" and r["physical_pipe_count"] > 0 \
                and r.pop("hatched_only_pipes", 0) == r["physical_pipe_count"]:
            r["state"] = HATCHED_ONLY
        r.pop("hatched_only_pipes", None)
        for f in ("confirmed_horizontal_m", "confirmed_vertical_m", "confirmed_total_m", "ambiguous_m",
                  "horizontal_pdf_units", "in_hatched_area_m", "ambiguous_pdf_units", "double_line_m", "review_m",
                  "inferred_m"):
            r[f] = round(r[f], 3)
        r["vertical_m"] = r["confirmed_vertical_m"] if r["vertical_known"] else "UNKNOWN"
        h = slab_riser_height(r)
        if h is not None:
            r["riser_height_m"] = h      # what one riser counts on this row; absent means the storey height
        # how many of the row's risers go through the slab (a metre each) rather than a storey: the default is the
        # row's own kind, and the reader splits a row where the sheet has both
        r["riser_slab_count"] = max(r["riser_count"], r["riser_count_from_labels"]) if h is not None else 0
        out.append(r)
    return out


VENT_MARK = "-(L)"
VENT_SUMS = ("physical_pipe_count", "confirmed_horizontal_m", "confirmed_vertical_m", "confirmed_total_m", "horizontal_pdf_units",
             "ambiguous_m", "review_m", "inferred_m", "in_hatched_area_m", "declared_m", "double_line_m", "ambiguous_pdf_units")


def _fold_vent_rows(rows: dict[str, dict[str, Any]]) -> None:
    """A vent written on the pipe it vents - `S1-P2 / 75(L)`, `S2-P5-110 (L)`, `S3-R8-110L` - is that pipe in the takeoff.

    The reading keeps the vent apart while it names pipes, so that the mark is not carried onto every pipe of the
    size. In the quantity it is not a system of its own: no reference takeoff of the eleven measured sheets has a
    vent row, and the metres a vent row held belong to the designation it is written on. They are added there. Its
    labels and risers are not: the vent stack is counted by the sheet's own riser marks, and adding the vent label
    as one more riser put a vertical too many on the row it joined.
    """
    for k in [k for k in rows if k.split("|DN")[0].endswith(VENT_MARK)]:
        r = rows.pop(k)
        base = k.split("|DN")[0][: -len(VENT_MARK)]
        if not base:
            rows[k] = r
            continue
        target = f"{base}|DN{k.split('|DN')[-1]}"
        t = rows.get(target)
        if t is None:
            if not r.get("physical_pipe_count") and not r.get("ambiguous_pdf_units"):
                continue                                    # nothing measured: a vent label alone is no row
            t = rows[target] = {**r, "designation": f"{base}-{r.get('dn')}", "base": base,
                                "label_count": 0, "riser_count": 0, "riser_count_from_labels": 0}
            continue
        for f in VENT_SUMS:
            if r.get(f):
                t[f] = t.get(f, 0) + r[f]
        t["pipe_ids"] = list(t.get("pipe_ids", [])) + list(r.get("pipe_ids", []))
        t["vertical_known"] = bool(t.get("vertical_known") or r.get("vertical_known"))


SLAB_RISER_M = 1.0         # m: a radiator connection rises through the slab to the radiator, not a storey
SLAB_RISER_SYSTEM = "VS"    # heating (värmesystem)
SLAB_RISER_MAX_DN = 15      # the connection sizes; a heating stack is larger
SLAB_RISER_MATERIAL = "S"   # thin steel tube (S13 and kin); a copper heating riser is a full storey on the sheets


def slab_riser_height(row: dict[str, Any]) -> float | None:
    """The height one riser on this row stands for when the sheet does not say, or None for a storey.

    A riser symbol on a heating pipe of connection size is the branch going up through the slab to a radiator on
    the floor above (or down to one below): about a metre of pipe, not a storey. The takeoffs count it as
    "upp genom bjälklag" 1.0 m; counting it as a storey put 1.8 m too many on every radiator."""
    base = str(row.get("base") or row.get("designation") or "").upper()
    parts = base.split("-")
    if len(parts) < 2:
        return None
    system = "".join(c for c in parts[0] if c.isalpha())
    dn = row.get("dn")
    from .. import disciplines
    if system == disciplines.value("measure.SLAB_RISER_SYSTEM", SLAB_RISER_SYSTEM) \
            and parts[1].startswith(disciplines.value("measure.SLAB_RISER_MATERIAL", SLAB_RISER_MATERIAL)) \
            and isinstance(dn, (int, float)) \
            and 0 < dn <= SLAB_RISER_MAX_DN:
        return SLAB_RISER_M
    return None


def _empty_row(k: str) -> dict[str, Any]:
    """A quantity row for a designation with nothing measured under it yet."""
    return {"designation": k.split("|DN")[0], "base": k.split("|DN")[0], "dn": _dn_from_key(k), "system": "",
            "physical_pipe_count": 0, "confirmed_horizontal_m": 0.0, "confirmed_vertical_m": 0.0,
            "confirmed_total_m": 0.0, "horizontal_pdf_units": 0.0, "ambiguous_m": 0.0, "review_m": 0.0, "vertical_known": False,
            "in_hatched_area_m": 0.0, "state": "CONFIRMED", "pipe_ids": []}


def _dn_from_key(k: str):
    t = k.split("|DN")[-1]
    return int(t) if t.isdigit() else None
