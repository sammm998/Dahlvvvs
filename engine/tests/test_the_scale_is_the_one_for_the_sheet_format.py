"""Skalan är den som stämpeln anger för bladets eget format - och den är verifierad först när något mätt den.

En stämpel skriver ofta en skala per utskriftsformat: "SKALA 1:50 (A1) 1:100 (A3)". Bladet själv avgör vilken som
gäller: dess storlek i punkter är ett A-format. På ett A1-blad gäller 1:50; en skala angiven för A3 gäller på
A1-bladet omräknad med formatsteget (två A-steg = halva längden), alltså också 1:50.

Felet som provet håller borta (Årjäng, W-50-1-1000): A1-raden var oläslig, A3-raden läste rent, och en skrafferad
brandvägg tolkades som en omärkt skalstock vars längd "gick jämnt upp" i 1:100. Bladet mättes i 1:100 - varje meter
dubbel - och stod som VERIFIED. En stock utan läsbara siffror ger punkter, inte meter, och kan varken välja eller
bekräfta en skala. VERIFIED kräver en oberoende mätning på bladet (märkt skalstock eller måttsättning) inom 1 %;
annars är skalan overifierad och varje meter under den bär SCALE_UNVERIFIED.
"""
import json
import os

import pymupdf
import pytest

from vvs_engine.measure.measure import scale_standing
from vvs_engine.measure.scale import VERIFY_TOL, ScaleResult, discover_scale, hatch_bands
from vvs_engine.pdf.extract import extract_document
from vvs_engine.pipeline import searchable_rows

A1 = (2384.0, 1684.0)
A3 = (1191.0, 842.0)
MM_PER_PT = 25.4 / 72


def _ratio(sc: ScaleResult) -> float:
    return sc.meters_per_pt * 1000 / MM_PER_PT


def _scale_bar(page, x0, y0, metres_per_pt, labels=(0, 2, 5)):
    """En märkt skalstock: baslinje, hela och halva delstreck, siffror under."""
    length = max(labels) / metres_per_pt
    page.draw_line((x0, y0), (x0 + length, y0), width=0.5, color=(0, 0, 0))
    for k in range(int(max(labels)) + 1):
        x = x0 + k / metres_per_pt
        page.draw_line((x, y0 - 5), (x, y0 + 5), width=0.5, color=(0, 0, 0))
        if k < max(labels):
            xh = x + 0.5 / metres_per_pt
            page.draw_line((xh, y0 - 3), (xh, y0 + 3), width=0.5, color=(0, 0, 0))
    for v in labels:
        page.insert_text((x0 + v / metres_per_pt - 2, y0 + 16), str(v), fontsize=9, fontname="helv")
    page.insert_text((x0 - 2, y0 + 30), "METER", fontsize=9, fontname="helv")


def _sheet(path, size, stamps, hatched_wall=False, notes=(), bar=None, numbered_wall=False):
    doc = pymupdf.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.draw_line((100, 300), (900, 300), width=1.44, color=(0, 0, 0))     # en ledning
    for i, stamp in enumerate(stamps):
        page.insert_text((size[0] - 330, size[1] - 80 + 12 * i), stamp, fontsize=8, fontname="helv")
    for x, y, text in notes:
        page.insert_text((x, y), text, fontsize=8, fontname="helv")
    if hatched_wall:
        # en vägg ritad som baslinje med tät snedskraffering - samma geometri som en omärkt skalstock
        x0, y0, length, step = 686.2, 1103.1, 655.2, 12.06
        page.draw_line((x0, y0), (x0 + length, y0), width=0.25, color=(0, 0, 0))
        k = 0
        while k * step <= length:
            x = x0 + k * step
            page.draw_line((x - 4, y0 + 4), (x + 4, y0 - 4), width=0.18, color=(0, 0, 0))
            k += 1
    if numbered_wall:
        # siffror 0 2 5 under väggen, som rutnätsaxlar: väggen blir ändå ingen skalstock
        for v, dx in ((0, 0.0), (2, 200.0), (5, 500.0)):
            page.insert_text((686.2 + dx, 1103.1 + 16), str(v), fontsize=9, fontname="helv")
    if bar is not None:
        _scale_bar(page, *bar)
    doc.save(path)
    doc.close()
    return path


def _read(tmp_path, name, size, stamps, **kw):
    pg = extract_document(_sheet(str(tmp_path / f"{name}.pdf"), size, stamps, **kw)).pages[0]
    return pg, discover_scale(pg, searchable_rows(pg))


def test_a1_sheet_takes_the_ratio_stated_for_a1(tmp_path):
    _, sc = _read(tmp_path, "a1", A1, ["SKALA 1:50 (A1)", "1:100 (A3)"])
    assert abs(_ratio(sc) - 50) < 0.05, sc.reason
    assert sc.state == "TEXT_ONLY", "inget på bladet mätte skalan - den är inte verifierad"


def test_a3_sheet_takes_the_ratio_stated_for_a3(tmp_path):
    _, sc = _read(tmp_path, "a3", A3, ["SKALA 1:50 (A1)", "1:100 (A3)"])
    assert abs(_ratio(sc) - 100) < 0.1, sc.reason


def test_the_sheet_format_holds_in_either_orientation(tmp_path):
    _, sc = _read(tmp_path, "a1p", (A1[1], A1[0]), ["SKALA 1:50 (A1)", "1:100 (A3)"])
    assert abs(_ratio(sc) - 50) < 0.05, sc.reason


def test_a_ratio_for_another_format_is_rescaled_not_taken_as_is(tmp_path):
    # A1-raden oläslig: bara A3-raden finns kvar. Den gäller omräknad, inte som den står.
    _, sc = _read(tmp_path, "a3only", A1, ["SKALA 1:100 (A3)"])
    assert abs(_ratio(sc) - 50) < 0.05, sc.reason
    assert "rescaled" in sc.reason and sc.state == "TEXT_ONLY"


def test_a_hatched_wall_neither_chooses_nor_confirms_a_scale(tmp_path):
    pg, sc = _read(tmp_path, "wall", A1, ["SKALA 1:100 (A3)"], hatched_wall=True)
    lines = searchable_rows(pg)
    assert any(b[1] < 1103.1 < b[3] for b in hatch_bands(pg, lines)), "väggen ska kännas igen som skraffering"
    assert abs(_ratio(sc) - 50) < 0.05, sc.reason
    assert sc.state != "VERIFIED", sc.reason


def test_a_hatched_wall_with_figures_beside_it_is_not_a_scale_bar(tmp_path):
    # siffror 0, 2, 5 i rätt avstånd under en skrafferad vägg: stocksökningen ska inte bygga en stock på väggen
    _, sc = _read(tmp_path, "numbered", A1, ["SKALA 1:50 (A1)"], hatched_wall=True, numbered_wall=True)
    assert not [e for e in sc.evidence if e.kind == "scale_bar"], sc.evidence
    assert sc.state == "TEXT_ONLY", sc.reason


def test_a_labelled_scale_bar_verifies_the_stated_ratio(tmp_path):
    mpp = 50 * MM_PER_PT / 1000
    _, sc = _read(tmp_path, "bar", A1, ["SKALA 1:100 (A3)"], bar=(1637.0, 1550.0, mpp))
    assert sc.state == "VERIFIED", sc.reason
    assert abs(_ratio(sc) - 50) < 0.05, sc.reason


def test_a_scale_bar_is_never_read_as_hatching(tmp_path):
    mpp = 50 * MM_PER_PT / 1000
    pg, _ = _read(tmp_path, "barhatch", A1, ["SKALA 1:50 (A1)"], bar=(1637.0, 1550.0, mpp))
    assert not [b for b in hatch_bands(pg, searchable_rows(pg)) if b[1] < 1550.0 < b[3]]


def test_format_tied_ratios_that_disagree_are_a_conflict(tmp_path):
    # 1:200 (A3) omräknat till A1 är 1:100 - inte samma som 1:50 (A1). Ingen skala antas.
    _, sc = _read(tmp_path, "clash", A1, ["SKALA 1:50 (A1)", "1:200 (A3)"])
    assert sc.state == "CONFLICT" and sc.meters_per_pt is None, sc.reason


def test_a_bare_ratio_on_a_stamped_sheet_is_not_the_scale(tmp_path):
    # ett fall skrivet 1:100 vid en ledning är ingen skala när stämpeln knyter sina skalor till format
    _, sc = _read(tmp_path, "fall", A1, ["SKALA 1:50 (A1)"], notes=[(400, 290, "1:100")])
    assert abs(_ratio(sc) - 50) < 0.05, sc.reason


def test_only_a_verified_scale_gives_confirmed_metres():
    mpp = 50 * MM_PER_PT / 1000
    assert scale_standing(ScaleResult(mpp, "page", "VERIFIED", [], "bar")) is None
    for state in ("TEXT_ONLY", "BAR_ONLY", "DIMENSIONS_ONLY"):
        assert scale_standing(ScaleResult(mpp, "page", state, [], "x")) == "SCALE_UNVERIFIED", state


def test_verification_tolerance_is_one_percent():
    assert VERIFY_TOL == 0.01


# ---- lokal regression mot riktiga blad ---------------------------------------------------------------------
#
# Ritningarna är kundmaterial och ligger utanför repot. Manifestet anger blad och förväntad skala:
#   data/local_facit/scale.json (eller VVS_LOCAL_SCALE_FACIT):
#   {"sheets": [{"name": "W-50-1-1000", "pdf": "/sökväg/ritningar.pdf", "page": 1, "ratio": 50,
#                "state": "VERIFIED", "scale_bar_bbox": [x0, y0, x1, y1], "hatch_points": [[x, y], ...]}, ...]}

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_FACIT = os.environ.get("VVS_LOCAL_SCALE_FACIT", os.path.join(ROOT, "data", "local_facit", "scale.json"))


def _local_sheets():
    if not os.path.isfile(LOCAL_FACIT):
        return []
    with open(LOCAL_FACIT, encoding="utf-8") as fh:
        return [s for s in json.load(fh).get("sheets", []) if os.path.isfile(s.get("pdf", ""))]


def _overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


@pytest.mark.skipif(not _local_sheets(), reason="lokalt skalfacit saknas (data/local_facit/scale.json)")
@pytest.mark.parametrize("sheet", _local_sheets(), ids=lambda s: s.get("name", "blad"))
def test_local_sheet_is_read_in_its_stated_scale(sheet):
    """Varje blad i manifestet: skala och tillstånd enligt facit; har bladet en skalstock ska den vara läst
    där facit säger (`scale_bar_bbox`); ingen skraffering får bli stockkandidat, och skrafferingar facit pekar ut
    (`hatch_points`) ska kännas igen som skraffering."""
    from vvs_engine.pipeline import analyze_page
    page = extract_document(sheet["pdf"]).pages[sheet["page"]]
    pa = analyze_page(page)
    assert pa.scale.meters_per_pt is not None, pa.scale.reason
    ratio = _ratio(pa.scale)
    assert abs(ratio / sheet["ratio"] - 1) <= VERIFY_TOL, (sheet["name"], ratio, pa.scale.reason)
    assert pa.scale.state == sheet["state"], (sheet["name"], pa.scale.state, pa.scale.reason)
    bars = [e for e in pa.scale.evidence if e.kind == "scale_bar"]
    if sheet.get("scale_bar_bbox"):
        assert any(_overlaps(e.bbox, sheet["scale_bar_bbox"]) for e in bars), (sheet["name"], [e.bbox for e in bars])
    bands = hatch_bands(page, pa.lines)
    for e in bars:
        assert not any(_overlaps(e.bbox, b) for b in bands), (sheet["name"], "skraffering blev stock", e.bbox)
    for x, y in sheet.get("hatch_points", []):
        assert any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in bands), (sheet["name"], "skraffering missad", x, y)
