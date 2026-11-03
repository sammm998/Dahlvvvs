"""Reading the same drawing by more than one route, and saying where the routes disagree.

One reading can be wrong in a way it cannot see: a leader that lands on the wrong line names the wrong run, and
nothing inside that reading contradicts it. So the sheet is read again by a route that uses different evidence,
and the answers are put side by side. Where two routes name the same geometry the same way, the reading is
corroborated. Where they name it differently, the run is ambiguous and stays out of the quantity. Where only one
route reaches a run, that is said plainly rather than hidden inside a number.

There are two independent ways a drawing says which run a label names, and one way of carrying an identity
further than either of them said:

  pointing   the label's own leader, traced to the geometry it touches or the symbol it ends at
  writing    the label written along the run - parallel to it, beside it, spanning it, and alone on it
  closure    an identity continuing through a junction, or over a layer that names one system: not a second
             reading at all but the first one carried on, so metres reached only this way are counted as such

A route may add a run the others missed, and it may contradict one - which makes the run ambiguous. It may never
rename a run another route confirmed, because then the two readings would no longer be independent.
"""
from __future__ import annotations

import math
from collections import Counter, deque, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .geometry.core import angle_diff
from .pipes.ownership import Identity, identity_from_text
from .text.model import project, row_axes

from . import rules as _rules


def _R(rule_id, default):
    """Vad regeln står på för den läsning som körs på den här tråden."""
    return _rules.value(rule_id, default)


ROUTES = ("pointing", "writing", "closure")
INDEPENDENT = ("pointing", "writing")       # those that read the drawing rather than carry a reading on

_CLOSURE_REASONS = ("family_uniform_identity", "collinear_through_junction",
                    "unlabeled_branch_takes_the_only_junction_identity", "through_junction_up_to_tick_boundary",
                    "junction_dn_completes_dn_less_label", "junction_dn_over_symbol_labels")

ALONGSIDE_BAND = 2.2        # text heights: how far beside its run a label may be written
ALONGSIDE_ANGLE = 6.0       # degrees: the label reads along the run
ALONGSIDE_COVER = 0.6       # the run must span this much of the label's own length
ALONGSIDE_MIN_LABELS = 3    # and the sheet must label this way for more than one label, or it is not its way
ALONGSIDE_MIN_SHARE = 0.15

# A leader that stops short of every run is NOT read as pointing at the nearest one. Tried and measured: on the
# reference sheets it picks the wrong line out of a parallel bundle, because once the tip touches nothing, "which
# run" is decided by distance alone - which is the one thing this engine may never do. Such a leader is reported
# unplaced instead, with its reason, in the review.


@dataclass(frozen=True)
class Claim:
    """One route saying that one primitive belongs to one identity."""
    family: str
    prim_id: int
    identity: Identity
    route: str
    evidence: str


@dataclass
class RouteReport:
    route: str
    claims: list[Claim] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _first_reading(pa) -> dict[str, RouteReport]:
    """The reading already made, split into what the leaders said and what was carried on from it."""
    out = {r: RouteReport(route=r) for r in ROUTES}
    for fk, states in pa.ownership.prim_states.items():
        for pid, st in states.items():
            if st.state != "CONFIRMED" or st.identity is None:
                continue
            r = "closure" if st.reason in _CLOSURE_REASONS else "pointing"
            out[r].claims.append(Claim(family=fk, prim_id=pid, identity=st.identity, route=r, evidence=st.reason))
    return out


def _row_height(d) -> float:
    return max(min(d.bbox[3] - d.bbox[1], d.bbox[2] - d.bbox[0]), 1.0)


def _bbox_span(bbox, axis) -> tuple[float, float]:
    corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3])]
    ps = [project(c, axis) for c in corners]
    return min(ps), max(ps)


def writing_route(pa) -> RouteReport:
    """Labels written along the run they name.

    A drawing may name a run by writing on it rather than by pointing at it. That is evidence when the writing
    reads along the run, sits in a narrow band beside it, spans it, and is the only label on it: then the sheet
    has said which run it means as plainly as a leader would. Anything less is nearness, and nearness names
    nothing - so a label whose own leader already reached a pipe is not read this way, and a sheet where only a
    label or two happens to lie along a run is not a sheet that labels by writing.
    """
    from .pipes.representation import chains as graph_chains
    rep = RouteReport(route="writing")
    if not pa.graphs:
        return rep
    lg = pa.legend
    anchored = {a.designation_id for a in pa.anchors if a.state == "VERIFIED_PIPE_ATTACHMENT"}
    pipe_labels = [d for d in pa.designations
                   if lg.names_a_pipe(d) and (d.text or "").upper() not in lg.components()]
    cand = [d for d in pipe_labels if d.dn is not None and d.did not in anchored]
    chain_of: dict[str, dict[int, int]] = {}
    chains: dict[str, list[list[int]]] = {}
    for fk, g in pa.graphs.items():
        ch = graph_chains(g)
        chains[fk] = ch
        chain_of[fk] = {pid: ci for ci, c in enumerate(ch) for pid in c}
    hits: dict[tuple[str, int], list] = defaultdict(list)
    for d in cand:
        d_dir, d_nrm = row_axes(d.angle)
        H = _row_height(d)
        s0, s1 = _bbox_span(d.bbox, d_dir)
        n0, n1 = _bbox_span(d.bbox, d_nrm)
        base = 0.5 * (n0 + n1)
        found: set[tuple[str, int]] = set()
        for fk, g in pa.graphs.items():
            for pid, q in g.prims.items():
                if angle_diff(q.seg.angle, d.angle % 180.0) > _R("routes.ALONGSIDE_ANGLE", ALONGSIDE_ANGLE):
                    continue
                a0 = min(project(q.a, d_dir), project(q.b, d_dir))
                a1 = max(project(q.a, d_dir), project(q.b, d_dir))
                if min(a1, s1) - max(a0, s0) < _R("routes.ALONGSIDE_COVER", ALONGSIDE_COVER) * max(s1 - s0, 1e-6):
                    continue
                if abs(0.5 * (project(q.a, d_nrm) + project(q.b, d_nrm)) - base) > _R("routes.ALONGSIDE_BAND", ALONGSIDE_BAND) * H:
                    continue
                found.add((fk, chain_of[fk][pid]))
        if len(found) == 1:
            hits[next(iter(found))].append(d)
    named = {k: v[0] for k, v in hits.items() if len(v) == 1}
    rep.stats = {"labels_without_a_leader": len(cand), "runs_named": len(named),
                 "runs_with_several_labels": sum(1 for v in hits.values() if len(v) > 1),
                 "used": False}
    if len(named) < _R("routes.ALONGSIDE_MIN_LABELS", ALONGSIDE_MIN_LABELS) or len(named) < _R("routes.ALONGSIDE_MIN_SHARE", ALONGSIDE_MIN_SHARE) * max(len(pipe_labels), 1):
        return rep      # this sheet does not label by writing along its runs; a stray adjacency says nothing
    rep.stats["used"] = True
    for (fk, ci), d in sorted(named.items()):
        ident = identity_from_text(d.display_text, d.dn, d.system_token, None)
        for pid in chains[fk][ci]:
            rep.claims.append(Claim(family=fk, prim_id=pid, identity=ident, route="writing",
                                    evidence="label_written_along_the_run"))
    return rep


def run_routes(pa) -> dict[str, RouteReport]:
    reports = _first_reading(pa)
    reports["writing"] = writing_route(pa)
    return reports


def cross_check(pa, reports: dict[str, RouteReport]) -> dict[str, Any]:
    """Put the routes side by side, primitive by primitive, and say where they agree."""
    by_prim: dict[tuple[str, int], dict[str, Identity]] = defaultdict(dict)
    for rep in reports.values():
        for c in rep.claims:
            by_prim[(c.family, c.prim_id)][c.route] = c.identity
    mpp = (pa.scale.meters_per_pt or 0.0) if pa.scale else 0.0
    corroborated = pointed = closure_only = conflict = 0.0
    conflicts: list[dict] = []
    per_route: Counter = Counter()
    for (fk, pid), claims in by_prim.items():
        L = pa.graphs[fk].prims[pid].seg.length * mpp
        for r in claims:
            per_route[r] += L
        keys = {i.key for i in claims.values()}
        indep = [r for r in claims if r in INDEPENDENT]
        if len(keys) > 1:
            conflict += L
            conflicts.append({"family": fk, "prim": pid, "routes": {r: i.key for r, i in sorted(claims.items())}})
        elif len(indep) >= 2:
            corroborated += L
        elif indep:
            pointed += L
        else:
            closure_only += L
    return {
        "routes": {r: {"metres": round(per_route[r], 2), **reports[r].stats} for r in ROUTES},
        "corroborated_m": round(corroborated, 2),
        "one_reading_m": round(pointed, 2),
        "closure_only_m": round(closure_only, 2),
        "in_conflict_m": round(conflict, 2),
        "conflicts": conflicts[:200],
        "n_conflicts": len(conflicts),
    }


def apply_routes(pa, reports: dict[str, RouteReport]) -> dict[str, Any]:
    """Let a second route add what the first missed, and let a disagreement take a run out of the quantity."""
    from .pipes.ownership import _build_pipes
    own = pa.ownership
    added = made_ambiguous = 0.0
    mpp = (pa.scale.meters_per_pt or 0.0) if pa.scale else 0.0
    by_prim: dict[tuple[str, int], dict[str, Identity]] = defaultdict(dict)
    for rep in reports.values():
        if rep.route in ("pointing", "closure"):
            continue        # these are the reading already made
        for c in rep.claims:
            by_prim[(c.family, c.prim_id)][c.route] = c.identity
    touched: set[str] = set()
    for (fk, pid), claims in sorted(by_prim.items()):
        st = own.prim_states[fk][pid]
        keys = {i.key for i in claims.values()}
        L = pa.graphs[fk].prims[pid].seg.length * mpp
        if st.state == "CONFIRMED" and st.identity is not None:
            if len(keys) == 1 and next(iter(keys)) == st.identity.key:
                st.evidence.append("corroborated_by_" + "_and_".join(sorted(claims)))
            else:
                st.candidates = {st.identity} | set(claims.values())
                st.state, st.identity = "AMBIGUOUS", None
                st.reason = "AMBIGUOUS_ROUTES_DISAGREE"
                st.evidence.append("routes_disagree:" + ",".join(sorted(keys)))
                made_ambiguous += L
                touched.add(fk)
        elif st.state == "UNOWNED" and len(keys) == 1:
            st.state = "CONFIRMED"
            st.identity = next(iter(claims.values()))
            st.reason = "second_route_" + sorted(claims)[0]
            st.evidence.append("named_by_" + "_and_".join(sorted(claims)))
            added += L
            touched.add(fk)
    if touched:
        own.pipes = [p for p in own.pipes if p.family not in touched]
        for fk in sorted(touched):
            own.pipes.extend(_build_pipes(pa.graphs[fk], own.prim_states[fk], fk, pa.page.info.index))
        own.pipes.sort(key=lambda p: p.physical_pipe_id)
    return {"added_m": round(added, 2), "made_ambiguous_m": round(made_ambiguous, 2),
            "families_rebuilt": sorted(touched)}


def review(pa, cross: dict[str, Any]) -> dict[str, Any]:
    """What the reading did not reach, and why - so nothing is missing quietly.

    Two sweeps: the pipe geometry no route named, gathered into runs so a reader can find them on the sheet; and
    the pipe labels no route placed, with the reason the attachment gave.
    """
    from .pipes.representation import chains as graph_chains
    mpp = (pa.scale.meters_per_pt or 0.0) if pa.scale else 0.0
    unnamed: list[dict] = []
    unowned_m = ambiguous_m = confirmed_m = outside_m = 0.0
    for fk, g in pa.graphs.items():
        states = pa.ownership.prim_states[fk]
        for c in graph_chains(g):
            L = sum(g.prims[p].seg.length for p in c) * mpp
            confirmed_m += sum(g.prims[p].seg.length for p in c if states[p].state == "CONFIRMED") * mpp
            ambiguous_m += sum(g.prims[p].seg.length for p in c if states[p].state == "AMBIGUOUS") * mpp
            unowned_m += sum(g.prims[p].seg.length for p in c if states[p].state == "UNOWNED") * mpp
            outside_m += sum(g.prims[p].seg.length for p in c if states[p].state == "OUTSIDE_CONTRACT") * mpp
            if all(states[p].state == "UNOWNED" for p in c) and L >= 0.5:
                q = g.prims[c[0]].seg
                unnamed.append({"family": fk, "n_primitives": len(c), "metres": round(L, 2),
                                "at": [round(q.x0, 1), round(q.y0, 1)], "reason": "no_route_named_this_run"})
    unnamed.sort(key=lambda r: -r["metres"])
    lg = pa.legend
    pipe_labels = [d for d in pa.designations
                   if lg.names_a_pipe(d) and (d.text or "").upper() not in lg.components()]
    placed = {a.designation_id for a in pa.anchors if a.state == "VERIFIED_PIPE_ATTACHMENT"}
    reasons: Counter = Counter()
    unplaced: list[dict] = []
    for d in pipe_labels:
        if d.did in placed:
            continue
        why = sorted({a.reason for a in pa.anchors if a.designation_id == d.did}) or ["no_leader_found"]
        reasons[why[0]] += 1
        unplaced.append({"designation": d.display_text, "at": [round(d.bbox[0], 1), round(d.bbox[1], 1)],
                         "reason": why[0], "unknown_characters": d.unknown_chars})
    # den kapade biten vid en entreprenadgräns: delen bortom gränsen är utanför, inte bekräftad
    cut = sum(r["cut_outside_pt"] for r in (getattr(pa.ownership, "outside_contract", None) or [])) * mpp
    confirmed_m -= cut
    outside_m += cut
    total = confirmed_m + ambiguous_m + unowned_m + outside_m
    return {
        "pipe_geometry_m": round(total, 2),
        "named_m": round(confirmed_m, 2),
        "ambiguous_m": round(ambiguous_m, 2),
        "unnamed_m": round(unowned_m, 2),
        "outside_contract_m": round(outside_m, 2),
        "coverage_pct": round(100.0 * confirmed_m / (total - outside_m), 1) if total - outside_m > 0 else None,
        "corroborated_m": cross["corroborated_m"],
        "one_reading_m": cross["one_reading_m"],
        "closure_only_m": cross["closure_only_m"],
        "in_conflict_m": cross["in_conflict_m"],
        "unnamed_runs": unnamed[:100],
        "n_unnamed_runs": len(unnamed),
        "pipe_labels": len(pipe_labels),
        "pipe_labels_placed": sum(1 for d in pipe_labels if d.did in placed),
        "unplaced_labels": unplaced[:200],
        "unplaced_reasons": dict(reasons.most_common()),
        **_further_questions(pa, confirmed_m, ambiguous_m),
    }


def _further_questions(pa, confirmed: float = 0.0, ambiguous: float = 0.0) -> dict[str, Any]:
    """The rest of what a reader should be told the reading could not settle.

    Each of these is a way a takeoff can be quietly short without anything looking wrong: geometry the labels
    clearly point at that was never accepted as pipe, runs that stop facing each other across a gap nobody
    bridged, the same line drawn twice on two pens, a designation list that was never found. None of them is an
    error the engine can fix on its own - they are stated so that what is missing is visible instead of absent.
    """
    from .pipes.representation import chains as graph_chains
    out: dict[str, Any] = {}
    stats = getattr(pa, "contact_stats", None) or {}
    votes, ticks = stats.get("votes") or {}, stats.get("tick_votes") or {}
    accepted = set(pa.pipe_families)

    # 5b. the unnamed metres, split by whether they hang off pipe the reading did name.
    #
    # One number was hiding two very different things. Geometry in a component that also holds named pipe is
    # pipe the reading did not reach - a branch it stopped at, a length past the last label - and it is coverage
    # waiting to be won. Geometry in a component where nothing is named is something else: on a sheet with no
    # layer names the pipe family is a pen and a colour, and a wall or a grid drawn with that pen lands in it.
    # Reporting them together makes a reading of a busy sheet look far worse than it is, and hides which of the
    # two a reader should do something about.
    mpp = (pa.scale.meters_per_pt or 0.0) if getattr(pa, "scale", None) else 0.0
    touching = apart = 0.0
    n_touch = n_apart = 0
    for fk, g in pa.graphs.items():
        states = pa.ownership.prim_states[fk]
        seen: set[int] = set()
        for start in sorted(g.prims):
            if start in seen:
                continue
            comp, dq = [], deque([start])
            seen.add(start)
            while dq:
                q = dq.popleft()
                comp.append(q)
                for nid in g.prim_nodes[q]:
                    for r2 in g.nodes[nid].prims:
                        if r2 not in seen:
                            seen.add(r2)
                            dq.append(r2)
            named = any(states[q].state in ("CONFIRMED", "AMBIGUOUS") for q in comp)
            un = sum(g.prims[q].seg.length for q in comp if states[q].state == "UNOWNED") * mpp
            if un <= 0:
                continue
            if named:
                touching += un
                n_touch += 1
            else:
                apart += un
                n_apart += 1
    out["unnamed_touching_named_pipe_m"] = round(touching, 2)
    out["unnamed_touching_named_pipe_components"] = n_touch
    out["unnamed_standalone_m"] = round(apart, 2)
    out["unnamed_standalone_components"] = n_apart
    reach = confirmed + ambiguous + touching
    out["coverage_of_reachable_pct"] = round(100.0 * confirmed / reach, 1) if reach else None
    out["unnamed_split_note"] = ("metres hanging off pipe the reading named are coverage it did not reach; "
                                 "metres in components where nothing is named are geometry no label ever "
                                 "touched, which on a sheet without layer names is often not pipe at all")

    # 4. geometry the sheet's own labels point at, that was not accepted as pipe
    rejected = []
    for f, v in sorted(votes.items(), key=lambda kv: -kv[1]):
        if f in accepted:
            continue
        t = ticks.get(f, 0)
        if v >= 5 or t >= 3:
            rejected.append({"family": f, "leader_ends": round(v, 1), "with_tick": t,
                             "reason": "labels_end_here_but_it_was_not_accepted_as_pipe_geometry"})
    out["rejected_families_labels_point_at"] = rejected[:20]

    # 7. runs that stop facing another run of their own family, across a gap the reading did not bridge
    broken = []
    for fk, g in pa.graphs.items():
        gap = g.gap_mode or 0.0
        if gap <= 0 or not math.isfinite(gap):
            continue
        ends = []
        for c in graph_chains(g):
            for nid in (g.prim_nodes[c[0]][0], g.prim_nodes[c[-1]][1]):
                if g.nodes[nid].degree == 1:
                    ends.append((nid, g.nodes[nid]))
        # Each end looks only at the ends in its own lattice cell and the eight around it: comparing every free
        # end with every other was quadratic, and a dense dashed sheet has tens of thousands of them. The first
        # later end within reach is still the one reported, exactly as the pairwise walk found it.
        cell = gap * 6.0
        lattice: dict[tuple[int, int], list[int]] = {}
        for j, (_, b) in enumerate(ends):
            lattice.setdefault((math.floor(b.x / cell), math.floor(b.y / cell)), []).append(j)
        for i, (na, a) in enumerate(ends):
            cx, cy = math.floor(a.x / cell), math.floor(a.y / cell)
            hit = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in lattice.get((cx + dx, cy + dy), ()):
                        if j <= i or (hit is not None and j >= hit[0]):
                            continue
                        b = ends[j][1]
                        d = math.hypot(a.x - b.x, a.y - b.y)
                        if gap * 1.5 < d <= gap * 6.0:
                            hit = (j, d)
            if hit is not None:
                broken.append({"family": fk, "at": [round(a.x, 1), round(a.y, 1)], "gap_pt": round(hit[1], 1),
                               "this_familys_gap_pt": round(gap, 2),
                               "reason": "two_free_ends_face_each_other_further_apart_than_this_lines_own_gap"})
                if len(broken) >= 40:
                    break        # only the first forty are reported; looking further is time for nothing
        if len(broken) >= 40:
            break
    out["possible_lost_continuity"] = broken[:40]

    # 9. the same line drawn twice on two different pens: measured once per family, so twice in total
    stamps: dict[tuple, list[str]] = {}
    for fk, g in pa.graphs.items():
        for q in g.prims.values():
            a = (round(q.seg.x0 * 4), round(q.seg.y0 * 4)); b = (round(q.seg.x1 * 4), round(q.seg.y1 * 4))
            stamps.setdefault((a, b) if a <= b else (b, a), []).append(fk)
    dup = [{"at": [k[0][0] / 4, k[0][1] / 4], "families": sorted(set(v)),
            "reason": "one_drawn_line_appears_in_more_than_one_pipe_family"}
           for k, v in sorted(stamps.items()) if len(set(v)) > 1]
    out["possible_double_counted_geometry"] = dup[:40]

    # 11. the sheet's own designation list: found, and what it was able to say
    lg = pa.legend
    # a system the sheet's own list names, for which no label was ever read, is a system this reading missed
    read_codes = {(d.display_text or d.text or "").upper() for d in pa.designations}
    unseen_systems = sorted(c for c in lg.systems()
                            if not any(t == c or t.startswith(c) for t in read_codes))
    out["legend"] = {"found": bool(lg.entries), "entries": len(lg.entries),
                     "systems": sorted(lg.systems()), "components": sorted(lg.components()),
                     "systems_with_no_label_read": unseen_systems,
                     "reason": ("no_designation_list_found_on_this_page_the_reading_used_pattern_statistics_only"
                                if not lg.entries else
                                "found_but_named_no_systems" if not lg.systems() else
                                "listed_systems_this_reading_never_found_a_label_for" if unseen_systems else "used")}

    # 12. drawn families that look like pipe but no label ever reached: a style this reading does not support
    # 1. text the reading put back together but could not name every character of. Shown as it was read, with
    # the unread positions marked, because "F?V1" tells a reader what is missing and a count does not.
    unread = []
    for r in pa.lines:
        t = (r.text or "")
        if "?" not in t:
            continue
        unread.append({"read_as": t[:60], "at": [round(r.bbox[0], 1), round(r.bbox[1], 1)],
                       "unknown_characters": t.count("?"),
                       "reason": "the_shape_matched_no_reference_letter_closely_enough_to_be_named"})
    out["text_with_unread_characters"] = sorted(unread, key=lambda x: -x["unknown_characters"])[:40]
    out["n_text_with_unread_characters"] = len(unread)

    out["unsupported_style_candidates"] = [
        {"family": f, "reason": "chain_like_geometry_no_label_ever_reached"}
        for f in sorted(set(votes) - accepted) if votes.get(f, 0) < 5 and ticks.get(f, 0) == 0][:20]

    # 12b. the labels the sheet writes that never got a line to follow. A pipe designation with no leader is the
    # commonest way a named pipe goes unmarked, and the reason separates what the reading can fix from what the
    # drawing simply does not contain.
    no_leader = (pa.contact_stats or {}).get("labels_without_a_leader") or {}
    with_leader = {a.designation_id for a in pa.anchors}
    lost = []
    for d in pa.designations:
        if d.did in with_leader or not lg.names_a_pipe(d) or (d.text or "").upper() in lg.components():
            continue
        lost.append({"text": (d.text or "")[:40], "at": [round(d.bbox[0], 1), round(d.bbox[1], 1)],
                     "reasons": no_leader.get(d.block_id) or ["no_line_starts_at_this_label_at_all"]})
    out["pipe_labels_with_no_leader"] = lost[:40]
    out["n_pipe_labels_with_no_leader"] = len(lost)

    # 13. what the reading looked at and did not take. Drawn ink that never becomes pipe leaves the reading
    # silently, and a reader looking at un-measured lines cannot tell a declined wall from a missed run. The
    # families a leader end actually touched are the ones worth a second look, so they are named first.
    mpp = pa.scale.meters_per_pt if pa.scale else None
    dec = (pa.contact_stats or {}).get("declined_families") or {}
    rows = [{"family": f, "why": v["why"], "kind": v["kind"], "leader_ends_touching": v["tick_votes"],
             "label_votes": v["votes"], "n_segments": v["n_segments"],
             "length_m": round(v["total_length_pt"] * mpp, 2) if mpp else None}
            for f, v in dec.items()]
    rows.sort(key=lambda r: (-r["leader_ends_touching"], -(r["length_m"] or 0.0)))
    out["declined_families"] = rows[:20]
    unc = (pa.contact_stats or {}).get("unconsidered_families") or {}
    out["declined_families_total"] = {
        "families": len(rows), "length_m": round(sum(r["length_m"] or 0.0 for r in rows), 2) if mpp else None,
        "families_a_leader_end_touched": sum(1 for r in rows if r["leader_ends_touching"]),
        "length_m_a_leader_end_touched": round(sum(r["length_m"] or 0.0 for r in rows if r["leader_ends_touching"]), 2) if mpp else None,
        # ink no leader ever pointed at: it cannot become pipe, and it is still most of what a reader sees
        "unconsidered_families": len(unc),
        "unconsidered_length_m": round(sum(v["total_length_pt"] for v in unc.values()) * mpp, 2) if mpp else None,
        "unconsidered_families_on_a_pipe_like_layer": sum(1 for v in unc.values() if v.get("on_a_pipe_like_layer")),
        "unconsidered_length_m_on_a_pipe_like_layer": round(sum(v["total_length_pt"] for v in unc.values() if v.get("on_a_pipe_like_layer")) * mpp, 2) if mpp else None}
    return out
