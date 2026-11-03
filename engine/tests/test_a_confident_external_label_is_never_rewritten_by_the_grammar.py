"""En extern etikett (VS-Kalkyls T5) med konfidens över läsarens tröskel skrivs aldrig om av bladets grammatik.

Bladets mönster kan vilja läsa ett tvillingtecken annorlunda (2/Z, 0/O): `L9-28` på ett blad där `A9-A9` är
vanligast blir annars `L9-Z8` och tappar sin dimension. Den externa läsningen står; att mönstret ville annat
flaggas, det skrivs inte om.
"""
import json
from types import SimpleNamespace

from vvs_engine.semantics.annotation import AnnotationBlock, BlockRow, extract_designations
from vvs_engine.text.external_labels import ExternalLabels, apply_external_labels, ExternalLabel
from vvs_engine.text.model import Glyph, make_row


def _row(text: str, x: float, y: float, twins: dict[str, str] | None = None, page: int = 1):
    twins = twins or {}
    glyphs = [Glyph(gid=f"g{text}{i}", char=c, bbox=(x + i * 5.0, y, x + i * 5.0 + 4.0, y + 7.0), source="stroke",
                    score=0.6, alternatives=[(twins[c], 0.55)] if c in twins else [], path_ids=[f"p_{text}_{i}"])
              for i, c in enumerate(text)]
    return make_row(page, glyphs, 0.0, "stroke")


def _blocks(rows):
    return [AnnotationBlock(bid=f"b{i}", page=1, rows=[BlockRow(line=r)], bbox=r.bbox, angle=0.0, height=7.0,
                            layer="", source="stroke") for i, r in enumerate(rows)]


PAGE = SimpleNamespace(paths=[], info=SimpleNamespace(index=1))
SHEET = ["KV01-K1", "S01-P2", "S02-P2", "VV01-K5", "S03-P2"]       # bladets mönster: A9-A9


def _read(protect: bool):
    rows = [_row(t, 100.0, 100.0 + 20 * i) for i, t in enumerate(SHEET)]
    label = _row("L9-28", 300.0, 100.0, twins={"2": "Z"})
    label.protected = protect
    designations, _, _ = extract_designations(PAGE, _blocks(rows + [label]))
    return next(d for d in designations if d.bbox[0] >= 300.0)


def test_without_protection_the_sheet_pattern_rewrites_a_twin():
    assert _read(protect=False).text == "L9-Z8"          # så som motorn läser på egen hand


def test_a_confident_external_label_keeps_its_text_and_is_flagged():
    d = _read(protect=True)
    assert d.text == "L9-28" and d.dn == 28
    assert d.evidence["external_text_kept"] and d.evidence["grammar_would_read"] == "L9-Z8"


def test_a_label_that_already_fits_the_pattern_is_not_flagged():
    rows = [_row(t, 100.0, 100.0 + 20 * i) for i, t in enumerate(SHEET)]
    label = _row("KV01-K5", 300.0, 100.0)
    label.protected = True
    designations, _, _ = extract_designations(PAGE, _blocks(rows + [label]))
    d = next(d for d in designations if d.bbox[0] >= 300.0)
    assert d.text == "KV01-K5" and "external_text_kept" not in d.evidence


def test_protection_follows_the_readers_own_threshold(tmp_path):
    path = tmp_path / "t5.json"
    path.write_text(json.dumps({"_meta": {"min_read_confidence": 0.9},
                                "1": {"rows": [{"text": "L9-28", "bbox": [99, 99, 125, 108], "read_confidence": 0.85},
                                               {"text": "L9-22", "bbox": [99, 199, 125, 208], "read_confidence": 0.95}]}}),
                    encoding="utf-8")
    labels = ExternalLabels.load(str(path))
    lines, _ = apply_external_labels([], labels.by_page[1], 1)
    by_text = {ln.text: ln for ln in lines}
    assert not by_text["L9-28"].protected and by_text["L9-22"].protected      # 0,85 < 0,9 <= 0,95


def test_an_engine_line_the_reader_confirms_is_protected_too():
    engine = _row("L9-28", 100.0, 100.0, twins={"2": "Z"})
    lines, rep = apply_external_labels([engine], [ExternalLabel("L9-28", (99.0, 99.0, 126.0, 108.0), 0.98)], 1)
    assert lines == [engine] and engine.protected and rep.unchanged == 1
