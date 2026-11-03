"""Spridning (VS-Kalkyl SPEC 6.4.3 h.3): en etikett gäller längs röret - raka fortsättningar och böjar - tills ny
etikett, symbol, dimensionsbyte eller gräns, utan längdbudget. En gren utan egen etikett ärver system och material,
men dimensionen är osäker: den mäts som ett eget rör i granskningsnivån, och huvudledningen påverkas inte.
"""
from vvs_engine.geometry.core import Seg
from vvs_engine.pipes.ownership import INHERITED_EVIDENCE, Identity, propagate
from vvs_engine.pipes.representation import Prim, build_graph
from vvs_engine.rules import using
from vvs_engine.semantics.attachment import Contact, PipeCodeAnchor

FAM = "V-53-FE-_S01-|s|w2.34"


def _graph(segs):
    prims = [Prim(prim_id=i, pid=f"p{i}", seg_index=0, seg=s, family=FAM, layer=FAM.split("|")[0], width=2.34)
             for i, s in enumerate(segs)]
    return build_graph(prims, FAM)


def _anchor(aid, pid, pt):
    return PipeCodeAnchor(anchor_id=aid, page=1, designation_id=f"d{aid}", designation="S01-P2-110",
                          designation_display="S01-P2-110", system_token="S01", dn=110, multiplier=1, block_id="b",
                          leader_id="l", leader_paths=[], endpoint=pt, state="VERIFIED_PIPE_ATTACHMENT", reason="",
                          contacts=[Contact(point=pt, kind="end_tick", family=FAM, pid=pid, seg_index=0, distance=0.0)])


S01_110 = Identity(base="S01-P2", dn=110, system="S01", display="S01-P2-110")
S01_75 = Identity(base="S01-P2", dn=75, system="S01", display="S01-P2-75")
S02_110 = Identity(base="S02-P2", dn=110, system="S02", display="S02-P2-110")


def _tee():
    # huvudledning 0-200 längs y=0 med en böj upp till (200, 100); gren rakt ned från (100, 0) till (100, -150)
    return _graph([Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(200, 0, 200, 100), Seg(100, 0, 100, -150)])


def _states(result):
    return {pid: s for pid, s in result.prim_states[FAM].items()}


def test_the_label_follows_straight_runs_and_bends():
    g = _tee()
    res = propagate({FAM: g}, [_anchor("a1", "p0", (50.0, 0.0))], 1, {"a1": S01_110})
    st = _states(res)
    assert [st[p].state for p in (0, 1, 2)] == ["CONFIRMED"] * 3          # rakt och runt böjen
    assert all(not st[p].tentative for p in (0, 1, 2))


def test_an_unlabelled_branch_inherits_system_and_material_with_an_uncertain_size():
    res = propagate({FAM: _tee()}, [_anchor("a1", "p0", (50.0, 0.0))], 1, {"a1": S01_110})
    branch = _states(res)[3]
    assert branch.state == "CONFIRMED" and branch.identity.stem == "S01-P2" and branch.tentative
    assert INHERITED_EVIDENCE in branch.evidence
    pipes = {(p.identity.key, p.needs_review) for p in res.pipes}
    assert ("S01-P2|DN110", False) in pipes and ("S01-P2|DN110", True) in pipes   # grenen är ett eget rör


def test_a_branch_between_two_systems_stays_ambiguous():
    g = _graph([Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(100, 0, 100, -150)])
    anchors = [_anchor("a1", "p0", (50.0, 0.0)), _anchor("a2", "p1", (150.0, 0.0))]
    res = propagate({FAM: g}, anchors, 1, {"a1": S01_110, "a2": S02_110})
    assert _states(res)[2].state != "CONFIRMED" or _states(res)[2].identity is None


def test_a_branch_between_two_sizes_of_one_system_takes_the_coarser_as_uncertain():
    g = _graph([Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(100, 0, 100, -150)])
    anchors = [_anchor("a1", "p0", (50.0, 0.0)), _anchor("a2", "p1", (150.0, 0.0))]
    res = propagate({FAM: g}, anchors, 1, {"a1": S01_110, "a2": S01_75})
    branch = _states(res)[2]
    assert branch.state == "CONFIRMED" and branch.tentative
    assert branch.identity.dn == 110 and branch.identity.stem == "S01-P2"
    assert not _states(res)[0].tentative and not _states(res)[1].tentative        # de märkta armarna står


def test_no_length_budget_on_a_long_straight_continuation():
    # en kort märkt bit och en lång rak fortsättning genom en T: inget tas tillbaka för att den är lång
    g = _graph([Seg(0, 0, 10, 0), Seg(10, 0, 400, 0), Seg(10, 0, 10, 20)])
    res = propagate({FAM: g}, [_anchor("a1", "p0", (5.0, 0.0))], 1, {"a1": S01_110})
    assert _states(res)[1].state == "CONFIRMED" and not _states(res)[1].tentative


def test_the_rule_can_be_switched_off():
    with using({"pipes.ownership.PROPAGATE_TO_STOPS": False}):
        res = propagate({FAM: _tee()}, [_anchor("a1", "p0", (50.0, 0.0))], 1, {"a1": S01_110})
    assert not _states(res)[3].tentative


def test_a_double_drawn_line_is_no_loop_one_loop_is_a_ring_and_a_grid_is_a_mesh():
    from vvs_engine.pipes.ownership import _cycles, _has_cycle
    twice = _graph([Seg(0, 0, 100, 0), Seg(0, 0, 100, 0), Seg(100, 0, 200, 0)])
    assert _cycles(twice, list(twice.prims)) == 0                       # samma linje två gånger
    ring = _graph([Seg(0, 0, 100, 0), Seg(100, 0, 100, 100), Seg(100, 100, 0, 100), Seg(0, 100, 0, 0)])
    assert _cycles(ring, list(ring.prims)) == 1 and not _has_cycle(ring, list(ring.prims))
    grid = _graph([Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(0, 100, 100, 100), Seg(100, 100, 200, 100),
                   Seg(0, 0, 0, 100), Seg(100, 0, 100, 100), Seg(200, 0, 200, 100)])
    assert _cycles(grid, list(grid.prims)) == 2 and _has_cycle(grid, list(grid.prims))
