"""En rörlista på en rad är ett knippe; en text vars tal inte är en rördimension är inget rör."""
from types import SimpleNamespace

from vvs_engine.rules import using
from vvs_engine.semantics.legend import PIPE_SIZES, DrawingLegend
from vvs_engine.text.external_labels import ExternalLabel, split_pipe_lists


def _lb(text):
    return ExternalLabel(text, (100.0, 100.0, 160.0, 112.0), 0.98)


def test_a_list_becomes_one_row_per_pipe_and_shares_the_last_size():
    rows = split_pipe_lists([_lb("KV02-53-,VV02-53-20")])
    assert [r.text for r in rows] == ["KV02-53-20", "VV02-53-20"]
    assert rows[0].bbox[1] < rows[1].bbox[1] and rows[0].bbox[3] <= rows[1].bbox[1] + 1e-9   # staplade, i ordning
    assert all("split_from_pipe_list" in r.flags for r in rows)


def test_each_part_keeps_its_own_size_and_three_parts_work():
    rows = split_pipe_lists([_lb("KV01-53-20, VV01-53-20, VVC01-53-16")])
    assert [r.text for r in rows] == ["KV01-53-20", "VV01-53-20", "VVC01-53-16"]
    assert [r.text for r in split_pipe_lists([_lb("KV01-3-/VV01-3-15")])] == ["KV01-3-15", "VV01-3-15"]


def test_text_that_is_not_a_list_of_pipes_is_left_alone():
    for text in ("ANSL. HÖGTRYCK, SPOLSLANG", "KV01-K1-15", "KV01-53-,VV01-53-"):   # sista ledet saknar dimension
        assert [r.text for r in split_pipe_lists([_lb(text)])] == [text]
    with using({"text.external_labels.SPLIT_PIPE_LISTS": False}):
        assert [r.text for r in split_pipe_lists([_lb("KV02-53-,VV02-53-20")])] == ["KV02-53-,VV02-53-20"]


def test_a_number_that_is_not_a_pipe_size_names_no_pipe():
    legend = DrawingLegend()
    ref = SimpleNamespace(text="V50-02-271", system_token="V50", dn=271)
    pipe = SimpleNamespace(text="VS02-22-15", system_token="VS02", dn=15)
    assert 271 not in PIPE_SIZES and not legend.names_a_pipe(ref)
    assert legend.names_a_pipe(pipe)
    with using({"semantics.legend.PIPE_SIZES_ONLY": False}):
        assert legend.names_a_pipe(ref)


def test_splitting_a_list_next_to_inkless_labels_does_not_shift_the_ink_check():
    # förr hölls bläckflaggan i en lista parallell med etiketterna – en delad lista flyttade indexen
    from vvs_engine.text.external_labels import apply_external_labels

    class NoInk:
        def inside(self, box):
            return None if box[0] > 500 else box

    labels = [_lb("KV02-53-,VV02-53-20"), ExternalLabel("KV01-53-32", (600.0, 100.0, 650.0, 108.0), 0.98)]
    lines, rep = apply_external_labels([], labels, 1, NoInk())
    assert sorted(ln.text for ln in lines) == ["KV02-53-20", "VV02-53-20"] and rep.no_ink == 1
