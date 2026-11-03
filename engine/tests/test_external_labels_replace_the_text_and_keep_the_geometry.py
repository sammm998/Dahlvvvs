"""En extern etikettfil (VS-Kalkyls T5) byter text i motorns rader men behåller motorns geometri och streck."""
import json

from vvs_engine.text.external_labels import ExternalLabel, ExternalLabels, apply_external_labels
from vvs_engine.text.model import Glyph, make_row


def _row(text: str, x0: float, y0: float, w: float = 5.0, h: float = 7.0, angle: float = 0.0, page: int = 1):
    glyphs = [Glyph(gid=f"g{i}_{text}", char=c, bbox=(x0 + i * w, y0, x0 + (i + 1) * w, y0 + h), source="stroke",
                    path_ids=[f"p_{text}_{i}"]) for i, c in enumerate(text)]
    return make_row(page, glyphs, angle, "stroke")


def test_a_partial_engine_reading_takes_the_labels_text_and_keeps_its_strokes():
    engine = _row("KVO1", 100.0, 200.0)                     # motorn läste bara början, med O för 0
    label = ExternalLabel("KV01-K1-15-W40", (98.0, 198.0, 172.0, 209.0), 0.98)
    lines, rep = apply_external_labels([engine], [label], 1)
    [ln] = lines
    assert ln.text == "KV01-K1-15-W40" and len(ln.glyphs) == len(ln.text)
    assert ln.angle == engine.angle and ln.height == engine.height
    assert set(engine.provenance) <= set(ln.provenance)        # strecken är fortfarande text, inte rör
    assert {p for g in ln.glyphs for p in g.path_ids} == set(engine.provenance)
    assert ln.bbox[0] <= 100.0 and ln.bbox[2] >= 171.0          # hela etikettens längd
    assert rep.changed == 1 and rep.replaced_lines == 1 and rep.added == 0 and not rep.uncertain_rids


def test_a_label_the_engine_did_not_read_becomes_a_new_line():
    other = _row("RUM", 500.0, 500.0)
    flat = ExternalLabel("ENTR. GRÄNS", (100.0, 100.0, 160.0, 108.0), 0.85)
    tall = ExternalLabel("AV20", (300.0, 100.0, 308.0, 130.0), 0.98)
    lines, rep = apply_external_labels([other], [flat, tall], 1)
    by_text = {ln.text: ln for ln in lines}
    assert set(by_text) == {"RUM", "ENTR. GRÄNS", "AV20"}
    assert by_text["ENTR. GRÄNS"].angle == 0.0 and by_text["AV20"].angle == -90.0
    assert by_text["ENTR. GRÄNS"].source == "stroke"            # grupperas med bladets övriga text
    assert rep.added == 2 and rep.replaced_lines == 0


def test_an_uncertain_label_is_used_but_marked():
    engine = _row("AV2O1-15", 100.0, 200.0)
    shaky = ExternalLabel("AV201-15", (98.0, 198.0, 142.0, 209.0), 0.7, ("readers_disagree", "needs_review"))
    lines, rep = apply_external_labels([engine], [shaky], 1)
    [ln] = lines
    assert ln.text == "AV201-15" and rep.uncertain == 1 and ln.rid in rep.uncertain_rids


def test_the_same_text_already_read_counts_as_unchanged():
    engine = _row("RA101", 100.0, 200.0)
    lines, rep = apply_external_labels([engine], [ExternalLabel("RA101", (99.0, 199.0, 126.0, 208.0), 0.98)], 1)
    assert [ln.text for ln in lines] == ["RA101"] and rep.unchanged == 1 and rep.changed == 0


def test_two_labels_inside_one_engine_line_both_survive():
    engine = _row("KVO1-K1 W4O", 100.0, 200.0)
    a = ExternalLabel("KV01-K1", (99.0, 199.0, 136.0, 208.0), 0.98)
    b = ExternalLabel("W40", (139.0, 199.0, 156.0, 208.0), 0.98)
    lines, rep = apply_external_labels([engine], [a, b], 1)
    assert sorted(ln.text for ln in lines) == ["KV01-K1", "W40"]
    assert rep.replaced_lines == 1


def test_the_result_does_not_depend_on_the_order_of_the_inputs():
    rows = [_row("KVO1", 100.0, 200.0), _row("VVO1", 100.0, 300.0), _row("RUM", 400.0, 400.0)]
    labels = [ExternalLabel("KV01-K1-15", (98.0, 198.0, 152.0, 209.0), 0.98),
              ExternalLabel("VV01-K1-15", (98.0, 298.0, 152.0, 309.0), 0.98),
              ExternalLabel("ENTR. GRÄNS", (600.0, 100.0, 660.0, 108.0), 0.85)]
    a, _ = apply_external_labels(rows, labels, 1)
    b, _ = apply_external_labels(list(reversed(rows)), list(reversed(labels)), 1)
    assert [(r.rid, r.text, r.bbox) for r in a] == [(r.rid, r.text, r.bbox) for r in b]


def test_the_file_is_read_per_page_and_hashed(tmp_path):
    path = tmp_path / "t5.json"
    path.write_text(json.dumps({
        "1": {"rows": [{"text": "KV01-K1-15", "bbox": [1, 2, 3, 4], "read_confidence": 0.98, "flags": []},
                       {"text": "", "bbox": [1, 2, 3, 4], "read_confidence": 0.9}]},
        "2": {"rows": [{"text": "AV20", "bbox": [5, 6, 7, 8], "read_confidence": 0.75, "flags": ["needs_review"]}]},
        "_meta": {"model": "claude-sonnet-5-5", "ocr": "rapidocr", "ai_run_ids": ["x"]},
    }), encoding="utf-8")
    labels = ExternalLabels.load(str(path))
    assert sorted(labels.by_page) == [1, 2] and len(labels.by_page[1]) == 1
    assert labels.by_page[2][0].uncertain and not labels.by_page[1][0].uncertain
    assert len(labels.sha256) == 64 and labels.meta["model"] == "claude-sonnet-5-5"
    assert sorted(labels.without([2]).by_page) == [1]
