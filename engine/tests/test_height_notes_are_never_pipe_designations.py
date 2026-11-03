"""Höjdnoter - CL (centrumlinje) och VG (vattengång) med en höjd - säger var ett rör ligger, aldrig att det finns
ett rör till. Svensk standardnotation: `CL+2300`, `VG+-828`, `VG±563`, `VG-12`, `CL 4000`.

En höjdnot som läses som rörbeteckning gör blocket `S02-P2-110` / `VG+-563` till en bunt med två rader som ska ha
två rör; ledaren fäster då inte, och röret som etiketten namnger mäts inte.
"""
import json
import os
from types import SimpleNamespace

import pytest

from vvs_engine.semantics.annotation import _row_role
from vvs_engine.semantics.grammar import is_code_like, is_height_note

HEIGHT_NOTES = ["CL+2300", "CL 4000", "CL-150", "CL=2500", "VG+-563", "VG+-1005", "VG +- 828", "VG+828",
                "VG-12", "VG±563", "VG-+40", "VG 1,67", "vg+-563"]
NOT_HEIGHT_NOTES = ["S02-P2-110", "KV01-K1-15-W40", "VG01-K1-15", "VGB-12A", "CL+", "VG+-", "L9-S13-28"]


@pytest.mark.parametrize("text", HEIGHT_NOTES)
def test_height_notes_are_recognised_in_every_sign(text):
    assert is_height_note(text)


@pytest.mark.parametrize("text", NOT_HEIGHT_NOTES)
def test_codes_and_bare_tags_are_not_height_notes(text):
    assert not is_height_note(text)


@pytest.mark.parametrize("text", HEIGHT_NOTES)
def test_a_height_note_is_never_code_like(text):
    assert not is_code_like(text.replace(" ", ""))


@pytest.mark.parametrize("text", ["CL+2300", "VG+-563", "VG±563", "VG-12", "VG+828"])
def test_a_height_note_row_has_the_role_elevation(text):
    assert _row_role(text, None, None) == "elevation"


@pytest.mark.parametrize("text", ["S02-P2-110", "KV01-K1-15", "VG01-K1-15"])
def test_a_pipe_designation_keeps_its_role(text):
    assert _row_role(text, None, None) == "designation"


def test_a_height_whose_sign_the_consultant_settles_claims_no_vertical_length():
    from vvs_engine.pipeline import _elevations
    rows = [SimpleNamespace(role="designation", text_norm="S02-P2-110", line=SimpleNamespace(rid="r0")),
            SimpleNamespace(role="elevation", text_norm="VG+-563", line=SimpleNamespace(rid="r1")),
            SimpleNamespace(role="elevation", text_norm="CL+2300", line=SimpleNamespace(rid="r2"))]
    block = SimpleNamespace(bid="b1", rows=rows, unit_of_row=lambda ri: [0, 1, 2])
    anchor = SimpleNamespace(anchor_id="a1", block_id="b1", evidence={"row_index": 0})
    [vg, cl] = _elevations([block], [anchor])["a1"]
    assert vg["tag"] == "VG" and vg["value"] == 563 and vg["unit"] is None     # tecknet är inte avgjort
    assert cl["tag"] == "CL" and cl["value"] == 2300 and cl["unit"] == "mm"


# ---- lokal regression mot riktiga blad med etikettfilen ------------------------------------------------------
#
# Ritningarna och etikettfilen är kundmaterial och ligger utanför repot:
#   data/local_facit/t5_labels.json (eller VVS_LOCAL_T5_FACIT):
#   {"pdf": "...", "labels": "t5.json", "sheets": [{"name", "page", "pipe_family_tokens": [...],
#    "confirmed": {"S02-P2-110": 10.0}, "never_designations": ["VG", "CL"]}]}

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL = os.environ.get("VVS_LOCAL_T5_FACIT", os.path.join(ROOT, "data", "local_facit", "t5_labels.json"))


def _local():
    if not os.path.isfile(LOCAL):
        return None
    with open(LOCAL, encoding="utf-8") as fh:
        facit = json.load(fh)
    return facit if os.path.isfile(facit.get("pdf", "")) and os.path.isfile(facit.get("labels", "")) else None


def _sheets():
    f = _local()
    return f["sheets"] if f else []


@pytest.mark.skipif(_local() is None, reason="lokalt facit för etikettfilen saknas (data/local_facit/t5_labels.json)")
@pytest.mark.parametrize("sheet", _sheets(), ids=lambda s: s.get("name", "blad"))
def test_local_sheet_measures_the_pipes_its_labels_name(sheet):
    """Med etikettfilen: rörfamiljerna facit pekar ut räknas som rör, beteckningarna får sina meter, och ingen
    höjdnot blir en rörbeteckning."""
    from vvs_engine.pdf.extract import extract_document
    from vvs_engine.pipeline import analyze_page
    from vvs_engine.text.external_labels import ExternalLabels
    facit = _local()
    labels = ExternalLabels.load(facit["labels"])
    pa = analyze_page(extract_document(facit["pdf"]).pages[sheet["page"]], external_labels=labels)
    families = {pp.family for pp in pa.ownership.pipes} if getattr(pa, "ownership", None) else \
        {m.pipe.family for m in pa.measures}
    for token in sheet["pipe_family_tokens"]:
        assert any(token in f for f in families), (sheet["name"], token, sorted(families))
    for name, metres in sheet["confirmed"].items():
        got = sum(float(q.get("confirmed_total_m") or 0) - float(q.get("review_m") or 0)
                  for q in pa.quantities if str(q.get("designation", "")).startswith(name))
        assert got >= metres, (sheet["name"], name, got)
    for tag in sheet["never_designations"]:
        assert not any(is_height_note(d.text) for d in pa.designations if d.text.upper().startswith(tag)), \
            (sheet["name"], [d.text for d in pa.designations if d.text.upper().startswith(tag)])
