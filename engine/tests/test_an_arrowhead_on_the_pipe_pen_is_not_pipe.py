"""Beslut 2026-11-03: pilspetsar ritade med rörpennan räknas aldrig som rörlängd. Generellt: en liten sluten form."""
from vvs_engine.geometry.core import Seg
from vvs_engine.pipes.representation import is_small_closed_figure
from vvs_engine.rules import using
from types import SimpleNamespace


def _path(points, closed=False):
    segs = [Seg(*a, *b) for a, b in zip(points, points[1:])]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return SimpleNamespace(segs=segs, closed=closed, bbox=(min(xs), min(ys), max(xs), max(ys)))


ARROW = [(1405.1, 543.2), (1409.6, 543.2), (1409.6, 543.5), (1410.8, 543.0), (1409.6, 542.4), (1409.6, 542.8),
         (1405.1, 542.8), (1405.1, 543.2)]          # pilspets ritad som kontur, som på Bilaga A plan 0


def test_an_arrowhead_outline_is_a_small_closed_figure():
    assert is_small_closed_figure(_path(ARROW))


def test_a_pipe_run_and_a_large_ring_are_not():
    assert not is_small_closed_figure(_path([(0, 0), (100, 0)]))                               # rak ledning
    assert not is_small_closed_figure(_path([(0, 0), (8, 0), (8, 3)]))                         # öppen, liten
    assert not is_small_closed_figure(_path([(0, 0), (200, 0), (200, 100), (0, 100), (0, 0)]))  # ringledning


def test_the_size_is_a_rule():
    with using({"pipes.representation.SMALL_FIGURE_MAX_PT": 4.0}):
        assert not is_small_closed_figure(_path(ARROW))
