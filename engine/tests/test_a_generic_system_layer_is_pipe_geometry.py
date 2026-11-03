"""Ett lager med rörlagrens namnmall men tomt systemled (V-53-FE-_- bredvid V-53-FE-_S02-) är rörgeometri."""
from vvs_engine.geometry.core import Seg
from vvs_engine.pdf.extract import RawPath
from vvs_engine.pipeline import _generalize_families
from vvs_engine.pipes.representation import collect_prims, build_graph, describe_family, family_key
from vvs_engine.rules import using


def _path(pid, layer, x0, x1, y):
    return RawPath(pid=pid, seqno=0, page=0, layer=layer, layer_id=0, kind="s", width=1.44, color=(0., 0., 0.),
                   fill=None, closed=False, segs=[Seg(x0, y, x1, y)], bbox=(x0, y, x1, y), n_items=1, n_curves=0,
                   n_subpaths=1)


def _page(paths):
    from types import SimpleNamespace
    return SimpleNamespace(paths=paths, info=SimpleNamespace(index=0, width=2384.0, height=1684.0, rotation=0),
                           drawings=[], spans=[])


def _families(page, layers):
    fams = {family_key(p) for p in page.paths if p.layer in layers}
    prims = collect_prims(page, fams)
    graphs = {fk: build_graph(prims[fk], fk) for fk in prims}
    return {fk: describe_family(fk, prims[fk], graphs[fk]) for fk in prims}, graphs


def test_the_template_with_an_empty_system_slot_is_taken():
    paths = [_path("a", "V-53-FE-_S02-", 0, 200, 100), _path("b", "V-53-FE-_S03-", 0, 200, 200),
             _path("c", "V-53-FE-_-", 200, 400, 100), _path("d", "A-40-P-01", 0, 400, 300)]
    page = _page(paths)
    fams, graphs = _families(page, {"V-53-FE-_S02-", "V-53-FE-_S03-"})
    got, _ = _generalize_families(page, dict(fams), dict(graphs))
    layers = {fk.split("|s|")[0] for fk in got}
    assert "V-53-FE-_-" in layers and "A-40-P-01" not in layers
    with using({"pipeline.GENERIC_SYSTEM_LAYER": False}):
        off, _ = _generalize_families(page, dict(fams), dict(graphs))
    assert "V-53-FE-_-" not in {fk.split("|s|")[0] for fk in off}
