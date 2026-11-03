"""Beslut 2026-11-03 (S01 DN75): en etikett med dimension som pekar på en återvändsgren eller symbolen i dess ände,
och ingen annan etikett på grenen, ger grenen etikettens dimension – före regeln att korsningens grövre dimension
går in i en gren som bara bär symboletiketter."""
from vvs_engine.geometry.core import Seg
from vvs_engine.pipes.ownership import DEAD_END_KEEPS_EVIDENCE, Identity, propagate
from vvs_engine.pipes.representation import Prim, build_graph
from vvs_engine.rules import using
from vvs_engine.semantics.attachment import Contact, PipeCodeAnchor

FAM = "V-53-FE-_S01-|s|w2.34"
S110 = Identity(base="S01-P2", dn=110, system="S01", display="S01-P2-110")
S75 = Identity(base="S01-P2", dn=75, system="S01", display="S01-P2-75")


def _graph():
    # huvudledning 0-100-200 (DN110), återvändsgren rakt upp från (100, 0) till en symbol vid (100, -60)
    segs = [Seg(0, 0, 100, 0), Seg(100, 0, 200, 0), Seg(100, 0, 100, -60)]
    prims = [Prim(prim_id=i, pid=f"p{i}", seg_index=0, seg=s, family=FAM, layer=FAM.split("|")[0], width=2.34)
             for i, s in enumerate(segs)]
    return build_graph(prims, FAM)


def _anchor(aid, pid, pt, ident, kind):
    return PipeCodeAnchor(anchor_id=aid, page=1, designation_id=f"d{aid}", designation=ident.display,
                          designation_display=ident.display, system_token="S01", dn=ident.dn, multiplier=1,
                          block_id=f"b{aid}", leader_id=f"l{aid}", leader_paths=[], endpoint=pt,
                          state="VERIFIED_PIPE_ATTACHMENT", reason="",
                          contacts=[Contact(point=pt, kind=kind, family=FAM, pid=pid, seg_index=0, distance=0.0)])


def _run():
    anchors = [_anchor("a", "p0", (50.0, 0.0), S110, "end_tick"), _anchor("b", "p1", (150.0, 0.0), S110, "end_tick"),
               _anchor("c", "p2", (100.0, -60.0), S75, "via_symbol")]          # etiketten pekar på symbolen
    return propagate({FAM: _graph()}, anchors, 1, {"a": S110, "b": S110, "c": S75})


def test_the_branch_keeps_the_size_its_own_label_gives():
    st = _run().prim_states[FAM]
    assert st[2].identity.dn == 75 and DEAD_END_KEEPS_EVIDENCE in st[2].evidence
    assert st[0].identity.dn == st[1].identity.dn == 110


def test_with_the_rule_off_the_junction_size_runs_into_the_branch():
    with using({"pipes.ownership.DEAD_END_LABEL_KEEPS_DN": False}):
        st = _run().prim_states[FAM]
    assert st[2].identity.dn == 110
