"""En ledare som stannar strax före sitt rör fångas av det närmaste röret (VS-Kalkyl SPEC 6.4.3 h.1).

Räckhållet är två gånger den normala toleransen (NEAR_MISS, 6 pt ≈ 2 mm på papper). Ligger ett annat rör högst
1,5 gånger så långt bort har ritningen inte sagt vilket som menas, och inget fångas. Ett rör som bara nås så får
konfidens 0,7. Flera etiketter vars ledare slutar i en punkt där inget rör finns rapporteras med punktens koordinater.
"""
from types import SimpleNamespace

from vvs_engine.geometry.core import Seg
from vvs_engine.output.artifacts import leaders_meeting_without_pipe
from vvs_engine.pdf.extract import RawPath
from vvs_engine.rules import using
from vvs_engine.semantics.annotation import AnnotationBlock, BlockRow
from vvs_engine.semantics.attachment import GeometryIndex, leader_contacts, resolve_block
from vvs_engine.semantics.leaders import Leader


def path(pid, points, width=2.0, layer="V-52BB-FE-_KV-"):
    return RawPath(pid=pid, seqno=0, page=0, layer=layer, layer_id=0, kind="s",
                   width=width, color=(0., 0., 0.), fill=None, closed=False,
                   segs=[Seg(*a, *b) for a, b in zip(points, points[1:])],
                   bbox=(min(x for x, y in points), min(y for x, y in points),
                         max(x for x, y in points), max(y for x, y in points)),
                   n_items=len(points) - 1, n_curves=0, n_subpaths=1)


def _leader(end=(100.0, 100.0)):
    return Leader("L", 0, "B", [], [(70.0, 70.0), end], (70.0, 70.0), end, "underline_end", "LEAD", .25,
                  color=(0., 0., 0.))


def _contacts(*pipes):
    paths = {p.pid: p for p in pipes}
    index = GeometryIndex(SimpleNamespace(paths=list(paths.values())), set(), set())
    return leader_contacts(_leader(), index, None, paths)


def test_the_nearest_pipe_catches_a_leader_that_stops_short():
    near = path("near", [(50, 103), (150, 103)])                 # 3 pt nedanför änden
    far = path("far", [(50, 94), (150, 94)], layer="V-52BC-FE-_VV-")   # 6 pt ovanför: mer än 1,5 × 3
    cs = _contacts(near, far)
    assert {c.pid for c in cs} == {"near"} and all(c.kind == "near_miss" for c in cs)


def test_two_pipes_about_as_near_catch_nothing():
    a = path("a", [(50, 103), (150, 103)])
    b = path("b", [(50, 96), (150, 96)], layer="V-52BC-FE-_VV-")      # 4 pt: inom 1,5 × 3
    assert _contacts(a, b) == []


def test_beyond_the_reach_nothing_is_caught():
    assert _contacts(path("p", [(50, 107), (150, 107)])) == []      # 7 pt > 6 pt
    with using({"semantics.attachment.NEAR_MISS": 8.0}):
        assert len(_contacts(path("p", [(50, 107), (150, 107)]))) == 1


def test_the_old_rule_only_caught_a_lone_pipe():
    a = path("near", [(50, 103), (150, 103)])
    b = path("far", [(50, 94), (150, 94)], layer="V-52BC-FE-_VV-")
    with using({"semantics.attachment.NEAR_MISS_NEAREST": False}):
        assert _contacts(a, b) == []


def _designation(text="KV01-E13-40"):
    return SimpleNamespace(did="d1", page=1, text=text, display_text=text, system_token="KV01", dn=40,
                           multiplier=1, row_index=0, bbox=(0, 0, 50, 8))


def test_a_caught_pipe_has_confidence_07():
    cs = _contacts(path("near", [(50, 103), (150, 103)]))
    block = AnnotationBlock(bid="B", page=1, rows=[BlockRow(line=SimpleNamespace())], bbox=(0, 0, 50, 8),
                            angle=0.0, height=7.0, layer="", source="stroke")
    [a] = resolve_block(block, [_designation()], _leader(), cs, {"KV01"})
    assert a.evidence["leader_end_captured"] and a.evidence["confidence"] == 0.7
    assert abs(a.evidence["capture_distance_mm"] - 3 * 25.4 / 72) < 0.01


def _no_pipe(lid, end, text):
    return SimpleNamespace(state="NO_PIPE_ATTACHMENT", leader_id=lid, endpoint=end, designation=text)


def test_leaders_meeting_where_no_pipe_is_are_reported_with_the_point():
    anchors = [_no_pipe(f"L{i}", (532.8 + 0.3 * i, 695.26), t)
               for i, t in enumerate(["VV01-K1-15-F", "KV01-K1-15-W", "KV01-K1-15-W", "S01-P3", "L9-28"])]
    anchors.append(_no_pipe("L9", (300.0, 300.0), "S03-P2"))        # ensam: ett läsfel, inget möte
    [m] = leaders_meeting_without_pipe(anchors)
    assert m["kind"] == "leaders_meet_without_pipe" and m["n_leaders"] == 5 and m["n_labels"] == 5
    assert abs(m["point"][0] - 533.4) < 0.01 and m["point"][1] == 695.26
    assert "S03-P2" not in m["texts"]


def test_one_leader_from_a_stacked_block_counts_its_labels():
    anchors = [_no_pipe("L1", (532.8, 695.26), t) for t in ["VV01-K1-15-F", "KV01-K1-15-W", "S01-P3", "L9-28"]]
    [m] = leaders_meeting_without_pipe(anchors)
    assert m["n_leaders"] == 1 and m["n_labels"] == 4 and m["point"] == [532.8, 695.26]


def test_two_leaders_are_not_yet_a_meeting():
    anchors = [_no_pipe("L1", (10.0, 10.0), "A"), _no_pipe("L2", (10.5, 10.0), "B")]
    assert leaders_meeting_without_pipe(anchors) == []
    with using({"output.artifacts.LEADERS_MEET_MIN": 2}):
        assert len(leaders_meeting_without_pipe(anchors)) == 1
