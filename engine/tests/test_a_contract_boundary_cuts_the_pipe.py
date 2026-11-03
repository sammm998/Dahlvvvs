"""Entreprenadgräns (VS-Kalkyl SPEC 6.4.3): en not `ENTR. GRÄNS` med en kort ledare till ett rör kapar röret.

Den sida som saknar egen etikett är utanför entreprenaden: tillståndet OUTSIDE_CONTRACT, varken bekräftat eller
otilldelat, och dess längd - också den del av den kapade biten som ligger bortom punkten - räknas för sig.
"""
from types import SimpleNamespace

from vvs_engine.geometry.core import Seg
from vvs_engine.pipes.ownership import OUTSIDE_CONTRACT, Identity, propagate
from vvs_engine.pipes.representation import Prim, build_graph
from vvs_engine.rules import using
from vvs_engine.semantics.attachment import Contact, PipeCodeAnchor
from vvs_engine.semantics.boundary import ContractBoundary, find_contract_boundaries, is_boundary_note

FAM = "V-52BB-FE-_1-|s|w1.44"
S03 = Identity(base="S03-P2", dn=110, system="S03", display="S03-P2-110")


def _graph(segs):
    prims = [Prim(prim_id=i, pid=f"p{i}", seg_index=0, seg=s, family=FAM, layer=FAM.split("|")[0], width=1.44)
             for i, s in enumerate(segs)]
    return build_graph(prims, FAM)


def _anchor(aid, pid, pt):
    return PipeCodeAnchor(anchor_id=aid, page=2, designation_id=f"d{aid}", designation="S03-P2-110",
                          designation_display="S03-P2-110", system_token="S03", dn=110, multiplier=1, block_id="b",
                          leader_id="l", leader_paths=[], endpoint=pt, state="VERIFIED_PIPE_ATTACHMENT", reason="",
                          contacts=[Contact(point=pt, kind="end_tick", family=FAM, pid=pid, seg_index=0, distance=0.0)])


def _run():
    # en rak ledning 0-300 längs y=0 i tre bitar; etiketten på första biten, gränsen mitt på sista biten (x=250)
    return _graph([Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(200, 0, 300, 0)])


def _boundary(prim_id=2, point=(250.0, 0.0)):
    return ContractBoundary(page=2, note="ENTR. GRÄNS", note_bbox=(260, -30, 320, -20), point=point, family=FAM,
                            prim_id=prim_id, distance=0.0)


def test_the_unlabelled_side_is_outside_and_counted_on_its_own():
    res = propagate({FAM: _run()}, [_anchor("a1", "p0", (50.0, 0.0))], 2, {"a1": S03}, boundaries=[_boundary()])
    st = res.prim_states[FAM]
    assert [st[p].state for p in (0, 1, 2)] == ["CONFIRMED"] * 3        # den kapade biten står kvar innanför...
    [out] = res.outside_contract
    assert out["cut_outside_pt"] == 50.0 and out["length_pt"] == 50.0   # ...men halva den är utanför
    assert out["designation"] == "S03-P2-110"
    assert out["cut_segment"] == [[250.0, 0.0], [300.0, 0.0]]          # bitens del bortom punkten, för ritningen
    [pipe] = res.pipes
    assert pipe.length_pt == 250.0                                       # röret innanför slutar vid gränsen


def test_whole_pieces_beyond_the_point_change_state():
    g = _graph([Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(200, 0, 300, 0), Seg(300, 0, 400, 0)])
    res = propagate({FAM: g}, [_anchor("a1", "p0", (50.0, 0.0))], 2, {"a1": S03}, boundaries=[_boundary()])
    st = res.prim_states[FAM]
    assert st[3].state == OUTSIDE_CONTRACT and st[3].identity == S03     # identiteten följer med, för listan
    [out] = res.outside_contract
    assert out["length_pt"] == 150.0 and out["prims"] == [3]


def test_labels_on_both_sides_decide_nothing():
    anchors = [_anchor("a1", "p0", (50.0, 0.0)), _anchor("a2", "p2", (290.0, 0.0))]
    res = propagate({FAM: _run()}, anchors, 2, {"a1": S03, "a2": S03}, boundaries=[_boundary()])
    assert res.outside_contract == []
    [u] = res.boundaries_undecided
    assert u["reason"] == "labels_on_both_sides"


def test_a_boundary_that_reaches_no_pipe_is_reported():
    b = ContractBoundary(page=2, note="ENTR. GRÄNS", note_bbox=(0, 0, 1, 1), point=(0.0, 1.0), family=None,
                         prim_id=None, distance=None)
    res = propagate({FAM: _run()}, [_anchor("a1", "p0", (50.0, 0.0))], 2, {"a1": S03}, boundaries=[b])
    assert res.boundaries_undecided[0]["reason"] == "boundary_leader_reaches_no_pipe"


def test_the_rule_can_be_switched_off():
    with using({"pipes.ownership.CONTRACT_BOUNDARIES": False}):
        res = propagate({FAM: _run()}, [_anchor("a1", "p0", (50.0, 0.0))], 2, {"a1": S03},
                        boundaries=[_boundary()])
    assert res.outside_contract == [] and res.pipes[0].length_pt == 300.0


def test_the_note_is_read_in_its_usual_spellings():
    for text in ("ENTR. GRÄNS", "Entr.gräns", "ENTREPRENADGRÄNS", "ENTR GRANS", "Gräns för entreprenad"):
        assert is_boundary_note(text), text
    for text in ("GRÄNS", "ENTRÉ", "S01-P2-110"):
        assert not is_boundary_note(text), text


def _block(text="ENTR. GRÄNS", box=(1387.1, 510.2, 1451.9, 520.5)):
    return SimpleNamespace(bid="b1", rows=[SimpleNamespace(line=SimpleNamespace(text=text))], bbox=box, height=7.8)


def test_the_note_leader_is_followed_from_the_underline_to_the_pipe():
    # understrykning 3,6 pt under texten och en diagonal ned till röret; textens egna streck ligger i lådan
    leader = [Seg(1364.6, 543.0, 1383.5, 524.1), Seg(1383.5, 524.1, 1455.5, 524.1)]
    glyphs = [Seg(1387.1, 520.5, 1387.1, 512.7), Seg(1393.2, 512.7, 1397.3, 520.5), Seg(1402.0, 520.5, 1402.0, 512.7)]
    g = _graph([Seg(1348.9, 543.0, 1365.8, 543.0), Seg(1370.2, 543.0, 1387.1, 543.0)])
    [b] = find_contract_boundaries(2, [_block()], leader + glyphs, {FAM: g})
    assert b.family == FAM and b.prim_id == 0 and b.distance == 0.0
    assert abs(b.point[0] - 1364.6) < 1e-6 and b.point[1] == 543.0


def test_a_note_whose_leader_stops_far_from_any_pipe_reaches_none():
    leader = [Seg(1364.6, 563.0, 1383.5, 524.1), Seg(1383.5, 524.1, 1455.5, 524.1)]
    g = _graph([Seg(1348.9, 543.0, 1365.8, 543.0)])
    [b] = find_contract_boundaries(2, [_block()], leader, {FAM: g})
    assert b.family is None


def test_other_notes_are_not_boundaries():
    leader = [Seg(1364.6, 543.0, 1383.5, 524.1), Seg(1383.5, 524.1, 1455.5, 524.1)]
    g = _graph([Seg(1348.9, 543.0, 1365.8, 543.0)])
    assert find_contract_boundaries(2, [_block("S03-P2-110")], leader, {FAM: g}) == []
