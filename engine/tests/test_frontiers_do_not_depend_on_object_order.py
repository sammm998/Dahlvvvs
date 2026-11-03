"""Fronterna är samma fronter hur PDF:en än numrerar sina objekt.

Determinismprovet hashar läsningens semantik för original, omvänd och två slumpade ordningar. Fronterna
ingår i signaturen: en kant som byter skäl beroende på i vilken ordning objekten råkade komma är inte en
läsning av ritningen utan av filens numrering.
"""
import pymupdf

from vvs_engine.determinism import run_determinism, semantic_signature
from vvs_engine.pdf.extract import extract_document
from vvs_engine.pipeline import analyze_page

PEN = 1.44
LEAD = 0.72


def _label(page, x, y, text, to):
    page.insert_text((x, y), text, fontsize=10, fontname="helv")
    page.draw_line((x, y + 3), (x + 62, y + 3), width=LEAD, color=(0, 0, 0))
    page.draw_line((x + 62, y + 3), to, width=LEAD, color=(0, 0, 0))
    page.draw_line((to[0] - 1, to[1] - 1), (to[0] + 1, to[1] + 1), width=LEAD, color=(0, 0, 0))


def _sheet(path):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.draw_line((100, 300), (400, 300), width=PEN, color=(0, 0, 0))
    page.draw_line((400, 300), (700, 300), width=PEN, color=(0, 0, 0))
    page.draw_line((300, 300), (300, 120), width=PEN, color=(0, 0, 0))                  # gren utan bevis
    page.draw_line((550, 300), (550, 450), width=PEN, color=(0, 0, 0))                  # gren till en apparat
    page.draw_rect(pymupdf.Rect(546, 450, 554, 458), color=(0, 0, 1), width=0.5)
    for x in (120.0, 250.0):
        _label(page, x, 220.0, "KV11-16", (x + 60, 300.0))
    for x in (450.0, 600.0):
        _label(page, x, 220.0, "KV11-22", (x + 60, 300.0))
    page.insert_text((100, 560), "SKALA 1:50", fontsize=10, fontname="helv")
    for i in range(6):
        page.insert_text((300 + i * 56.69, 560), str(i), fontsize=8, fontname="helv")
    page.draw_line((302, 566), (302 + 5 * 56.69, 566), width=1.0, color=(0, 0, 0))
    doc.save(path); doc.close()
    return path


def test_the_frontiers_are_part_of_the_signature_and_survive_every_order(tmp_path):
    doc = extract_document(_sheet(str(tmp_path / "ordning.pdf")))
    pa = analyze_page(doc.pages[0])
    sig = semantic_signature(pa)
    assert sig["frontiers"], "fronterna ska stå i signaturen"
    reasons = {r for r, _, _ in sig["frontiers"]}
    # grenen utan bevis ärver (SPEC 6.4.3 h.3) och ger ingen tvetydig korsning längre; det provet gäller är ordningen
    assert {"REAL_DN_BOUNDARY", "SYMBOL"} <= reasons, reasons
    rep = run_determinism(doc, 0, base_pa=pa)
    assert rep["state"] == "PASS", rep
    assert len(set(rep["hashes"].values())) == 1
