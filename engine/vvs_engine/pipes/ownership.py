"""Physical pipe ownership.

VERIFIED PipeCodeAnchors seed identities on pipe graphs. Resolution is CHAIN based (a chain = maximal run of
primitives through degree-2 nodes): a chain carrying seeds of one identity is confirmed entirely; where seeds of
different identities sit on the same chain, the geometry between them is AMBIGUOUS (DN boundary / system
conflict) - never split at an invented midpoint. At junctions an identity continues only along a collinear arm
(straight run through a tee/cross) or an arm supported by agreeing anchors; other unlabeled arms are
AMBIGUOUS_BRANCH. Every primitive ends as CONFIRMED, AMBIGUOUS or UNOWNED.
"""
from __future__ import annotations

import os
import re

import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Any

from ..geometry.core import GridIndex, angle_diff, dist, point_seg_distance, stable_id
from ..semantics.attachment import PipeCodeAnchor, system_layer_match
from .representation import PipeGraph, Prim, chains as graph_chains
from .frontier import BRANCH_END_EVIDENCE
from ..source_rules.systems import permits_unlabelled_twin

from .. import rules as _rules


def _R(rule_id, default):
    """Vad regeln står på för den läsning som körs på den här tråden."""
    return _rules.value(rule_id, default)



@dataclass(frozen=True)
class Identity:
    base: str                 # designation without its inline DN token (system + material tokens + qualifier)
    dn: int | None
    system: str
    display: str              # designation as written (most common form)
    stem: str = ""            # ...and without the qualifier either: what the two ways of writing it share
    qualifier: str | None = None   # what the drawing writes after the dimension: insulation, covering, medium

    def __post_init__(self) -> None:
        if not self.stem:
            object.__setattr__(self, "stem", self.base)

    @property
    def key(self) -> str:
        return f"{self.base}|DN{self.dn if self.dn is not None else '?'}"

    def compatible(self, other: "Identity") -> bool:
        """Two labels name the same pipe when nothing either of them states contradicts the other.

        A drawing states a run's dimension and its insulation where it has room to and leaves them off where it
        has not: the same heating run is written `VS21-S13-15-F50` at one end and `VS21-S13` with `15` on the row
        below at the other. Silence is not a different answer, so a label that says nothing about the insulation
        agrees with one that names it - and a label that names a different one does not, because a second
        insulation on the same dimension is a second thing to order.
        """
        return (self.stem == other.stem
                and (self.dn is None or other.dn is None or self.dn == other.dn)
                and (self.qualifier is None or other.qualifier is None or self.qualifier == other.qualifier))


@dataclass
class PrimState:
    state: str = "UNOWNED"                    # CONFIRMED | AMBIGUOUS | UNOWNED
    identity: Identity | None = None
    candidates: set[Identity] = field(default_factory=set)
    reason: str = ""
    anchors: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)
    tentative: bool = False                   # CONFIRMED on the best reading only: measured, and marked for review


@dataclass
class PhysicalPipe:
    physical_pipe_id: str
    page: int
    family: str
    identity: Identity
    anchor_ids: list[str]
    prim_ids: list[int]
    points: list[list[tuple[float, float]]]
    source_paths: list[str]
    source_segments: list[str]
    nodes: list[int]
    raw_length_pt: float
    bridged_gap_pt: float
    frontier_reasons: list[str]
    evidence: list[str]
    state: str = "CONFIRMED"
    frontiers: list[dict] = field(default_factory=list)     # var röret slutar och varför (pipes/frontier.py)

    section_levels: list[dict] = field(default_factory=list)
    elevation_anchor_ids: list[str] | None = None
    needs_review: bool = False                # measured on a tentative reading; the person checks it on the sheet
    flags: list = field(default_factory=list)  # consistency checks this run fails (source_rules/consistency.py)

    @property
    def length_pt(self) -> float:
        return self.raw_length_pt + self.bridged_gap_pt


@dataclass
class OwnershipResult:
    prim_states: dict[str, dict[int, PrimState]]
    pipes: list[PhysicalPipe]
    ambiguous_runs: list[dict]
    stats: dict[str, Any]
    riser_labels: dict[str, list[dict]] = field(default_factory=dict)   # identity key -> symbol-attached labels


def identity_of(a: PipeCodeAnchor, dn_token_index: int | None) -> Identity:
    """The identity an anchor's label names (see identity_from_text)."""
    return identity_from_text(a.designation_display or a.designation, a.dn, a.system_token, dn_token_index)


def identity_from_text(text: str, dn: int | None, system_token: str, dn_token_index: int | None) -> Identity:
    """The identity a label names: its designation without the dimension token, plus the dimension.

    The name is read from the designation as the drawing states it - with a dimension row folded in - so that the
    two ways of writing one pipe, all on one line or the dimension on the row below, give the same identity and
    are not measured as two. The dimension token is the one the grammar points at, and otherwise the first token
    after the system token that spells the dimension itself.
    """
    from ..semantics.grammar import split_tokens
    # Keep explicitly printed L/V service variants separate from a pipe without
    # that suffix. Treating them like optional insulation would let sheet-wide
    # completion transfer the suffix to every pipe of the same size.
    service = None
    core = text
    if dn is not None:
        suffix = re.search(r"(?<![A-Za-zÅÄÖ0-9])" + re.escape(str(dn))
                           + r"\s*(?:\(\s*([LV])\s*\)|([LV]))(?=$|[\s/\-])", text)
        if suffix:
            service = suffix.group(1) or suffix.group(2)
            core = text[:suffix.start()] + str(dn) + text[suffix.end():]
    toks = [t.strip() for t in split_tokens(core) if t.strip()]
    idx = dn_token_index
    if not (idx is not None and 0 < idx < len(toks) and toks[idx].isdigit() and dn is not None and int(toks[idx]) == dn):
        idx = None
        if dn is not None:
            pat = re.compile(re.escape(str(dn)) + r"[A-Za-zÅÄÖÅäö]{0,3}")
            idx = next((i for i, t in enumerate(toks) if i > 0 and pat.fullmatch(t)), None)
    stem_toks = list(toks)
    tail: list[str] = []
    if idx is not None:
        # The dimension token goes, and with it the short code the drawing writes after it - F50, W40, F60 - which
        # names the insulation or covering rather than the pipe. It is kept beside the name instead of inside it,
        # because a drawing writes it where it has room and leaves it off where it has not: held as part of the
        # name, the same run read at both ends became two runs and was reported twice.
        end = idx + 1
        while end < len(toks) and len(toks[end]) <= 4 and toks[end][:1].isalpha():
            tail.append(toks[end])
            end += 1
        stem_toks = toks[:idx] + toks[end:]
    stem = "-".join(stem_toks)
    # A label naming two pipes laid side by side writes both codes and the size once: `VV01-53-/KV01-53-20`. The
    # next label along the same pair writes them the other way round, `KV01-53-/VV01-53-20`, and it names the
    # same two pipes. Held in the order written they were two identities claiming one run, the run was left to
    # neither, and nine metres of it had no name. The parts are held in one order, whichever the label used.
    parts = [p for p in core.split("/") if p.strip()]
    if len(parts) >= 2 and dn is not None and not tail and idx == len(toks) - 1:
        own = [[t.strip() for t in split_tokens(p) if t.strip()] for p in parts]
        if len(own[-1]) >= 2 and own[-1][-1] == str(dn):
            own[-1] = own[-1][:-1]
            if all(o[0][:1].isalpha() and any(ch.isdigit() for ch in o[0]) for o in own):
                stem = "/".join(sorted("-".join(o) for o in own))
    if service:
        stem += f"-({service})"
    qual = "-".join(tail) or None
    return Identity(base="-".join([stem, qual]) if qual else stem, dn=dn, system=system_token, display=text,
                    stem=stem, qualifier=qual)


TICK_KINDS = ("end_tick", "crossing_tick")
STRONG_KINDS = ("end", "end_tick", "crossing_tick")        # the leader meets the pipe line itself
BOUNDARY_TOL = 0.75


def _prims_of_segment(g: PipeGraph, pid: str, seg_index: int) -> list[int]:
    """The graph primitives cut from one drawn segment, in the graph's own order.

    Looking them up by walking every primitive for every contact made the seeding quadratic: on a sheet with a
    few thousand labels and a few hundred thousand primitives that alone ran for minutes. The index is kept on the
    graph and rebuilt whenever the primitive table is no longer the one it was built from, or has grown or shrunk
    (a run split at a landing adds primitives); a listed primitive that no longer matches rebuilds it as well.
    """
    cached = getattr(g, "_segment_index", None)
    if cached is None or cached[0] is not g.prims or cached[1] != len(g.prims):
        index: dict[tuple, list[int]] = defaultdict(list)
        for k, q in g.prims.items():
            index[(q.pid, q.seg_index)].append(k)
        cached = (g.prims, len(g.prims), index)
        g._segment_index = cached
    found = cached[2].get((pid, seg_index), [])
    for k in found:
        q = g.prims.get(k)
        if q is None or q.pid != pid or q.seg_index != seg_index:
            g._segment_index = None
            return _prims_of_segment(g, pid, seg_index)
    return found


def _seed_prims(a: PipeCodeAnchor, graphs: dict[str, PipeGraph]) -> dict[str, list[tuple[int, str, tuple[float, float]]]]:
    """Graph primitives touched by an anchor's contacts: (prim id, contact kind, contact point) per family."""
    out: dict[str, list[tuple[int, str, tuple[float, float]]]] = defaultdict(list)
    for c in a.contacts:
        g = graphs.get(c.family)
        if g is None:
            continue
        best = None
        for pid in _prims_of_segment(g, c.pid, c.seg_index):
            q = g.prims[pid]
            d, t = point_seg_distance(c.point[0], c.point[1], q.seg)
            if best is None or d < best[0] - 1e-9:
                best = (d, pid)
        if best is not None and best[0] <= 3.0 and all(p != best[1] for p, _, _ in out[c.family]):
            out[c.family].append((best[1], c.kind, c.point))
    return out


def complete_identities(identities: dict[str, Identity]) -> dict[str, Identity]:
    """What a label leaves out, read off the rest of the sheet - but only where the sheet says it once.

    A draughtsman writes as much of a designation as the space allows. The same heating run is `VS21-S13-15-F50`
    where it runs through open floor and `VS21-S13` with `15` on the row below where it threads between two
    walls; a stack is `S01-P5-110` at the riser and `S01-P5` beside it. Read literally, one run becomes two
    entries in the takeoff - the metres split between them, the estimator ordering twice - and on twenty-five of
    thirty-three reference sheets that is what happened, to four hundred metres of correctly measured pipe.

    So a label that leaves the dimension or the insulation unsaid takes what the sheet says elsewhere for the
    same designation, on one condition: the sheet must say it exactly one way. Where a sheet runs `VV01-X7-25`
    both as F50 and as F60, silence does not pick one of them - the run keeps its own short name and is reported
    under it, which is the reading a person can argue with rather than a guess they cannot see.
    """
    stated_dn: dict[str, set[int]] = defaultdict(set)
    for i in identities.values():
        if i.dn is not None:
            stated_dn[i.stem].add(i.dn)
    out: dict[str, Identity] = {}
    for aid, i in identities.items():
        if i.dn is None and len(stated_dn.get(i.stem) or ()) == 1:
            i = replace(i, dn=next(iter(stated_dn[i.stem])), display=_fullest(identities.values(), i.stem, None))
        out[aid] = i
    stated_q: dict[tuple[str, int | None], set[str]] = defaultdict(set)
    for i in out.values():
        if i.qualifier is not None:
            stated_q[(i.stem, i.dn)].add(i.qualifier)
    for aid, i in list(out.items()):
        if i.qualifier is not None:
            continue
        q = stated_q.get((i.stem, i.dn)) or ()
        if len(q) == 1:
            qual = next(iter(q))
            out[aid] = replace(i, qualifier=qual, base="-".join([i.stem, qual]),
                               display=_fullest(out.values(), i.stem, i.dn))
    return out


def _fullest(ids, stem: str, dn: int | None) -> str:
    """How the drawing writes this run where it writes it out in full.

    A run named in two ways is reported the fuller way. The estimator orders from this name, and `VS21-S13-15`
    and `VS21-S13-15-F50` are the same pipe but not the same order: one of them says how it is insulated. Ties
    go to the name that sorts first, so the same drawing is always read the same way.
    """
    said = [i for i in ids if i.stem == stem and i.qualifier is not None and (dn is None or i.dn == dn)]
    if not said:
        said = [i for i in ids if i.stem == stem and i.dn is not None and (dn is None or i.dn == dn)]
    if not said:
        return stem
    # the fuller name - the one with more parts, since a part says how the run is insulated or qualified - wins;
    # among names with the same parts the one most of the sheet's labels use, so that a stray letter one
    # dimension row picked up from its neighbour ("110L" beside eight "110") does not rename the run
    votes = Counter(i.display for i in said)
    return min(votes, key=lambda t: (-len([x for x in t.split("-") if x]), -votes[t], -len(t), t))


DECLARED_REASON = "DECLARED_CONNECTION_PIPE_BY_SHEET_TABLE"
# "Kopplingsledningar från fördelare till apparat": a connection pipe runs from a distributor to a fixture and
# is short. A run longer than this on the declared pen is a main whose label the reading did not reach, and the
# rule the sheet wrote for connection pipes does not name it - it stays unowned and is reported as such.
DECLARED_RUN_MAX_M = 15.0


def _unowned_components(g: PipeGraph, st: dict[int, "PrimState"]) -> list[list[int]]:
    """Connected sets of UNOWNED primitives: a run nobody named, taken as one piece."""
    adj: dict[int, set[int]] = defaultdict(set)
    for pid, nodes in g.prim_nodes.items():
        if st[pid].state != "UNOWNED":
            continue
        for n in nodes:
            adj[n].add(pid)
    seen: set[int] = set()
    out: list[list[int]] = []
    for pid in sorted(g.prims):
        if pid in seen or st[pid].state != "UNOWNED":
            continue
        comp, stack = [], [pid]
        while stack:
            q = stack.pop()
            if q in seen:
                continue
            seen.add(q)
            comp.append(q)
            for n in g.prim_nodes[q]:
                stack.extend(r for r in adj[n] if r not in seen)
        out.append(sorted(comp))
    return out


def _declare_unowned(graphs: dict[str, PipeGraph], states: dict[str, dict[int, "PrimState"]], declared,
                     spelled_out: frozenset[str], max_run_pt: float | None = None) -> dict[str, int]:
    """Geometry no label reached, named by the rule the sheet wrote for exactly that case.

    "Kopplingsledningar från fördelare till apparat enligt tabell om inget annat anges": the table gives a
    designation per system and a dimension, and the rule applies to the pipes nobody labelled. So an UNOWNED
    primitive - never AMBIGUOUS, never one a label reached - on a pen whose layer name carries the declared
    system's token takes the declared identity. A layer that two declared systems name equally well gets
    nothing: the sheet has not said which. A layer that names one of them better - its class and its number,
    against its class alone - is that one's: a rule declared for KV1 and KV2 alike names the pen whose layer
    says V1. The reason is written on every primitive so the takeoff can show which metres were pointed at and
    which were declared."""
    from ..semantics.attachment import system_layer_rank
    from ..semantics.grammar import split_tokens
    given: dict[str, int] = {}
    for fk, g in graphs.items():
        layer = fk.split("|s|")[0]
        ranked = sorted(((r[0], d) for d in declared if d.dn is not None
                         for r in [system_layer_rank(d.system_token, layer, spelled_out)] if r),
                        key=lambda t: (t[0], t[1].text))
        by_join = not layer and not ranked
        if not by_join and (not ranked or (len(ranked) > 1 and ranked[1][0] == ranked[0][0])):
            continue
        d = None if by_join else ranked[0][1]
        n = 0
        for comp in _unowned_components(g, states[fk]):
            run = sum(g.prims[pid].seg.length for pid in comp)
            if max_run_pt is not None and run > max_run_pt:
                for pid in comp:
                    states[fk][pid].evidence.append("too_long_for_a_declared_connection_pipe")
                continue                    # a main the rule for connection pipes does not name
            here = _declared_by_join(g, states[fk], comp, declared) if by_join else d
            if here is None:
                continue
            ident = identity_from_text(here.text, here.dn, here.system_token, len(split_tokens(here.stem)))
            for pid in comp:
                st = states[fk][pid]
                st.state, st.identity, st.reason = "CONFIRMED", ident, DECLARED_REASON
                st.evidence = [f"sheet_table:{here.text}"] + (["system_from_the_pipe_it_joins"] if by_join else [])
                n += 1
        if n:
            given[fk] = n
    return given


def _declared_by_join(g: PipeGraph, st: dict[int, "PrimState"], comp: list[int], declared):
    """The declared connection pipe for a run on a pen with no layer name: the one system it joins.

    A file exported without layers gives the pen no system, and the rule for connection pipes is written per
    system. A connection tube runs from a distributor, so it meets the distributor's labelled pipe; when every
    labelled pipe the run meets belongs to one declared system it is that system's connection pipe. A run that
    meets two of them, or none, is left unowned - the sheet has not said which."""
    nodes = {n for pid in comp for n in g.prim_nodes[pid]}
    inside = set(comp)
    systems = set()
    for pid, ns in g.prim_nodes.items():
        if pid in inside or st[pid].state != "CONFIRMED" or st[pid].identity is None:
            continue
        if nodes.intersection(ns):
            systems.add((st[pid].identity.system or "").upper())
    fits = [d for d in declared if d.dn is not None and (d.system_token or "").upper() in systems]
    if len({(d.system_token or "").upper() for d in fits}) != 1 or len({d.text for d in fits}) != 1:
        return None
    return fits[0]


def propagate(graphs: dict[str, PipeGraph], anchors: list[PipeCodeAnchor], page: int,
              identities: dict[str, Identity], spelled_out: frozenset[str] = frozenset(),
              declared=None, declared_max_pt: float | None = None,
              end_evidence: dict[str, dict[int, dict]] | None = None) -> OwnershipResult:
    """identities: anchor_id -> Identity (only anchors that are verified AND belong to pipe-designation families).
    declared: the sheet's written rules for unlabelled pipes (semantics.declarations.DeclaredPipe), applied last
    and only to geometry every other reading left unowned.
    end_evidence: what sits at every free end of every family (pipes.frontier.end_evidence) - the positive evidence
    an unlabelled branch needs before it may take the junction's name."""
    identities = complete_identities(identities)
    states: dict[str, dict[int, PrimState]] = {fk: {pid: PrimState() for pid in g.prims} for fk, g in graphs.items()}
    seeds: dict[str, dict[int, list[tuple[Identity, str, str, tuple[float, float]]]]] = {fk: defaultdict(list) for fk in graphs}
    for a in sorted(anchors, key=lambda a: a.anchor_id):
        if a.anchor_id not in identities:
            continue
        ident = identities[a.anchor_id]
        for fk, lst in _seed_prims(a, graphs).items():
            for pid, kind, pt in lst:
                seeds[fk][pid].append((ident, a.anchor_id, kind, pt))
    ambiguous_runs: list[dict] = []
    for fk, g in graphs.items():
        _resolve_family(g, states[fk], seeds[fk], ambiguous_runs, fk, end_evidence)
    for fk, g in graphs.items():
        _family_uniform_identity(fk, states[fk], anchors, identities, spelled_out, g)
    for fk, g in graphs.items():
        _demote_sliver_outlines(g, states[fk], fk, ambiguous_runs)
    for fk, g in graphs.items():
        _bound_junction_flow(g, states[fk], fk, ambiguous_runs)
    for fk, g in graphs.items():
        _pair_unowned_runs(g, states[fk])
    given = _declare_unowned(graphs, states, list(declared), spelled_out, declared_max_pt) if declared else {}
    if _R("pipes.ownership.PROPAGATE_TO_STOPS", PROPAGATE_TO_STOPS):
        # efter bladets egna kopplingsledningsregler: de går före ett ärvt, osäkert namn
        for fk, g in graphs.items():
            _inherit_at_branches(g, states[fk])
        ambiguous_runs = [r for r in ambiguous_runs
                          if states[r["family"]].get(r["from_prim"]) is None
                          or states[r["family"]][r["from_prim"]].state == "AMBIGUOUS"]
    pipes: list[PhysicalPipe] = []
    for fk, g in graphs.items():
        pipes.extend(_build_pipes(g, states[fk], fk, page))
    pipes.sort(key=lambda p: p.physical_pipe_id)
    stats = Counter()
    for fk in graphs:
        for st in states[fk].values():
            stats[st.state] += 1
    out = dict(stats)
    if given:
        out["declared_prims"] = sum(given.values())
    return OwnershipResult(prim_states=states, pipes=pipes, ambiguous_runs=ambiguous_runs, stats=out)


# the two rules that carry an identity into geometry no label touched and no drawn boundary delimits
FLOWED_REASONS = ("collinear_through_junction", "unlabeled_branch_takes_the_only_junction_identity")

# ---------------------------------------------------------------- parade ledningar: tillopp och retur i samma penna
#
# Ett värmesystem ritas som två parallella linjer - tillopp och retur - på ett fast avstånd, och etiketten
# sätts på den ena. Mängdaren räknar båda; läsningen ägde bara den etiketterade. Regeln här är bladets egen:
# först måste pennan visa att den ritar par - två ägda linjer med *samma* identitet, parallella och
# överlappande, på ett avstånd som återkommer (fördelningens topp). Först då får en oägd ledning som löper
# parallellt med en ägd på just det avståndet, längs större delen av sin längd och utan att någon annan
# identitet konkurrerar, ta identiteten. Avståndet är aldrig en konstant: det är pennans egen topp, mätt på
# det som redan är ägt. Saknar bladet par blir ingenting parat.
PAIR_ANGLE_DEG = 3.0          # parallellt: inom tre grader
PAIR_OFFSET_MIN = 3.0         # pt: närmare än så är det samma streck (dubbla konturer), inte ett par
PAIR_OFFSET_MAX = 60.0        # pt: längre bort än så är det två ledningar som råkar gå åt samma håll
PAIR_SUPPORT_MIN = 40.0       # pt: så mycket överlapp måste de ägda paren ha innan pennan räknas som en som ritar par
PAIR_SHARE_MIN = 0.6          # så stor del av den oägda ledningen måste löpa parallellt med den ägda
PAIR_COMPETITION_MAX = 0.15   # ...och så liten del får en andra identitet göra anspråk på
PAIRED_REASON = "paired_run_at_the_drawings_pair_spacing"


def _pair_geometry(p: Prim, q: Prim, extend: float = 0.0) -> tuple[float, float] | None:
    """Är q parallell med p? Då (vinkelrätt avstånd mellan linjerna, överlapp längs p i pt), annars None.

    `extend` förlänger q i båda ändar: ett streck i en streckad linje täcker halva glappet på var sida, så att
    en streckad tvilling räknas som den hela linje den är och inte som sina streck - annars avgör streckens
    fas, inte ledningen, hur mycket som överlappar."""
    if angle_diff(p.seg.angle, q.seg.angle) > _R("pipes.ownership.PAIR_ANGLE_DEG", PAIR_ANGLE_DEG):
        return None
    L = p.seg.length
    if L < 1e-9:
        return None
    ux, uy = (p.seg.x1 - p.seg.x0) / L, (p.seg.y1 - p.seg.y0) / L
    d = abs(-(q.seg.x0 - p.seg.x0) * uy + (q.seg.y0 - p.seg.y0) * ux)
    t0 = (q.seg.x0 - p.seg.x0) * ux + (q.seg.y0 - p.seg.y0) * uy
    t1 = (q.seg.x1 - p.seg.x0) * ux + (q.seg.y1 - p.seg.y0) * uy
    lo, hi = max(0.0, min(t0, t1) - extend), min(L, max(t0, t1) + extend)
    if hi - lo <= 0.0:
        return None
    return d, hi - lo


def _pair_spacing(g: PipeGraph, st: dict[int, PrimState], idx: GridIndex) -> tuple[float, float] | None:
    """Pennans eget paravstånd: toppen i fördelningen av avstånd mellan ägda, parallella, överlappande
    primitiver med samma identitet - och hur mycket överlapp som bär toppen. None när pennan inte ritar par."""
    lo_off = _R("pipes.ownership.PAIR_OFFSET_MIN", PAIR_OFFSET_MIN)
    hi_off = _R("pipes.ownership.PAIR_OFFSET_MAX", PAIR_OFFSET_MAX)
    hist: Counter = Counter()
    seen: set[tuple[int, int]] = set()
    ext = (g.gap_mode or 0.0) / 2.0 + 0.3
    for pid in sorted(g.prims):
        p = g.prims[pid]
        sp = st[pid]
        if sp.state != "CONFIRMED" or sp.identity is None or p.seg.length <= 2.5:
            continue
        x0, y0, x1, y1 = min(p.seg.x0, p.seg.x1), min(p.seg.y0, p.seg.y1), max(p.seg.x0, p.seg.x1), max(p.seg.y0, p.seg.y1)
        for qid in idx.query((x0 - hi_off, y0 - hi_off, x1 + hi_off, y1 + hi_off)):
            if qid == pid or (min(pid, qid), max(pid, qid)) in seen:
                continue
            sq = st[qid]
            if sq.state != "CONFIRMED" or sq.identity is None or sq.identity.key != sp.identity.key or g.prims[qid].seg.length <= 2.5:
                continue
            seen.add((min(pid, qid), max(pid, qid)))
            geo = _pair_geometry(p, g.prims[qid], ext)
            if geo is None or not (lo_off <= geo[0] <= hi_off):
                continue
            hist[int(round(geo[0]))] += geo[1]
    if not hist:
        return None
    mode = max(hist, key=lambda k: (hist[k] + hist.get(k - 1, 0.0) + hist.get(k + 1, 0.0), -k))
    support = hist[mode] + hist.get(mode - 1, 0.0) + hist.get(mode + 1, 0.0)
    if support < max(_R("pipes.ownership.PAIR_SUPPORT_MIN", PAIR_SUPPORT_MIN), 3.0 * mode):
        return None
    return float(mode), support


def _pair_unowned_runs(g: PipeGraph, st: dict[int, PrimState]) -> int:
    """Para endast system med line_count=2 enligt Swedish VVS/PipeStudio, med belagd pargeometri."""
    idx = GridIndex(cell=24.0)
    for pid, p in g.prims.items():
        idx.insert(pid, (min(p.seg.x0, p.seg.x1), min(p.seg.y0, p.seg.y1), max(p.seg.x0, p.seg.x1), max(p.seg.y0, p.seg.y1)))
    spacing = _pair_spacing(g, st, idx)
    if spacing is None:
        return 0
    d_star, support = spacing
    tol = max(2.0, 0.25 * d_star)
    ext = (g.gap_mode or 0.0) / 2.0 + 0.3
    share_min = _R("pipes.ownership.PAIR_SHARE_MIN", PAIR_SHARE_MIN)
    comp_max = _R("pipes.ownership.PAIR_COMPETITION_MAX", PAIR_COMPETITION_MAX)
    n = 0
    for comp in _unowned_components(g, st):
        total = sum(g.prims[pid].seg.length for pid in comp)
        if total <= 2.5:
            continue
        overlap: Counter = Counter()
        idents: dict[str, Identity] = {}
        anchors: dict[str, set[str]] = defaultdict(set)
        for pid in comp:
            p = g.prims[pid]
            if p.seg.length <= 2.5:
                continue
            x0, y0, x1, y1 = min(p.seg.x0, p.seg.x1), min(p.seg.y0, p.seg.y1), max(p.seg.x0, p.seg.x1), max(p.seg.y0, p.seg.y1)
            for qid in idx.query((x0 - d_star - tol, y0 - d_star - tol, x1 + d_star + tol, y1 + d_star + tol)):
                sq = st[qid]
                if (sq.state != "CONFIRMED" or sq.identity is None or sq.reason == PAIRED_REASON
                        or not permits_unlabelled_twin(sq.identity.system)):
                    continue
                geo = _pair_geometry(p, g.prims[qid], ext)
                if geo is None or abs(geo[0] - d_star) > tol:
                    continue
                key = sq.identity.key
                overlap[key] += geo[1]
                idents[key] = sq.identity
                anchors[key] |= sq.anchors
        if not overlap:
            continue
        ranked = overlap.most_common(2)
        best_key, best_ov = ranked[0]
        second_ov = ranked[1][1] if len(ranked) > 1 else 0.0
        best_ov, second_ov = min(best_ov, total), min(second_ov, total)
        if best_ov < share_min * total or second_ov > comp_max * total:
            continue
        # A run the drawing already joins to a named pipe - it shares a node with a confirmed primitive of
        # another identity, an elbow into it or a tee off it - is that junction's to decide, not the pair's.
        # Measured on a bend where a DN35 riser turned into a horizontal that a DN22 line ran beside: the
        # pair rule named the horizontal DN22 and the riser's own run stopped at a DN boundary it had drawn
        # itself. The neighbour across the gap says which pair it is; the neighbour at the node says what it
        # is connected to, and a connection outranks a spacing.
        joined = {st[r].identity.key for pid in comp for node in g.prim_nodes[pid] for r in g.nodes[node].prims
                  if st[r].state == "CONFIRMED" and st[r].identity is not None}
        if joined - {best_key}:
            continue
        for pid in comp:
            s = st[pid]
            s.state, s.identity, s.reason = "CONFIRMED", idents[best_key], PAIRED_REASON
            s.anchors = set(anchors[best_key])
            s.evidence.append(f"paired_at_{d_star:.0f}pt_overlap_{best_ov / total:.2f}_pair_support_{support:.0f}pt")
            n += 1
    return n

TRANSITION_EVIDENCE = "representation_transition_needs_its_own_evidence"


def _up_to_transition(g: PipeGraph, pids: list[int], from_end: bool, seeded_solid: bool = False) -> list[int]:
    """Så långt ett namn får rinna längs en kedja: fram till den första långa heldragna linjen.

    På ett blad utan lager delar pålbalkarna penna med de streckade ledningarna, och där en ledning slutade
    intill en balk rann namnet vidare längs balken - runt hela huset. Ett streckat rör som fortsätter som en
    lång heldragen linje har bytt ritsätt, och ett byte av ritsätt är inte ett bevis på att röret fortsätter.
    Det som får bevisa det är en etikett på den heldragna delen; utan den stannar namnet vid övergången, och
    fronten säger varför. `from_end` säger från vilken ände av listan namnet kommer."""
    order = list(reversed(pids)) if from_end else list(pids)
    out: list[int] = []
    for pid in order:
        # övergången är relativ det etiketten sitter på: från streck in i en lång heldragen linje, eller från
        # en etiketterad heldragen linje in i streck. Korta heldragna bitar - böjar, armaturer - är ingetdera.
        if g.prims[pid].solid_long != seeded_solid and (g.prims[pid].solid_long or seeded_solid):
            break
        out.append(pid)
    return list(reversed(out)) if from_end else out
# how far past the labelled runs the flow may reach before it stops being a reading of the drawing. Measured over
# the style library: every reading confirmed by overlay sits at or under 1.7, every reading of walls at or over
# 2.7, so the flow may at most double what the labels themselves delimit.
FLOW_LIMIT = 2.0


# Så många oberoende slingor gör spridd geometri till ett nät (väggar, rutnät): där gäller längdbudgeten kvar.
# En enda slinga kan vara en ledning som går runt och möter sig själv.
FLOW_MESH_CYCLES = 2


def _cycles(g: PipeGraph, prims) -> int:
    """Oberoende slingor (E - V + C) bland rörbitarna, med dubbelritade bitar mellan samma nodpar räknade en gång."""
    pairs = {tuple(sorted(g.prim_nodes[p])) for p in prims}
    nodes = {n for pr in pairs for n in pr}
    parent = {n: n for n in nodes}

    def find(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in pairs:
        parent[find(a)] = find(b)
    return len(pairs) - len(nodes) + len({find(n) for n in nodes})


def _has_cycle(g: PipeGraph, comp: list[int]) -> bool:
    """Är de här spridda rörbitarna ett nät snarare än ett träd av rör?"""
    return _cycles(g, comp) >= _R("pipes.ownership.FLOW_MESH_CYCLES", FLOW_MESH_CYCLES)

# VS-Kalkyl SPEC 6.4.3 h.3: en etikett gäller längs röret - raka fortsättningar och böjar - tills ny etikett,
# symbol, dimensionsbytesmärke eller entreprenadgräns; ingen längdbudget. Vid en förgrening utan egen etikett ärver
# grenen system och material, men dimensionen är osäker: grenen mäts, i granskningsnivån (konfidens 0,6), som ett
# eget rör, och den märkta ledningen den grenar av påverkas inte. Av: flödesbudgeten och de äldre kraven på bevis i
# grenens ände gäller som förut.
PROPAGATE_TO_STOPS = True
INHERITED_REASON = "unlabelled_branch_inherits_system_and_material"
INHERITED_EVIDENCE = "dn_inherited_at_branch_uncertain"


def _unnamed_meshes(g: PipeGraph, st: dict[int, PrimState]) -> set[int]:
    """Rörbitar i onämnda sammanhängande delar som innehåller en slinga. Ett rörnät i plan är ett träd; ett nät av
    väggar på samma penna, ett hölje, en ritad rektangel har slingor. Dit ärvs inget namn."""
    unnamed = [p for p in g.prims if st[p].state != "CONFIRMED"]
    parent: dict[int, int] = {}

    def find(n: int) -> int:
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for p in unnamed:
        a, b = g.prim_nodes[p]
        parent[find(a)] = find(b)
    for br in g.bridges:
        fa, fb = br.get("from_node"), br.get("to_node")
        if fa in parent and fb in parent:
            parent[find(fa)] = find(fb)
    by_root: dict[int, list[int]] = defaultdict(list)
    for p in unnamed:
        by_root[find(g.prim_nodes[p][0])].append(p)
    cyclic = {r for r, prims in by_root.items() if _cycles(g, prims) >= 1}
    return {p for p in unnamed if find(g.prim_nodes[p][0]) in cyclic}


def _inherit_at_branches(g: PipeGraph, st: dict[int, PrimState]) -> None:
    """Grenar utan egen etikett ärver system och material; dimensionen markeras osäker (tentative).

    - En gren som redan tagit korsningens enda identitet: namnet står, men dimensionen är en gissning.
    - En gren som var tvetydig bara för att dess ände saknade bevis, eller mellan identiteter som delar system och
      material och bara skiljer i dimension: den tar systemet och materialet, med den grövre dimensionen som osäker.
      Skiljer sig system eller material är det en verklig konflikt och grenen förblir tvetydig.
    - Onämnd geometri som möter ett namngivet rör i en förgrening ärver på samma sätt, gren för gren.
    """
    def inherit(pid: int, ident: Identity, why: str) -> None:
        s = st[pid]
        s.state, s.identity, s.reason, s.candidates = "CONFIRMED", ident, INHERITED_REASON, set()
        s.tentative = True
        s.evidence.extend([INHERITED_EVIDENCE, why])

    for s in st.values():
        if s.state == "CONFIRMED" and s.reason == "unlabeled_branch_takes_the_only_junction_identity" \
                and not s.tentative:
            s.tentative = True
            s.evidence.append(INHERITED_EVIDENCE)
    meshes = _unnamed_meshes(g, st)
    ch0 = [c for c in graph_chains(g)]
    chain0 = {p: i for i, c in enumerate(ch0) for p in c}

    def leads_into_mesh(pid: int) -> bool:
        # grenens kedja eller det den mynnar i har en slinga: ett nät, inte ett rör
        chain = ch0[chain0[pid]]
        if any(q in meshes for q in chain):
            return True
        ends = {n for q in chain for n in g.prim_nodes[q]}
        return any(q in meshes for n in ends for q in g.nodes[n].prims)

    for pid in sorted(st):
        s = st[pid]
        if s.state == "AMBIGUOUS" and s.reason in (NO_END_EVIDENCE, "AMBIGUOUS_BRANCH") and s.candidates \
                and len({c.stem for c in s.candidates}) == 1 and not leads_into_mesh(pid):
            inherit(pid, max(s.candidates, key=lambda c: (c.dn or 0, c.key)), f"was_{s.reason}")
    ch = [c for c in graph_chains(g)]
    chain_of = {p: i for i, c in enumerate(ch) for p in c}
    # "Samma stråk byter dimension, den grövre fortsätter" gäller ett stråk med en böj. Där de två namngivna armarna
    # ligger i linje med varandra och denna arm går av från dem är den en gren, och dess dimension en gissning.
    for nid in sorted(g.nodes):
        n = g.nodes[nid]
        if n.degree < 3:
            continue
        for p in n.prims:
            s = st[p]
            if s.state != "CONFIRMED" or s.reason != "same_run_changes_size_the_coarser_carries_on" or s.tentative:
                continue
            others = [q for q in n.prims if q != p and st[q].state == "CONFIRMED"]
            straight_past = any(angle_diff(g.prims[a].seg.angle, g.prims[b].seg.angle) <= 3.0
                                for i, a in enumerate(others) for b in others[i + 1:])
            in_line = any(angle_diff(g.prims[q].seg.angle, g.prims[p].seg.angle) <= 3.0 for q in others)
            if straight_past and not in_line:
                for q in ch[chain_of[p]]:
                    if st[q].state == "CONFIRMED" and st[q].reason == s.reason and not st[q].tentative:
                        st[q].tentative = True
                        st[q].evidence.append(INHERITED_EVIDENCE)
    changed = True
    while changed:
        changed = False
        for nid in sorted(g.nodes):
            n = g.nodes[nid]
            if n.degree < 3:
                continue
            owners = {st[p].identity for p in n.prims if st[p].state == "CONFIRMED" and st[p].identity is not None}
            if not owners or len({o.stem for o in owners}) != 1:
                continue
            ident = max(owners, key=lambda c: (c.dn or 0, c.key))
            for p in sorted(n.prims):
                if st[p].state != "UNOWNED" or p in meshes:
                    continue
                for q in ch[chain_of[p]]:
                    if st[q].state == "UNOWNED":
                        inherit(q, ident, f"unowned_branch_at_node_{nid}")
                        changed = True


def _bound_junction_flow(g: PipeGraph, st: dict[int, PrimState], fk: str, ambiguous_runs: list[dict]) -> None:
    """An identity may run on through a junction into geometry the drawing does not name - a straight run through
    a tee, an unnamed branch off a labelled one. On a pipe network that adds a little to what the labels say. On
    a mesh of geometry that only looks like a network it adds without end, and every metre of it is a guess.

    So the flow has to stay within reach of what the drawing itself states - and the reach is the run's own,
    not the family's. It used to be summed over the whole pen: one run that had wandered into a wall grid could
    push the family past the limit and take the flowed metres off every other run on that pen, runs that had
    flowed a few points past their last label and no further. Now each connected run of one identity is weighed
    against its own labelled length: where its flowed length runs past twice what its labels delimit, that
    run's flow is not a reading and its geometry is AMBIGUOUS, and the run next to it is left alone.
    """
    to_stops = _R("pipes.ownership.PROPAGATE_TO_STOPS", PROPAGATE_TO_STOPS)
    limit = _R("pipes.ownership.FLOW_LIMIT", FLOW_LIMIT)
    seen: set[int] = set()
    for start in sorted(st):
        s0 = st[start]
        if start in seen or s0.state != "CONFIRMED" or s0.identity is None:
            continue
        comp = []
        dq = deque([start])
        seen.add(start)
        while dq:
            p = dq.popleft()
            comp.append(p)
            for node in g.prim_nodes[p]:
                for q in g.nodes[node].prims:
                    if q not in seen and st[q].state == "CONFIRMED" and st[q].identity == s0.identity:
                        seen.add(q)
                        dq.append(q)
        labelled = flowed = 0.0
        for pid in comp:
            if st[pid].reason in FLOWED_REASONS:
                flowed += g.prims[pid].seg.length
            else:
                labelled += g.prims[pid].seg.length
        if os.environ.get("VVS_DEBUG_FLOW") and flowed > 0:
            print(f"[flow] {s0.identity.key:28s} labelled={labelled:8.1f} flowed={flowed:8.1f} "
                  f"kvot={flowed / labelled if labelled else float('inf'):6.2f} gräns={limit:5.2f}",
                  file=__import__("sys").stderr)
        if flowed <= limit * labelled:
            continue
        if to_stops and not _has_cycle(g, [p for p in comp if st[p].reason in FLOWED_REASONS]):
            continue    # spridningen bildar ett träd: ingen längdbudget; den stannar vid etikett, symbol eller gräns
        ident_key = s0.identity.key                 # read before the demotion below may blank the start prim
        caught: list[int] = []
        for pid in sorted(comp):
            s = st[pid]
            if s.reason not in FLOWED_REASONS:
                continue
            ident = s.identity
            s.state, s.identity, s.reason = "AMBIGUOUS", None, "AMBIGUOUS_FLOW_BEYOND_THE_LABELLED_RUNS"
            s.candidates = {ident} if ident is not None else set()
            s.evidence.append("identity_flowed_far_past_what_the_labels_of_this_run_delimit")
            caught.append(pid)
        if caught:
            # a real primitive, not a sentinel: every reader of an ambiguous run looks its geometry up to put the
            # case on the drawing, and a placeholder id sends them looking for a primitive that does not exist
            ambiguous_runs.append({"family": fk, "chain": -1, "from_prim": caught[0], "to_prim": caught[-1],
                                   "reason": "AMBIGUOUS_FLOW_BEYOND_THE_LABELLED_RUNS",
                                   "identities": [ident_key], "n_primitives": len(caught),
                                   "flowed_pt": round(flowed, 1), "labelled_pt": round(labelled, 1)})



# A pipe is drawn as one line down its middle; a thin object - a radiator, a bench, a duct seen edge on - is
# drawn as its two long sides, and those sides are pipe-thin geometry on the same pen.
#
# What tells them apart is not a distance in millimetres of paper. A drawing states its own unit of "too close
# to read apart": the pen it draws with. Two centres within a couple of pen widths lay ink on the same place,
# whatever the paper size or the scale the sheet was plotted at, and an office that draws thicker draws its
# objects thicker too. So the reach is the family's own stroke width, and the run's own length: a pair only
# reads as one drawn thing while it runs many times further than it is wide.
#
# The drawing has a second thing to say, which is recorded rather than required: a spacing it comes back to
# again and again is its way of drawing an object, while a one-off is where two pipes happened to pass close.
SLIVER_ELONGATION = 20.0           # the pair runs this many times further than it is wide
SLIVER_COVER = 0.8
SLIVER_PENS = 2.0                  # ... and their centres lie within this many of the family's own pen widths
SLIVER_SPREAD = 0.15               # gaps within this much of each other are the same spacing (relative: scale-free)
SLIVER_MIN_REPEATS = 4             # a spacing the family returns to this often is how it draws an object
SLIVER_MIN_SHARE = 0.10            # ... and carries this share of the family's close parallel pairs


def _recurring_gaps(gaps: list[float]) -> list[tuple[float, float]]:
    """The parallel spacings this family draws over and over, as (centre, slack) bands.

    Clustered on the values themselves with a relative tolerance, so a band means the same thing on a sheet drawn
    at 1:50 and one at 1:100. A cluster the drawing returns to is its own statement about how it draws an object;
    a one-off is where two pipes passed close."""
    if len(gaps) < _R("pipes.ownership.SLIVER_MIN_REPEATS", SLIVER_MIN_REPEATS):
        return []
    clusters: list[list[float]] = []
    for v in sorted(gaps):
        if clusters and v <= clusters[-1][0] * (1.0 + SLIVER_SPREAD):
            clusters[-1].append(v)
        else:
            clusters.append([v])
    bands = []
    for c in clusters:
        if len(c) < _R("pipes.ownership.SLIVER_MIN_REPEATS", SLIVER_MIN_REPEATS) or len(c) < _R("pipes.ownership.SLIVER_MIN_SHARE", SLIVER_MIN_SHARE) * len(gaps):
            continue
        mid = c[len(c) // 2]
        bands.append((mid, max(c[-1] - mid, mid - c[0])))
    return bands


def _demote_sliver_outlines(g: PipeGraph, st: dict[int, PrimState], fk: str, ambiguous_runs: list[dict]) -> None:
    """A pipe is drawn as one line down its middle. A thin object - a radiator, a bench, a duct seen edge on - is
    drawn as its two long sides, and those sides are pipe-thin geometry on the same pen as the pipes.

    Where a confirmed run has a parallel twin of its own family covering it end to end, closer than a couple of
    the family's own pen widths, the two are the sides of something drawn, not two pipes. Such a run is
    AMBIGUOUS - the geometry stays on the page for a human to name, but its length is not anyone's pipe.

    The reach is never a fixed number of points: it is the drawing's own pen and the run's own length, so a sheet
    at another scale, or an office that draws its radiators wider, is read on its own terms.

    Whether the spacing is one the family returns to is measured too, but it is recorded as evidence rather than
    required. A sheet that draws a single such object states nothing about repetition, and demanding it there
    would hand that object's length back to a pipe on the strength of having only seen it once.
    """
    idx = GridIndex(cell=20.0)
    for pid, prim in g.prims.items():
        idx.insert(pid, prim.seg.bbox())
    # the pen this family draws with, as the drawing itself set it
    pen = max((g.prims[pid].width for pid in g.prims), default=0.0)
    if pen <= 0:
        return

    def twin_gap(pid: int) -> float | None:
        """The distance to a parallel same-family twin that covers this run end to end, if there is one.

        How far to look is set by the run itself: a pair only reads as one drawn object while it runs many times
        further than it is wide, so nothing beyond that ratio is a candidate and no paper measure is needed."""
        seg = g.prims[pid].seg
        if seg.length < _R("pipes.ownership.SLIVER_ELONGATION", SLIVER_ELONGATION) * 0.5:
            return None
        # both bounds come off the drawing: its own pen, and this run's own length
        reach = min(seg.length / _R("pipes.ownership.SLIVER_ELONGATION", SLIVER_ELONGATION), _R("pipes.ownership.SLIVER_PENS", SLIVER_PENS) * pen)
        if reach <= 0:
            return None
        ux, uy = (seg.x1 - seg.x0) / seg.length, (seg.y1 - seg.y0) / seg.length
        x0, y0, x1, y1 = seg.bbox()
        for tid in sorted(idx.query((x0 - reach, y0 - reach, x1 + reach, y1 + reach))):
            if tid == pid:
                continue
            t = g.prims[tid].seg
            if t.length < seg.length * _R("pipes.ownership.SLIVER_COVER", SLIVER_COVER):
                continue
            vx, vy = (t.x1 - t.x0) / t.length, (t.y1 - t.y0) / t.length
            if abs(ux * vx + uy * vy) < 0.999:                       # parallel within ~2.5 degrees
                continue
            mx, my = (t.x0 + t.x1) / 2, (t.y0 + t.y1) / 2
            gap = abs((mx - seg.x0) * -uy + (my - seg.y0) * ux)
            if not 1e-6 < gap <= reach:
                continue
            lo, hi = sorted(((t.x0 - seg.x0) * ux + (t.y0 - seg.y0) * uy,
                             (t.x1 - seg.x0) * ux + (t.y1 - seg.y0) * uy))
            if max(0.0, min(hi, seg.length) - max(lo, 0.0)) < seg.length * _R("pipes.ownership.SLIVER_COVER", SLIVER_COVER):
                continue
            return gap
        return None

    # first pass: which close parallel spacings does this family use, and which of them does it come back to
    pairs: list[tuple[int, float]] = []
    for pid in sorted(st):
        if st[pid].state != "CONFIRMED":
            continue
        gap = twin_gap(pid)
        if gap is not None:
            pairs.append((pid, gap))
    bands = _recurring_gaps([gap for _, gap in pairs])

    caught: list[int] = []
    for pid, gap in pairs:
        repeated = any(abs(gap - mid) <= slack for mid, slack in bands)
        s = st[pid]
        ident = s.identity
        s.state, s.identity, s.reason = "AMBIGUOUS", None, "AMBIGUOUS_SLIVER_PAIR_READS_AS_A_DRAWN_OUTLINE"
        s.candidates = {ident} if ident is not None else set()
        s.evidence.append(f"parallel_twin_of_the_same_family_{gap:.2f}pt_away_covers_this_run_end_to_end")
        s.evidence.append(f"closer_than_{_R('pipes.ownership.SLIVER_PENS', SLIVER_PENS):g}"
                                  f"_pen_widths_of_this_family_{pen:.2f}pt")
        if repeated:
            s.evidence.append("and_that_spacing_recurs_on_this_sheet_"
                              + "_".join(f"{m:.2f}" for m, _ in bands))
        caught.append(pid)
    if caught:
        ambiguous_runs.append({"family": fk, "chain": -1, "from_prim": caught[0], "to_prim": caught[-1],
                               "reason": "AMBIGUOUS_SLIVER_PAIR_READS_AS_A_DRAWN_OUTLINE",
                               "identities": sorted({c.key for p in caught for c in st[p].candidates}),
                               "n_primitives": len(caught)})

def _family_uniform_identity(fk: str, st: dict[int, PrimState], anchors: list[PipeCodeAnchor], identities: dict[str, Identity],
                             spelled_out: frozenset[str] = frozenset(), g: "PipeGraph | None" = None) -> None:
    """A vector family whose layer name structurally carries one system token (exact or abbreviated tail, never a
    wildcard) and whose verified anchors (>= 2) all agree on one designation AND DN is a single-system, single-size
    layer: its unlabeled runs carry that identity (evidence: layer token + every anchor of the family).

    Men bara det bläck som hänger ihop med det etiketterna nådde.

    Ett lagernamn säger vilket system bläcket tillhör. Det säger inte att två streck i var sin ände av bladet är
    samma rör, och det säger inte att en lös bit alls är ett rör. Regeln gav namnet åt allt i familjen, hur
    långt bort det än låg - och då blev en frånkopplad sträcka en bekräftad meter som ingen etikett hade pekat
    på, med samma säkerhet som en mätt sträcka. Det är precis den sortens fel som inte syns: talet ser ut som
    de andra talen.

    Så kravet är lokal koppling. Sträckan ska sitta i samma sammanhängande nät som någon av familjens egna
    ankare. Det som ligger för sig självt får stå kvar som onämnt - ett ärligt "vi vet inte" - tills ritningen
    eller en människa säger något om det.
    """
    aids = sorted({a.anchor_id for a in anchors if a.anchor_id in identities and any(c.family == fk for c in a.contacts)})
    if len(aids) < 2:
        return
    idents = [identities[aid] for aid in aids]
    if any(i.dn is None for i in idents):
        return
    uni = _merge_identity(idents)
    if uni is None or uni.dn is None:
        return
    layer = fk.split("|s|")[0]
    tok = system_layer_match(uni.system, layer, spelled_out)
    if not tok:
        return
    S, TU = uni.system.upper(), tok.upper()
    if not (TU == S or (len(S) > len(TU) and S.endswith(TU))):
        return      # alpha-only or wildcard layer tokens cover several systems: no family-level identity
    # vilka sammanhängande nät familjens egna ankare faktiskt sitter i
    reach = _components_with_anchors(g, st, set(aids)) if g is not None else None
    for pid in sorted(st):
        s = st[pid]
        if reach is not None and pid not in reach:
            continue                 # ligger inte i något nät en etikett nådde: lagernamnet räcker inte
        if s.state == "UNOWNED" or (s.state == "AMBIGUOUS" and s.candidates and all(_merge_identity([uni, c]) is not None for c in s.candidates)):
            s.state, s.identity, s.reason, s.candidates = "CONFIRMED", uni, "family_uniform_identity", set()
            s.anchors |= set(aids)
            s.evidence.append(f"layer_token_{tok}_and_{len(aids)}_agreeing_anchors_{uni.key}")


def _components_with_anchors(g: "PipeGraph", st: dict[int, PrimState], aids: set[str]) -> set[int]:
    """Primitiverna i de sammanhängande nät som något av ankarna sitter i.

    Nätet är familjens graf: två primitiver hänger ihop när de delar en nod. Ett ankare sitter i det nät vars
    primitiver bär dess id. Allt annat är en egen ö, och en ö är inte samma rör bara för att den ritats med
    samma penna på samma lager.
    """
    parent: dict[int, int] = {pid: pid for pid in g.prims}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for node in g.nodes.values():
        ps = sorted(node.prims)
        for q in ps[1:]:
            union(ps[0], q)
    seeded = {find(pid) for pid, s in st.items() if pid in parent and (s.anchors & aids)}
    return {pid for pid in parent if find(pid) in seeded}


SLIVER_RUN = 0.6            # pt: kortare än så är ingen sträcka, det är avrundningen i utdraget


def _too_short_to_carry(g: PipeGraph, ch: list[list[int]], ci: int) -> bool:
    """Är kedjan för kort för att bära ett namn vidare?

    En kedja på en tiondels punkt är inte ett rör. Den är vad ett CAD-utdrag lämnar efter sig när två linjer
    delas vid en skärning: en stump kortare än den tolerans som avgör om två saker alls rör vid varandra.

    Den sortens stump bär ingen riktning och inget bevis, men den duger som brygga: tar den namnet vid en nod
    blir den en löst arm vid nästa, och därifrån vandrar namnet vidare över ett glapp det aldrig borde ha
    korsat. Femton sådana bryggor på ett enda blad, och sex och en halv meter byggnad redovisad som rör.
    """
    return sum(g.prims[q].seg.length for q in ch[ci]) < _R("pipes.ownership.SLIVER_RUN", SLIVER_RUN)


def _opposite_sides(g: PipeGraph, n, a: int, b: int) -> bool:
    """Går de två armarna ut åt var sitt håll från noden?

    En linje som passerar en annan går in på ena sidan och ut på den andra. Två parallella armar som pekar åt
    samma håll gör inte det: det är samma linje ritad två gånger, eller en stump som ligger ovanpå ledningen.
    Riktningen räknas från noden och ut mot armens andra ände, för det är den änden som säger vart armen går.
    """
    def away(pid: int) -> tuple[float, float]:
        s = g.prims[pid].seg
        d0 = (s.x0 - n.x) ** 2 + (s.y0 - n.y) ** 2
        d1 = (s.x1 - n.x) ** 2 + (s.y1 - n.y) ** 2
        far = (s.x1, s.y1) if d1 >= d0 else (s.x0, s.y0)
        vx, vy = far[0] - n.x, far[1] - n.y
        L = math.hypot(vx, vy) or 1.0
        return vx / L, vy / L

    ax, ay = away(a)
    bx, by = away(b)
    return ax * bx + ay * by < 0.0


def _merge_identity(ids: list[Identity]) -> Identity | None:
    """Merge compatible identities: same stem, and each of dimension and qualifier stated at most one way.

    Where one label leaves the dimension or the insulation unsaid and another names it, the run takes the one
    that names it - and is written out the way the label that names it wrote it, so the takeoff carries the
    drawing's fullest statement of the run rather than its shortest.
    """
    if not ids:
        return None
    stem = ids[0].stem
    if any(i.stem != stem for i in ids):
        return None
    dns = {i.dn for i in ids if i.dn is not None}
    quals = {i.qualifier for i in ids if i.qualifier is not None}
    if len(dns) > 1 or len(quals) > 1:
        return None
    dn = next(iter(dns)) if dns else None
    qual = next(iter(quals)) if quals else None
    # the fullest statement wins, not the most frequent one: a run written short in sixteen places and in full
    # in three is still the run the three describe
    seen = Counter(i.display for i in ids if i.dn == dn and i.qualifier == qual)
    disp = min(seen, key=lambda t: (-len(t), t)) if seen else ids[0].display
    return Identity(base="-".join([stem, qual]) if qual else stem, dn=dn, system=ids[0].system, display=disp,
                    stem=stem, qualifier=qual)


def flows_into_other_size(source: Identity, arm: Identity) -> bool:
    """Får källans dimension ta över en arm som är märkt med en annan?

    Vid en knut är det den grövre ledningen som fortsätter och den klenare som lämnar den. En stam i DN110 som
    möter en gren märkt DN75 löper vidare fram till grenens bock - det är så en dimensionsändring ritas, och
    därför får den grövre flyta in i den klenares arm.

    Åt andra hållet går det inte. En gren i DN75 kan inte ta stammens DN110-arm, för då vore grenen grövre än
    det den grenar av från. Utan den spärren blev det tvärtom: ett stråk som bar åtta etiketter - sju som sa
    DN110, en som sa DN75 - bokfördes i sin helhet som DN75. Metrarna var mätta, bara skrivna på fel rad, och
    en mängd som står under fel dimension är värre för en kalkyl än en tappad meter: summan ser rätt ut och
    varje prissatt rad är fel.

    Säger den ena ingenting om sin dimension är det ingen storleksfråga, och då gäller de övriga reglerna.
    """
    return source.dn is None or arm.dn is None or source.dn > arm.dn


def _conflict_reason(ids: list[Identity]) -> str:
    return "AMBIGUOUS_DN_BOUNDARY" if len({i.base for i in ids}) == 1 or len({i.system for i in ids}) == 1 else "SYSTEM_CONFLICT"


@dataclass
class _SeedGroup:
    pos: float                    # chain position: k + 0.5 on primitive k, or integer k for a boundary at node k
    merged: Identity | None
    ids: list[Identity]
    anchors: set[str]
    boundary: bool                # tick mark sitting on a graph node = drawn DN boundary evidence
    strong: bool = True           # leader met the line itself (weak = label points at a symbol / marker / fitting)


def _chain_nodes(g: PipeGraph, c: list[int]) -> list[int]:
    """Ordered node ids along a chain: node i sits between primitives c[i-1] and c[i] (len == len(c) + 1)."""
    if len(c) == 1:
        a, b = g.prim_nodes[c[0]]
        return [a, b]
    inner = []
    for i in range(1, len(c)):
        shared = set(g.prim_nodes[c[i - 1]]) & set(g.prim_nodes[c[i]])
        inner.append(min(shared) if shared else g.prim_nodes[c[i]][0])
    first = [n for n in g.prim_nodes[c[0]] if n != inner[0]]
    last = [n for n in g.prim_nodes[c[-1]] if n != inner[-1]]
    return [first[0] if first else inner[0]] + inner + [last[0] if last else inner[-1]]


def _dead_end(g: PipeGraph, nid: int, chain_set: set[int], pidx: GridIndex) -> bool:
    """A chain end is a dead end when its node has degree 1 and no other primitive of the family has an endpoint
    ahead of it (within 2.5 gap modes, inside a 60 degree cone): the drawn line really stops here."""
    n = g.nodes[nid]
    if n.degree != 1:
        return False
    q = g.prims[n.prims[0]]
    far = q.b if dist(q.a, (n.x, n.y)) < dist(q.b, (n.x, n.y)) else q.a
    L = dist(far, (n.x, n.y))
    if L < 1e-9:
        return False
    ux, uy = (n.x - far[0]) / L, (n.y - far[1]) / L
    R = max(2.5 * (g.gap_mode or 0.0), 6.0)
    for pid2 in pidx.query((n.x - R, n.y - R, n.x + R, n.y + R)):
        if pid2 in chain_set:
            continue
        r = g.prims[pid2]
        for ep in (r.a, r.b):
            vx, vy = ep[0] - n.x, ep[1] - n.y
            dd = math.hypot(vx, vy)
            if dd <= R and (dd < 1e-9 or (vx * ux + vy * uy) >= 0.5 * dd):
                return False
    return True


def _chain_seed_groups(g: PipeGraph, c: list[int], nodes: list[int], seeds) -> list[_SeedGroup]:
    by_pos: dict[float, list[tuple[Identity, str, bool, bool]]] = defaultdict(list)
    for k, pid in enumerate(c):
        for ident, aid, kind, pt in seeds.get(pid, []):
            pos, boundary = k + 0.5, False
            strong = kind in STRONG_KINDS
            if kind in TICK_KINDS:
                n0, n1 = g.nodes[nodes[k]], g.nodes[nodes[k + 1]]
                d0 = dist(pt, (n0.x, n0.y)); d1 = dist(pt, (n1.x, n1.y))
                if min(d0, d1) <= _R("pipes.ownership.BOUNDARY_TOL", BOUNDARY_TOL):
                    pos, boundary = (float(k) if d0 <= d1 else float(k + 1)), True
                else:
                    # tick beyond the primitive's end (in a dash gap): boundary at the node on that side
                    q = g.prims[pid]
                    _, t = point_seg_distance(pt[0], pt[1], q.seg)
                    a_is_k = dist(q.a, (n0.x, n0.y)) <= dist(q.a, (n1.x, n1.y))
                    if t <= 0.02:
                        pos, boundary = (float(k) if a_is_k else float(k + 1)), True
                    elif t >= 0.98:
                        pos, boundary = (float(k + 1) if a_is_k else float(k)), True
            by_pos[pos].append((ident, aid, boundary, strong))
    out: list[_SeedGroup] = []
    for pos in sorted(by_pos):
        lst = by_pos[pos]
        ids = [i for i, _, _, _ in lst]
        out.append(_SeedGroup(pos=pos, merged=_merge_identity(ids), ids=ids, anchors={a for _, a, _, _ in lst},
                              boundary=all(b for _, _, b, _ in lst), strong=any(s for _, _, _, s in lst)))
    return out


def _drop_overridden_symbol_labels(gs: list[_SeedGroup]) -> tuple[list[_SeedGroup], list[_SeedGroup]]:
    """Labels pointing at a symbol (riser mark, fitting) describe that object; where labels on the line itself
    carry the same system with another DN, the symbol labels do not seed the run."""
    strong_ids = [i for grp in gs if grp.strong for i in grp.ids]
    if not strong_ids:
        return gs, []
    smerge = _merge_identity(strong_ids)
    if smerge is None:
        return gs, []
    kept, dropped = [], []
    for grp in gs:
        if (not grp.strong and grp.merged is not None and grp.merged.base == smerge.base
                and _merge_identity([smerge, grp.merged]) is None):
            dropped.append(grp)
        else:
            kept.append(grp)
    return kept, dropped


def _outward_compatible(gs: list[_SeedGroup], i: int, side: int) -> bool:
    """All seed groups beyond group i (towards the chain end on `side`) agree with group i."""
    others = gs[:i] if side < 0 else gs[i + 1:]
    return _merge_identity([gs[i].merged] + [x for grp in others for x in grp.ids]) is not None


NO_END_EVIDENCE = "UNLABELLED_BRANCH_WITHOUT_END_EVIDENCE"


def _branch_support(g: PipeGraph, st: dict[int, PrimState], ch, chain_nodes, ci: int, nid: int, ident: Identity,
                    end_evidence) -> str:
    """Det positiva bevis en onämnd gren behöver för att ta korsningens namn.

    Att en linje rör vid en annan är ingen anslutning. En måttlinje, en väggkant, en fixturs kontur på samma
    penna når fram till ledningen precis som en gren gör, och en läsning som ger varje sådan kontakt ett namn
    mäter byggnaden som rör. Så grenen ska sluta i något: en komponent, bladets kant, en annan pennas
    fortsättning, en annan pennas bläck - eller nå ett stråk som redan bär samma identitet. Slutar den i tomma
    luften finns inget som säger att den är ett rör, och den blir tvetydig med korsningens namn som kandidat:
    metrarna redovisas, men som en fråga, inte som ett svar.
    """
    chain_prims = set(ch[ci])
    for far in (chain_nodes[ci][0], chain_nodes[ci][-1]):
        if far == nid:
            continue
        fn = g.nodes[far]
        if fn.degree >= 3:
            for p in fn.prims:
                if p in chain_prims:
                    continue
                sp = st[p]
                if sp.state == "CONFIRMED" and sp.identity is not None and sp.identity.compatible(ident):
                    return f"branch_reaches_a_run_of_the_same_identity_at_node_{far}"
        elif fn.degree == 1:
            ev = (end_evidence or {}).get(g.family, {}).get(far)
            if ev and ev.get("kind") in BRANCH_END_EVIDENCE:
                return f"branch_ends_at_{ev['kind']}_node_{far}"
    return ""


def _wye_outlet(g, st, chain, nodes):
    """A straight outlet between two consistently directed wyes.

    At the upstream wye both labelled arms approach the outlet at the same
    smaller size. At the downstream wye the straight continuation states the
    larger size. The drawn junction supplies a size boundary; an ordinary tee,
    crossing, opposed wyes or differing materials supplies no such evidence.
    """
    ends = (nodes[0], nodes[-1])
    if ends[0] == ends[1] or any(g.nodes[n].degree != 3 for n in ends):
        return None
    a, b = (g.nodes[n] for n in ends)
    length = math.hypot(b.x-a.x, b.y-a.y)
    if length < 1e-6:
        return None
    dx, dy = (b.x-a.x)/length, (b.y-a.y)/length
    line_angle = math.degrees(math.atan2(dy, dx)) % 180
    if any(angle_diff(g.prims[i].seg.angle, line_angle) > 3 for i in chain):
        return None
    chain_set = set(chain)
    end_arms = []
    for side, nid in enumerate(ends):
        n = g.nodes[nid]
        toward = (dx,dy) if side == 0 else (-dx,-dy)
        arms = []
        for i in n.prims:
            if i in chain_set:
                continue
            state = st[i]
            if state.state != "CONFIRMED" or state.identity is None or state.identity.dn is None:
                return None
            other_nid = next((k for k in g.prim_nodes[i] if k != nid), None)
            if other_nid is None:
                return None
            other = g.nodes[other_nid]
            vx, vy = other.x-n.x, other.y-n.y
            size = math.hypot(vx,vy)
            if size < .15:
                return None
            arms.append(((vx*toward[0]+vy*toward[1])/size, state))
        if len(arms) != 2:
            return None
        end_arms.append(arms)
    for upstream, downstream in (end_arms, end_arms[::-1]):
        straight_up = [s for dot,s in upstream if dot < -.998]
        branch_up = [s for dot,s in upstream if -.94 < dot < -.34]
        straight_down = [s for dot,s in downstream if dot < -.998]
        branch_down = [s for dot,s in downstream if .34 < dot < .94]
        if not all(len(v) == 1 for v in (straight_up,branch_up,straight_down,branch_down)):
            continue
        small, peer, big, branch = [v[0].identity for v in (straight_up,branch_up,straight_down,branch_down)]
        if (small == peer and big.base == small.base == branch.base
                and small.dn < big.dn and branch.dn <= big.dn):
            aids = set().union(*(s.anchors for _,s in upstream+downstream))
            return big, aids
    return None


def _resolve_family(g: PipeGraph, st: dict[int, PrimState], seeds, ambiguous_runs, fk: str, end_evidence=None) -> None:
    ch = graph_chains(g)
    chain_of: dict[int, int] = {}
    for ci, c in enumerate(ch):
        for pid in c:
            chain_of[pid] = ci
    chain_nodes = [_chain_nodes(g, c) for c in ch]
    pidx = GridIndex(cell=12.0)
    for pid, q in g.prims.items():
        pidx.insert(pid, q.seg.bbox())
    dead_cache: dict[tuple[int, int], bool] = {}

    def dead(ci: int, nid: int) -> bool:
        key = (ci, nid)
        if key not in dead_cache:
            dead_cache[key] = _dead_end(g, nid, set(ch[ci]), pidx)
        return dead_cache[key]

    def confirm(pids, ident: Identity, reason: str, aids: set[str]) -> None:
        for pid in pids:
            s = st[pid]
            s.state, s.identity, s.reason, s.candidates = "CONFIRMED", ident, reason, set()
            s.anchors |= aids

    def ambiguous(pids, ids, aids: set[str], reason: str) -> None:
        cands = {i for i in ids if i is not None}
        for pid in pids:
            s = st[pid]
            s.state, s.identity, s.candidates, s.reason = "AMBIGUOUS", None, cands, reason
            s.anchors |= aids

    groups_of: dict[int, list[_SeedGroup]] = {}
    # 1. chains with seeds. A tick mark on a node is drawn boundary evidence: between two incompatible seed
    #    groups the DN changes at a tick, and the identity whose run ends at a dead end is delimited by its own tick.
    for ci, c in enumerate(ch):
        nodes = chain_nodes[ci]
        gs = _chain_seed_groups(g, c, nodes, seeds)
        if not gs:
            continue
        gs, dropped = _drop_overridden_symbol_labels(gs)
        for grp in dropped:
            for pid in c:
                st[pid].evidence.append(f"symbol_label_{grp.merged.key}_describes_riser_or_fitting_not_the_run")
        groups_of[ci] = gs
        all_ids = [i for grp in gs for i in grp.ids]
        anchors_all = set().union(*(grp.anchors for grp in gs))
        whole = _merge_identity(all_ids)
        if whole is not None:
            # Mellan etiketterna är kedjan röret, vad den än är ritad med: två etiketter som är överens om en
            # sträcka är beviset för att den fortsätter där ritsättet byter. Utanför den yttersta etiketten
            # finns inget sådant bevis, och där stannar namnet vid den första långa heldragna linjen.
            # en grupp kan stå på en nod i kedjans ände: läget klipps till kedjans egna primitiver
            pos = [min(len(c) - 1, max(0, int(grp.pos))) for grp in gs]
            lo, hi = min(pos), max(pos)
            head, middle, tail = c[:lo], c[lo:hi + 1], c[hi + 1:]
            kept = (_up_to_transition(g, head, from_end=True, seeded_solid=g.prims[c[lo]].solid_long) + middle
                    + _up_to_transition(g, tail, from_end=False, seeded_solid=g.prims[c[hi]].solid_long))
            confirm(kept, whole, "chain_with_agreeing_anchors" if len(gs) > 1 else "chain_from_anchor", anchors_all)
            for pid in c:
                if pid not in kept:
                    st[pid].evidence.append(TRANSITION_EVIDENCE)
            continue

        def prims_between(lo: float, hi: float) -> list[int]:
            return [c[k] for k in range(len(c)) if lo < k + 0.5 < hi]

        for grp in gs:
            if grp.boundary:
                continue
            pid = c[int(grp.pos)]
            if grp.merged is not None:
                confirm([pid], grp.merged, "chain_from_anchor", grp.anchors)
            else:
                ambiguous([pid], grp.ids, grp.anchors, _conflict_reason(grp.ids))
        # A closed chain has no free ends: what looks like the run before the first seed and the run after the
        # last is one span that joins them, and it sits between two seeds like any other. Confirming both halves
        # from their nearest seed would hand that span to whichever seed the chain happens to start at.
        closed = nodes[0] == nodes[-1] and len(c) > 1
        first, last = gs[0], gs[-1]
        if closed:
            wrap = prims_between(last.pos, len(c) + 1.0) + prims_between(-1.0, first.pos)
            joined = _merge_identity(first.ids + last.ids)
            if wrap:
                if joined is not None:
                    confirm(wrap, joined, "closed_chain_between_agreeing_anchors", first.anchors | last.anchors)
                else:
                    reason = _conflict_reason(first.ids + last.ids)
                    ambiguous(wrap, first.ids + last.ids, first.anchors | last.anchors, reason)
                    ambiguous_runs.append({"family": fk, "chain": ci, "from_prim": wrap[0], "to_prim": wrap[-1],
                                           "reason": reason,
                                           "identities": sorted({i.key for i in first.ids + last.ids})})
        else:
            if first.merged is not None:
                before = prims_between(-1.0, first.pos)
                kept = _up_to_transition(g, before, from_end=True, seeded_solid=g.prims[c[min(len(c) - 1, max(0, int(first.pos)))]].solid_long)
                confirm(kept, first.merged, "chain_before_first_anchor", first.anchors)
                for pid in before[:len(before) - len(kept)]:
                    st[pid].evidence.append(TRANSITION_EVIDENCE)
            if last.merged is not None:
                after = prims_between(last.pos, len(c) + 1.0)
                kept = _up_to_transition(g, after, from_end=False, seeded_solid=g.prims[c[min(len(c) - 1, max(0, int(last.pos)))]].solid_long)
                confirm(kept, last.merged, "chain_after_last_anchor", last.anchors)
                for pid in after[len(kept):]:
                    st[pid].evidence.append(TRANSITION_EVIDENCE)
        for gi in range(len(gs) - 1):
            A, B = gs[gi], gs[gi + 1]
            pids = prims_between(A.pos, B.pos)
            between = _merge_identity(A.ids + B.ids)
            if between is not None:
                confirm(pids, between, "chain_between_agreeing_anchors", A.anchors | B.anchors)
                continue
            owner = None
            aids: set[str] = set()
            reason = ""
            if A.merged is not None and B.merged is not None:
                if B.boundary and not A.boundary:
                    owner, aids, reason = A.merged, A.anchors, "dn_boundary_at_tick"
                elif A.boundary and not B.boundary:
                    owner, aids, reason = B.merged, B.anchors, "dn_boundary_at_tick"
                elif A.boundary and B.boundary:
                    term_a = _outward_compatible(gs, gi, -1) and dead(ci, nodes[0])
                    term_b = _outward_compatible(gs, gi + 1, +1) and dead(ci, nodes[-1])
                    if term_b and not term_a:
                        owner, aids, reason = A.merged, A.anchors, "dn_boundary_at_tick_before_dead_end"
                    elif term_a and not term_b:
                        owner, aids, reason = B.merged, B.anchors, "dn_boundary_at_tick_before_dead_end"
                    elif A.merged.dn is not None and B.merged.dn is not None and A.merged.dn != B.merged.dn and A.merged.base == B.merged.base:
                        # a reduction is drawn at the tick of the smaller size: the larger size runs up to it
                        big, small = (A, B) if A.merged.dn > B.merged.dn else (B, A)
                        owner, aids, reason = big.merged, big.anchors, "dn_boundary_at_smaller_dn_tick"
            if not pids:
                continue
            if owner is not None:
                confirm(pids, owner, reason, aids)
            else:
                reason = _conflict_reason(A.ids + B.ids)
                ambiguous(pids, A.ids + B.ids, A.anchors | B.anchors, reason)
                ambiguous_runs.append({"family": fk, "chain": ci, "from_prim": pids[0], "to_prim": pids[-1], "reason": reason,
                                       "identities": sorted({i.key for i in A.ids + B.ids})})

    def tick_delimited_stub(v: int, nid: int, ident: Identity) -> tuple[list[int], bool] | None:
        """Primitives of v's chain that the junction's identity may take over: up to the nearest tick boundary when
        the chain carries labels on the line; the whole chain when it carries only symbol labels (risers) or
        DN-less labels. All seeds must agree with ident. Also returns whether the far end is a dead end."""
        ci = chain_of[v]
        gs = groups_of.get(ci)
        if not gs or _merge_identity([ident] + [x for grp in gs for x in grp.ids]) is None:
            return None
        nodes = chain_nodes[ci]
        c = ch[ci]
        if nodes[0] == nodes[-1]:
            return None
        if nodes[0] == nid:
            side = 0
        elif nodes[-1] == nid:
            side = 1
        else:
            return None
        if not any(grp.strong for grp in gs) or ident.dn is None:
            return list(c), dead(ci, nodes[-1] if side == 0 else nodes[0])
        bpos = [grp.pos for grp in gs if grp.boundary]
        if not bpos:
            return None
        b = int(min(bpos) if side == 0 else max(bpos))
        pids = [c[k] for k in (range(0, b) if side == 0 else range(b, len(c)))]
        if not pids:
            return None
        return pids, dead(ci, nodes[-1] if side == 0 else nodes[0])

    def spread_into_unowned_chains() -> bool:
        """Vad en onämnd kedja får heta, vägt mot varje korsning den möter - inte bara den första.

        En vågrät förbindelse mellan ett DN20-stråk och ett DN40-stråk möter två korsningar och har ingen egen
        dimensionsgräns. Läsningen tog identiteten från den korsning den råkade nå först, och vilken det blev
        följde primitivernas numrering: samma ritning kunde ge DN20 eller DN40, båda som CONFIRMED. Det är två
        fel i ett - mängden blev olika för samma ritning, och den blev säker på något ritningen inte säger.

        Så stödet samlas först. Kedjans båda ändnoder får lämna sina bekräftade identiteter, och bara om allt
        stöd pekar åt samma håll tar kedjan det namnet. Pekar det åt två håll är kedjan tvetydig tills ritningen
        ger en gräns eller en identitet - en tick, en etikett - och det är ett ärligare svar än ett tal.

        Körs efter att korsningsslingan lagt sig, så att grannarna är färdigbestämda när kedjan vägs. Att den
        körs om medan något ändras är vad som låter ett avgjort stråk föra namnet vidare genom nätet.
        """
        touched = False
        for nid in sorted(g.nodes):
            n = g.nodes[nid]
            if n.degree < 3:
                continue
            arms = sorted(n.prims)
            resolved = [p for p in arms if st[p].state == "CONFIRMED"]
            unresolved = [p for p in arms if st[p].state == "UNOWNED"
                          or (st[p].state == "AMBIGUOUS" and st[p].reason == NO_END_EVIDENCE)]
            if not resolved or not unresolved:
                continue
            for u in unresolved:
                # En stump kortare än kontakttoleransen får inte ta ett namn och bära det vidare. Den är inget
                # ritat rör, och som brygga tar den namnet över glapp som aldrig var anslutningar.
                if _too_short_to_carry(g, ch, chain_of[u]):
                    continue
                ci = chain_of[u]
                # collinear resolved partner?
                partners = [p for p in resolved if angle_diff(g.prims[p].seg.angle, g.prims[u].seg.angle) <= 3.0]
                idents = {st[p].identity for p in partners}
                if len(idents) == 1:
                    ident = next(iter(idents))
                    # Collect contradictory evidence BEFORE assigning any part.
                    # Straight continuation at the first tee is not evidence of
                    # an invisible reducer before a differently labelled tee.
                    existing = [st[p].identity for p in ch[ci]
                                if st[p].state == "CONFIRMED" and st[p].identity is not None]
                    existing += [i for group in groups_of.get(ci, []) for i in group.ids]
                    for end in {chain_nodes[ci][0], chain_nodes[ci][-1]}:
                        end_arm = ch[ci][0] if end == chain_nodes[ci][0] else ch[ci][-1]
                        for p in g.nodes[end].prims:
                            if p in ch[ci] or st[p].state != "CONFIRMED" or st[p].identity is None:
                                continue
                            if angle_diff(g.prims[p].seg.angle, g.prims[end_arm].seg.angle) <= 3.0:
                                existing.append(st[p].identity)
                    conflicts = [other for other in existing if not ident.compatible(other)]
                    if conflicts:
                        wye = None if groups_of.get(ci) else _wye_outlet(g, st, ch[ci], chain_nodes[ci])
                        if wye is not None:
                            owner, aids = wye
                            confirm([pid for pid in ch[ci] if st[pid].state == "UNOWNED"],
                                    owner, "outlet_between_consistently_directed_wyes", aids)
                            touched = True
                            continue
                        for pid in ch[ci]:
                            if st[pid].state == "UNOWNED":
                                st[pid].state = "AMBIGUOUS"
                                st[pid].candidates = {ident, *conflicts}
                                st[pid].reason = _conflict_reason([ident, *conflicts])
                                st[pid].evidence.append("contradictory_collinear_end_evidence")
                                touched = True
                        continue
                    # extend along u's chain until a junction/terminal - or until the line stops being drawn
                    # the way the pipe is: a long solid line in a dashed family is not this pipe going on
                    reach = _up_to_transition(g, ch[ci], from_end=(ch[ci] and ch[ci][-1] == u),
                                              seeded_solid=all(g.prims[p].solid_long for p in partners))
                    for pid in ch[ci]:
                        s = st[pid]
                        if pid not in reach:
                            if s.state == "UNOWNED":
                                s.evidence.append(TRANSITION_EVIDENCE)
                            continue
                        if s.state == "UNOWNED":
                            s.state, s.identity, s.reason = "CONFIRMED", ident, "collinear_through_junction"
                            s.anchors |= set().union(*(st[p].anchors for p in partners))
                            s.evidence.append(f"straight_through_node_{nid}")
                    touched = True
                    continue
                # Allt stöd kedjan har, från varje korsning den möter. Den lokala noden ensam räcker inte: en
                # förbindelse mellan två dimensioner ser en enda kandidat vid var ände och skulle bekräftas av
                # båda, var för sig, till olika svar.
                chain_prims = set(ch[ci])
                cands: set = set()
                aids: set[str] = set()
                # Hur många armar varje kandidat har vid kedjans ändar. En ledning som *slutar* där lägger en
                # arm i noden; en som passerar lägger två. Skillnaden avgör vad förbindelsen är - se nedan.
                arms_of: Counter = Counter()
                for end in {chain_nodes[ci][0], chain_nodes[ci][-1]}:
                    for p in g.nodes[end].prims:
                        if p in chain_prims:
                            continue
                        sp = st[p]
                        if sp.state == "CONFIRMED" and sp.identity is not None:
                            cands.add(sp.identity)
                            aids |= sp.anchors
                            arms_of[sp.identity] += 1
                if not cands:
                    continue
                # En korsning är ingen anslutning. Två rör kan korsa varandra utan att mötas, och en gren
                # skapas aldrig bara för att linjer korsas - det är ritningsläsningens egen regel, och det
                # ritade beviset för den står i noden: en gren tar slut där den grenar av, medan en linje
                # som bara passerar fortsätter rakt ut på andra sidan som ännu en onämnd arm.
                #
                # Men det räcker inte att de är parallella. En linje som passerar går IN på ena sidan av
                # noden och UT på den andra - de två armarna pekar åt var sitt håll. Två onämnda armar som
                # pekar åt SAMMA håll är samma linje ritad två gånger, eller en stump ovanpå ledningen, och
                # där finns ingenting som passerar.
                passes_through = any(q != u
                                     and angle_diff(g.prims[q].seg.angle, g.prims[u].seg.angle) <= 3.0
                                     and _opposite_sides(g, n, q, u)
                                     for q in unresolved)
                if passes_through:
                    # Den som passerar är ingen gren, och den får inte heller korsningens namn som kandidat. Som
                    # tvetydig med det namnet räknades den inte längre som en linje som passerar vid nästa
                    # korsning, och den andra läsaren gjorde ett ensamt kandidatnamn till namn
                    # (host_reading_single_candidate): på ett sektionsblad slutade ett rörs två kanter mot en
                    # värmepumps vägg, och väggen tog rörets namn och förde det runt hela höljet.
                    continue
                only = next(iter(cands)) if len(cands) == 1 else None
                if only is not None and only.dn is not None and not groups_of.get(ci):
                    support = _branch_support(g, st, ch, chain_nodes, ci, nid, only, end_evidence)
                    if support:
                        reach = set(_up_to_transition(g, ch[ci], from_end=(ch[ci] and ch[ci][-1] == u)))
                        for pid in ch[ci]:
                            s = st[pid]
                            if pid not in reach:
                                if s.state == "UNOWNED":
                                    s.evidence.append(TRANSITION_EVIDENCE)
                                continue
                            if s.state == "UNOWNED" or (s.state == "AMBIGUOUS" and s.reason == NO_END_EVIDENCE):
                                s.state, s.identity, s.reason = "CONFIRMED", only, "unlabeled_branch_takes_the_only_junction_identity"
                                s.candidates = set()
                                s.anchors |= aids
                                s.evidence.append(f"single_candidate_at_node_{nid}")
                                s.evidence.append(support)
                        touched = True
                        continue
                    # ingen ände som säger att grenen är ett rör: namnet blir en kandidat, inte ett svar
                    fresh = [pid for pid in ch[ci] if st[pid].state == "UNOWNED"]
                    if not fresh:
                        continue                     # redan tvetydig av samma skäl: inget nytt att säga
                    for pid in fresh:
                        s = st[pid]
                        s.state, s.candidates, s.reason = "AMBIGUOUS", {only}, NO_END_EVIDENCE
                        s.anchors |= aids
                        s.evidence.append(f"single_candidate_at_node_{nid}")
                        s.evidence.append("branch_end_has_no_evidence_of_a_pipe")
                    ambiguous_runs.append({"family": fk, "chain": ci, "from_prim": ch[ci][0], "to_prim": ch[ci][-1],
                                           "reason": NO_END_EVIDENCE, "identities": [only.key]})
                    touched = True
                    continue
                else:
                    # Kandidaterna är oense - men ofta bara om storleken. Samma system, samma material, två
                    # dimensioner: det är inte två rör som möts, det är ett rör som byter dimension. En
                    # mängdare som tittar på en sådan koppling tvekar inte, och läsningen ska inte heller
                    # göra det. Den sa "kunde tillhöra S1-P2|DN110 eller S1-P2|DN160, ritningen avgör det
                    # inte" om böjen mellan just de stråken, och lämnade den omätt.
                    #
                    # Ritningen avgör den. En dimension byts vid en del - en övergång, en förminskning - och
                    # den delen ritas. Där ingen sådan del står ritad har stråket inte bytt dimension än, och
                    # det som fortsätter är det grövre. Det är också den riktning felet ligger åt mätt över
                    # korpusen (FYND §13): den grövsta dimensionen i en stam är den som saknar mest.
                    #
                    # Villkoret är smalt: en enda stam, var kandidat sin egen dimension. Möts två system, två
                    # material eller två likadana dimensioner säger ritningen ingenting och svaret förblir
                    # "tvetydig" - det är fortfarande ett giltigt svar, bara inte här.
                    #
                    # Och det gäller bara när det verkligen är *ett* stråk. Två lodräta ledningar bredvid
                    # varandra, DN20 och DN40, med en vågrät förbindelse emellan är inte ett rör som byter
                    # dimension - det är en koppling mellan två olika rör, och vilken av dem den hör till
                    # säger ritningen inte. Den skillnaden står i noderna: en ledning som slutar vid
                    # förbindelsen lägger en arm där, en som bara passerar lägger två. Slutar båda - en ände
                    # mot en ände - är det ett stråk med en böj. Passerar de är det en pinne i en stege, och
                    # svaret förblir tvetydigt.
                    coarser = None
                    if (len(cands) > 1 and len({c.stem for c in cands}) == 1
                            and all(c.dn is not None for c in cands)
                            and len({c.dn for c in cands}) == len(cands)
                            and all(arms_of[c] == 1 for c in cands)):
                        coarser = max(cands, key=lambda c: c.dn)
                    if coarser is not None:
                        reach = set(_up_to_transition(g, ch[ci], from_end=(ch[ci] and ch[ci][-1] == u)))
                        took = [pid for pid in ch[ci] if pid in reach and st[pid].state == "UNOWNED"]
                        confirm(took, coarser, "same_run_changes_size_the_coarser_carries_on", aids)
                        for pid in took:
                            st[pid].evidence.append(f"one_run_two_sizes_at_node_{nid}")
                        for pid in ch[ci]:
                            if pid not in reach and st[pid].state == "UNOWNED":
                                st[pid].evidence.append(TRANSITION_EVIDENCE)
                    else:
                        for pid in ch[ci]:
                            s = st[pid]
                            if s.state == "UNOWNED":
                                s.state, s.candidates, s.reason = "AMBIGUOUS", set(cands), "AMBIGUOUS_BRANCH"
                                s.evidence.append(f"unlabeled_branch_at_node_{nid}")
                        if len(cands) > 1:
                            ambiguous_runs.append({"family": fk, "chain": ci, "from_prim": ch[ci][0],
                                                   "to_prim": ch[ci][-1], "reason": "AMBIGUOUS_BRANCH",
                                                   "identities": sorted({i.key for i in cands})})
                touched = True
        return touched

    # 2. junction resolution (iterative): the junction's identity flows into a labeled arm up to its tick boundary;
    #    collinear continuation into unowned arms; other unlabeled arms ambiguous
    changed = True
    rounds = 0
    while changed and rounds < 100:
        changed = False
        rounds += 1
        for nid in sorted(g.nodes):
            n = g.nodes[nid]
            if n.degree < 3:
                continue
            arms = sorted(n.prims)
            resolved = [p for p in arms if st[p].state == "CONFIRMED" and st[p].identity is not None]
            # arms whose far end is a dead end and which carry a tick / symbol / DN-less label are stubs; the arms
            # that continue into the network are the sources of the junction's DN. A stub takes the sources' DN
            # up to its drawn tick (a DN change is drawn as a tick; riser labels describe the riser, not the run).
            info = {}
            for v in resolved:
                res = tick_delimited_stub(v, nid, st[v].identity)
                if res:
                    info[v] = res
            def is_collinear(a: int, b: int) -> bool:
                return angle_diff(g.prims[a].seg.angle, g.prims[b].seg.angle) <= 3.0

            # a stub is a dead-end BRANCH (not part of a straight run through the junction)
            stubs = {v for v, (pids, dead_far) in info.items() if dead_far and not any(is_collinear(v, p) for p in arms if p != v)}
            sources = [p for p in resolved if p not in stubs]
            src_aids = set().union(*(st[p].anchors for p in sources)) if sources else set()

            X = _merge_identity([st[p].identity for p in sources]) if sources else None
            if X is not None and X.dn is not None:
                for v in sorted(stubs):
                    Y = st[v].identity
                    if X.base != Y.base:
                        continue
                    merged = _merge_identity([X, Y])
                    if merged is not None and Y.dn is not None:
                        continue
                    if merged is None and not flows_into_other_size(X, Y):
                        continue      # den klenare får inte ta den grövres arm
                    if any(is_collinear(p, v) for p in arms if p != v) and not any(is_collinear(p, v) for p in sources):
                        continue    # straight run through the junction: only its own run may flow into it
                    pids, _ = info[v]
                    todo = [p for p in pids if st[p].state == "CONFIRMED" and st[p].identity == Y]
                    if not todo:
                        continue
                    if merged is not None:
                        reason = "junction_dn_completes_dn_less_label"
                    elif any(grp.strong for grp in groups_of.get(chain_of[v], [])):
                        reason = "through_junction_up_to_tick_boundary"
                    else:
                        reason = "junction_dn_over_symbol_labels"
                    confirm(todo, merged if merged is not None else X, reason, src_aids)
                    for p in todo:
                        st[p].evidence.append(f"junction_{nid}_identity_flows_into_stub")
                    changed = True
            # continuing arms with a tick boundary: the identity of the OTHER sources flows up to the tick when the
            # arm is the straight continuation of one of them; otherwise undecidable
            flows = []
            for v in sorted(info):
                if v in stubs:
                    continue
                others = [p for p in sources if p != v]
                if not others:
                    continue
                Xv = _merge_identity([st[p].identity for p in others])
                Y = st[v].identity
                if Xv is None or Xv.dn is None or Xv.base != Y.base:
                    continue
                merged = _merge_identity([Xv, Y])
                if merged is not None and Y.dn is not None:
                    continue
                if merged is None and not flows_into_other_size(Xv, Y):
                    continue          # den klenare får inte ta den grövres arm
                pids, _ = info[v]
                collinear = any(is_collinear(p, v) for p in others)
                if any(is_collinear(p, v) for p in arms if p != v) and not collinear:
                    continue
                flows.append((v, merged if merged is not None else Xv, Y, pids, collinear, set().union(*(st[p].anchors for p in others)), merged is not None))
            decidable = [f for f in flows if f[4]]
            undecidable = [f for f in flows if not f[4] and not f[6]]
            if len(decidable) >= 2:
                # competing tick boundaries on collinear arms: the run that ends at a dead end is delimited by its
                # own tick (the other identity flows up to it); with no or several dead ends it is undecidable
                dead_arms = [f for f in decidable if info[f[0]][1]]
                if len(dead_arms) == 1:
                    decidable = dead_arms
                else:
                    bigger = [f for f in decidable if f[1].dn is not None and f[2].dn is not None and f[1].dn > f[2].dn]
                    if len(bigger) == 1:
                        decidable = bigger      # the larger size runs up to the smaller size's tick
            if len(decidable) == 1:
                v, X, Y, pids, _, aids, completion = decidable[0]
                todo = [p for p in pids if st[p].state == "CONFIRMED" and st[p].identity == Y]
                if todo:
                    reason = "junction_dn_completes_dn_less_label" if completion else "through_junction_up_to_tick_boundary"
                    confirm(todo, X, reason, aids)
                    for p in todo:
                        st[p].evidence.append(f"junction_{nid}_identity_flows_into_arm")
                    changed = True
            elif len(decidable) >= 2:
                for v, X, Y, pids, _, aids, completion in decidable:
                    todo = [p for p in pids if st[p].state == "CONFIRMED" and st[p].identity == Y]
                    if todo:
                        ambiguous(todo, [X, Y], aids, "AMBIGUOUS_DN_BOUNDARY")
                        for p in todo:
                            st[p].evidence.append(f"competing_tick_boundaries_at_junction_{nid}")
                        ambiguous_runs.append({"family": fk, "chain": chain_of[v], "from_prim": todo[0], "to_prim": todo[-1],
                                               "reason": "AMBIGUOUS_DN_BOUNDARY", "identities": sorted({X.key, Y.key})})
                        changed = True
            for v, X, Y, pids, _, aids, completion in undecidable:
                # a tick in the middle of a branch that continues into the network: the label points at this
                # branch; the branch keeps its own label (the tick is the leader's pointer, not a DN change)
                for p in pids:
                    if f"tick_on_branch_at_junction_{nid}_taken_as_label_pointer" not in st[p].evidence:
                        st[p].evidence.append(f"tick_on_branch_at_junction_{nid}_taken_as_label_pointer")
            # Onämnda armar avgörs inte här. Se `spread_into_unowned_chains`: en kedja som möter två
            # korsningar ska vägas mot båda, och det går bara när grannarna slutat ändra sig.
            continue
        # När korsningarnas egna regler lagt sig för den här omgången vägs de onämnda kedjorna mot allt stöd de
        # har. Ändrade det något körs korsningarna om, så ett namn kan bäras vidare genom nätet.
        if not changed and spread_into_unowned_chains():
            changed = True

def _build_pipes(g: PipeGraph, st: dict[int, PrimState], fk: str, page: int, stop_nodes: set[int] | None = None) -> list[PhysicalPipe]:
    pipes: list[PhysicalPipe] = []
    visited: set[int] = set()
    node_gap = {b["from_node"]: b["gap_pt"] for b in g.bridges}
    for pid in sorted(g.prims):
        s = st[pid]
        if s.state != "CONFIRMED" or pid in visited:
            continue
        comp = []
        dq = deque([pid])
        visited.add(pid)
        while dq:
            p = dq.popleft()
            comp.append(p)
            for node in g.prim_nodes[p]:
                if stop_nodes and node in stop_nodes:
                    continue
                for q in g.nodes[node].prims:
                    if q != p and q not in visited and st[q].state == "CONFIRMED" and st[q].identity == s.identity \
                            and st[q].tentative == s.tentative:
                        visited.add(q)
                        dq.append(q)
        comp.sort()
        prims = [g.prims[p] for p in comp]
        raw = sum(q.seg.length for q in prims)
        nodes = sorted({n for p in comp for n in g.prim_nodes[p]})
        compset = set(comp)
        gap = 0.0
        for n in nodes:
            if n not in node_gap:continue
            adjacent=g.nodes[n].prims
            if all(q in compset for q in adjacent):
                gap += node_gap[n]
            elif stop_nodes and n in stop_nodes and all(
                    st[q].state == "CONFIRMED" and st[q].identity == s.identity for q in adjacent):
                # A measurement boundary inside an already proven dash bridge
                # divides its length; it must neither drop nor duplicate it.
                gap += node_gap[n] * sum(q in compset for q in adjacent) / len(adjacent)
        polylines = _order_polylines(g, comp)
        anchors = sorted({a for p in comp for a in st[p].anchors})
        evidence = sorted({st[p].reason for p in comp})
        ppid = stable_id("pp", page, fk, s.identity.key, *(f"{g.prims[p].pid}#{g.prims[p].seg_index}" for p in comp[:8]), len(comp))
        pipes.append(PhysicalPipe(physical_pipe_id=ppid, page=page, family=fk, identity=s.identity, anchor_ids=anchors,
                                  prim_ids=comp, points=polylines, source_paths=sorted({q.pid for q in prims}),
                                  source_segments=[f"{q.pid}#{q.seg_index}" for q in prims], nodes=nodes,
                                  raw_length_pt=raw, bridged_gap_pt=gap, frontier_reasons=[], evidence=evidence,
                                  needs_review=s.tentative))
    return pipes


def _order_polylines(g: PipeGraph, comp: list[int]) -> list[list[tuple[float, float]]]:
    compset = set(comp)
    used: set[int] = set()
    out: list[list[tuple[float, float]]] = []

    def comp_degree(n):
        return sum(1 for p in g.nodes[n].prims if p in compset)

    starts = sorted(p for p in comp if any(comp_degree(n) != 2 for n in g.prim_nodes[p]))
    for start in starts + sorted(comp):
        if start in used:
            continue
        a, b = g.prim_nodes[start]
        if comp_degree(a) != 2:
            cur_node, far = a, b
        else:
            cur_node, far = b, a
        pts = [(g.nodes[cur_node].x, g.nodes[cur_node].y)]
        p = start
        while True:
            used.add(p)
            pts.append((g.nodes[far].x, g.nodes[far].y))
            nxt = [r for r in g.nodes[far].prims if r in compset and r != p and r not in used]
            if comp_degree(far) != 2 or not nxt:
                break
            p = nxt[0]
            a2, b2 = g.prim_nodes[p]
            far = b2 if a2 == far else a2
        out.append(pts)
    return out
