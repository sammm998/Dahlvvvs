"""En rörbeteckning utan ledare tillhör det närmaste rör som löper parallellt med texten (SPEC 6.4.3 h.2).

Avståndet räknas på papper (6 mm = 17,0 pt). Ett annat rör högst 1,5 gånger så långt bort gör tilldelningen
tvetydig. En streckad ledning är en sammanhängande sträcka även över strecken.
"""
from types import SimpleNamespace

from vvs_engine.geometry.core import Seg
from vvs_engine.pipes.representation import Prim, build_graph
from vvs_engine.rules import using
from vvs_engine.semantics.proximity import PT_PER_MM, leaderless_anchors

FAM = "V-52BB-FE-_KV-|s|w1.44"


def _graph(segs, fam=FAM, start=0):
    prims = [Prim(prim_id=start + i, pid=f"{fam[:6]}{start + i}", seg_index=0, seg=s, family=fam, layer=fam.split("|")[0],
                  width=1.44) for i, s in enumerate(segs)]
    return build_graph(prims, fam)


def _label(text="KV01-E13-40", box=(100.0, 90.0, 170.0, 98.0), angle=0.0, did="d1", block="b1"):
    return SimpleNamespace(did=did, page=1, text=text, system_token=text.split("-")[0], dn=40, multiplier=1,
                           block_id=block, bbox=box, angle=angle, row_index=0)


def _anchors(labels, graphs, led=(), anchored=()):
    return leaderless_anchors(labels, {d.did for d in labels}, set(led), set(anchored), graphs)


def test_a_label_written_along_a_pipe_names_it_with_confidence_08():
    g = _graph([Seg(0, 100, 400, 100)])                      # röret 2 pt under etiketten
    [a] = _anchors([_label()], {FAM: g})
    assert a.state == "VERIFIED_PIPE_ATTACHMENT" and a.evidence["proximity_without_leader"]
    assert a.evidence["confidence"] == 0.8 and a.contacts[0].family == FAM
    assert abs(a.evidence["distance_mm"] - 2 / PT_PER_MM) < 0.01


def test_a_dashed_pipe_counts_as_one_run():
    dashes = [Seg(x, 100, x + 12, 100) for x in range(0, 400, 15)]      # streck 12, paus 3
    [a] = _anchors([_label()], {FAM: _graph(dashes)})
    assert a.state == "VERIFIED_PIPE_ATTACHMENT"                          # många streck, ett rör


def test_a_pipe_across_the_text_is_not_the_one_it_names():
    across = _graph([Seg(130, 0, 130, 300)])                 # lodrätt genom etiketten
    assert _anchors([_label()], {FAM: across}) == []


def test_too_far_on_paper_names_nothing():
    far = _graph([Seg(0, 98 + 7 * PT_PER_MM, 400, 98 + 7 * PT_PER_MM)])     # 7 mm under lådan
    assert _anchors([_label()], {FAM: far}) == []
    with using({"semantics.proximity.LEADERLESS_MM": 8.0}):                # avståndet går att ställa in
        assert len(_anchors([_label()], {FAM: far})) == 1


def test_two_pipes_about_as_near_make_it_ambiguous():
    other = "V-52BC-FE-_VV-|s|w1.44"
    graphs = {FAM: _graph([Seg(0, 100, 400, 100)]), other: _graph([Seg(0, 86, 400, 86)], other, start=10)}
    [a] = _anchors([_label()], graphs)                       # 2 pt under, 4 pt över: 4 <= 1,5 × 2? nej
    assert a.state == "VERIFIED_PIPE_ATTACHMENT"
    graphs[other] = _graph([Seg(0, 87, 400, 87)], other, start=10)           # 3 pt över: 3 <= 1,5 × 2
    [a] = _anchors([_label()], graphs)
    assert a.state == "AMBIGUOUS_PIPE_ATTACHMENT" and a.evidence["confidence"] == 0.0
    assert {c.family for c in a.contacts} == {FAM, other}


def test_a_label_with_a_leader_or_an_anchor_is_left_to_it():
    g = {FAM: _graph([Seg(0, 100, 400, 100)])}
    assert _anchors([_label()], g, led={"b1"}) == []
    assert _anchors([_label()], g, anchored={"d1"}) == []


def test_vertical_text_names_a_vertical_pipe():
    g = _graph([Seg(200, 0, 200, 400)])
    [a] = _anchors([_label(box=(190.0, 100.0, 198.0, 170.0), angle=-90.0)], {FAM: g})
    assert a.state == "VERIFIED_PIPE_ATTACHMENT"
