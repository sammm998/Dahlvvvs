"""Rörbeteckningar utan ledare: närmaste parallella rör (VS-Kalkyl SPEC 6.4.3 h.2).

En ritare skriver ofta etiketten direkt längs röret, utan ledare – särskilt på långa ledningar i mark, som ritas
streckade. Har en rörbeteckning ingen ledare alls tilldelas den det närmaste rörsegmentet som är parallellt med
textens riktning, inom LEADERLESS_MM på papper. Ligger ett annat rör (en annan sammanhängande sträcka, även över
streckens pauser) högst AMBIGUITY_RATIO gånger så långt bort som det närmaste är tilldelningen tvetydig, och
ingen meter bekräftas.

Ett ankare härifrån bär `evidence["proximity_without_leader"]` och `evidence["confidence"]`; röret det namnger
är aldrig "säkert" (pipes/confidence.py), bara härlett.
"""
from __future__ import annotations

import math
from collections import defaultdict

from ..geometry.core import Seg, point_seg_distance, stable_id
from ..rules import value as _rule
from .attachment import Contact, PipeCodeAnchor

PT_PER_MM = 72.0 / 25.4
LEADERLESS_MM = 6.0           # hur långt från etiketten ett rör får ligga, på papper
PARALLEL_DEG = 10.0           # hur mycket röret får avvika från textens riktning och ändå vara parallellt
AMBIGUITY_RATIO = 1.5         # ett andra rör högst så här många gånger så långt bort gör tilldelningen tvetydig
CONFIDENCE = 0.8              # tilldelningens konfidens (ledare: 0,95)


def _R(rule_id: str, default):
    return _rule(rule_id, default)


def _components(graph) -> dict[int, int]:
    """Sammanhängande sträckor i en familjs graf: rörbitar som delar nod, och streck som grafen överbryggat."""
    parent: dict[int, int] = {}

    def find(n: int) -> int:
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for na, nb in graph.prim_nodes.values():
        union(na, nb)
    for br in graph.bridges:
        if "from_node" in br and "to_node" in br:
            union(br["from_node"], br["to_node"])
    return {pid: find(graph.prim_nodes[pid][0]) for pid in graph.prims}


def _parallel(seg: Seg, angle_deg: float, tol_deg: float) -> bool:
    a = math.degrees(math.atan2(seg.y1 - seg.y0, seg.x1 - seg.x0))
    diff = abs(((a - angle_deg) + 90.0) % 180.0 - 90.0)
    return diff <= tol_deg


def _box_seg_distance(box, seg: Seg) -> tuple[float, tuple[float, float]]:
    """Kortaste avstånd mellan etikettens låda och segmentet, och punkten på segmentet där det nås."""
    x0, y0, x1, y1 = box
    if (min(seg.x0, seg.x1) <= x1 and max(seg.x0, seg.x1) >= x0
            and min(seg.y0, seg.y1) <= y1 and max(seg.y0, seg.y1) >= y0):
        # segmentet går in i lådan: avstånd 0, punkten närmast lådans mitt
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        _, t = point_seg_distance(cx, cy, seg)
        return 0.0, (seg.x0 + t * (seg.x1 - seg.x0), seg.y0 + t * (seg.y1 - seg.y0))
    best = (math.inf, (seg.x0, seg.y0))
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    for cx, cy in corners:
        d, t = point_seg_distance(cx, cy, seg)
        if d < best[0]:
            best = (d, (seg.x0 + t * (seg.x1 - seg.x0), seg.y0 + t * (seg.y1 - seg.y0)))
    for px, py in ((seg.x0, seg.y0), (seg.x1, seg.y1)):
        d = math.hypot(max(x0 - px, 0.0, px - x1), max(y0 - py, 0.0, py - y1))
        if d < best[0]:
            best = (d, (px, py))
    return best


def leaderless_anchors(designations, pipe_labels: set[str], led_blocks: set[str], anchored: set[str],
                       graphs: dict) -> list[PipeCodeAnchor]:
    """Ankare för rörbeteckningar vars block saknar ledare, till närmaste parallella rör."""
    reach = _R("semantics.proximity.LEADERLESS_MM", LEADERLESS_MM) * PT_PER_MM
    ratio = _R("semantics.proximity.AMBIGUITY_RATIO", AMBIGUITY_RATIO)
    tol = _R("semantics.proximity.PARALLEL_DEG", PARALLEL_DEG)
    conf = _R("semantics.proximity.CONFIDENCE", CONFIDENCE)
    comps = {fk: _components(g) for fk, g in graphs.items()}
    out: list[PipeCodeAnchor] = []
    for d in sorted(designations, key=lambda d: d.did):
        if d.did not in pipe_labels or d.did in anchored or d.block_id in led_blocks:
            continue
        nearest: dict[tuple[str, int], tuple[float, object, tuple[float, float]]] = {}
        for fk, g in graphs.items():
            for pid, prim in g.prims.items():
                if not _parallel(prim.seg, d.angle, tol):
                    continue
                dist, pt = _box_seg_distance(d.bbox, prim.seg)
                if dist > reach * ratio:
                    continue
                key = (fk, comps[fk][pid])
                if key not in nearest or dist < nearest[key][0]:
                    nearest[key] = (dist, prim, pt)
        runs = sorted(nearest.items(), key=lambda kv: (kv[1][0], kv[0]))
        if not runs or runs[0][1][0] > reach:
            continue
        (fk, _), (dist, prim, pt) = runs[0]
        rivals = [r for r in runs[1:] if r[1][0] <= ratio * max(dist, 1e-6) and r[1][0] <= reach * ratio]
        contacts = [Contact(point=pt, kind="proximity", family=fk, pid=prim.pid, seg_index=prim.seg_index,
                            distance=dist)]
        for (rfk, _), (rd, rprim, rpt) in rivals:
            contacts.append(Contact(point=rpt, kind="proximity", family=rfk, pid=rprim.pid,
                                    seg_index=rprim.seg_index, distance=rd))
        state = "AMBIGUOUS_PIPE_ATTACHMENT" if rivals else "VERIFIED_PIPE_ATTACHMENT"
        reason = "two_parallel_pipes_equally_near_a_label_without_leader" if rivals else "nearest_parallel_pipe"
        out.append(PipeCodeAnchor(
            anchor_id=stable_id("anc", d.page, d.did, "proximity"), page=d.page, designation_id=d.did,
            designation=d.text, designation_display=d.text, system_token=d.system_token, dn=d.dn,
            multiplier=getattr(d, "multiplier", 1), block_id=d.block_id, leader_id=f"proximity:{d.did}",
            leader_paths=[], endpoint=pt, state=state, reason=reason, contacts=contacts,
            candidate_families=sorted({c.family for c in contacts}),
            evidence={"proximity_without_leader": True, "confidence": conf if not rivals else 0.0,
                      "distance_mm": round(dist / PT_PER_MM, 2), "row_index": d.row_index,
                      "n_rows": 1, "n_groups": 1 if not rivals else len(contacts),
                      "rivals_mm": [round(r[1][0] / PT_PER_MM, 2) for r in rivals]}))
    return out
