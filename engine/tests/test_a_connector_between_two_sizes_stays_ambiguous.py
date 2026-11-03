"""En förbindelse mellan två dimensioner får inte avgöras av numreringen.

Två lodräta stråk står bredvid varandra, det vänstra DN20 och det högra DN40, och en vågrät förbindelse går
mellan dem. Förbindelsen har ingen egen dimensionsgräns - ingen tick, ingen etikett - så ritningen säger inte
vilken av de två den är.

Läsningen avgjorde den ändå. Den onämnda förbindelsen tog identiteten från den korsning den råkade nå först,
och vilken det blev berodde på i vilken ordning primitiverna hade numrerats. Samma ritning, ritad med objekten
i en annan ordning, gav DN20 i stället för DN40 - och båda redovisades som CONFIRMED.

Det är två fel i ett: mängden blir olika för samma ritning, och den blir säker på något ritningen inte säger.
Provet håller fast båda: samma figur ritad i två ordningar ska ge samma mängd, och förbindelsen ska stanna
tvetydig tills ritningen ger en gräns eller en identitet.
"""
import pymupdf

from vvs_engine.pdf.extract import extract_document
from vvs_engine.pipeline import analyze_page

PEN = 1.44
LEAD = 0.72


def _label(page, x: float, y: float, text: str, to: tuple[float, float]) -> None:
    """En etikett med sitt understreck och sin hänvisningslinje ned till röret, med bock i änden."""
    page.insert_text((x, y), text, fontsize=10, fontname="helv")
    page.draw_line((x, y + 3), (x + 62, y + 3), width=LEAD, color=(0, 0, 0))
    page.draw_line((x + 62, y + 3), to, width=LEAD, color=(0, 0, 0))
    page.draw_line((to[0] - 1, to[1] - 1), (to[0] + 1, to[1] + 1), width=LEAD, color=(0, 0, 0))


def _scale(page) -> None:
    page.insert_text((100, 560), "SKALA 1:50", fontsize=10, fontname="helv")
    for i in range(6):
        page.insert_text((300 + i * 56.69, 560), str(i), fontsize=8, fontname="helv")
    page.draw_line((302, 566), (302 + 5 * 56.69, 566), width=1.0, color=(0, 0, 0))


def _sheet(path: str, order: str) -> str:
    """Samma figur varje gång; bara i vilken ordning objekten skrivs till filen skiljer.

    Stråken ritas delade på mitten, som ett CAD-utdrag gör där en förbindelse möter dem.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)

    left = [((200.0, 150.0), (200.0, 250.0)), ((200.0, 250.0), (200.0, 350.0))]
    right = [((400.0, 150.0), (400.0, 250.0)), ((400.0, 250.0), (400.0, 350.0))]
    link = [((200.0, 250.0), (400.0, 250.0))]
    parts = {"vanster-forst": left + right + link,
             "forbindelsen-forst": link + right + left}[order]
    for a, b in parts:
        page.draw_line(a, b, width=PEN, color=(0, 0, 0))

    # två etiketter per stråk: en ensam etikett räcker inte för att en penna alls ska tas som rör
    _label(page, 90.0, 120.0, "KV01-X7-20", (200.0, 180.0))
    _label(page, 90.0, 400.0, "KV01-X7-20", (200.0, 330.0))
    _label(page, 520.0, 120.0, "KV01-X7-40", (400.0, 180.0))
    _label(page, 520.0, 400.0, "KV01-X7-40", (400.0, 330.0))
    _scale(page)
    doc.save(path)
    doc.close()
    return path


def _read(path: str):
    pa = analyze_page(extract_document(path).pages[0])
    rows = {q["designation"]: round(q["confirmed_horizontal_m"], 3) for q in pa.quantities}
    return rows, pa


def test_the_same_figure_in_another_drawing_order_gives_the_same_takeoff(tmp_path):
    a, _ = _read(_sheet(str(tmp_path / "a.pdf"), "vanster-forst"))
    b, _ = _read(_sheet(str(tmp_path / "b.pdf"), "forbindelsen-forst"))
    assert a == b, f"ritordningen ändrade mängden: {a} mot {b}"


def test_the_connector_does_not_become_one_of_the_two_sizes(tmp_path):
    """Förbindelsen är 200 punkter, 3,53 m i 1:50. Den får aldrig bli *säkert* en av de två dimensionerna.
    VS-Kalkyl SPEC 6.4.3 h.3: en gren utan egen etikett ärver system och material med osäker dimension - den mäts
    i granskningsnivån, inte som säker."""
    rows, pa = _read(_sheet(str(tmp_path / "c.pdf"), "vanster-forst"))
    for q in pa.quantities:
        sure = q["confirmed_horizontal_m"] - q.get("review_m", 0.0)
        assert sure < 5.0, f"{q['designation']} fick {sure} m säkert - förbindelsen tillhör ingen dimension säkert"


def test_what_the_drawing_does_not_say_is_reported_as_ambiguous(tmp_path):
    """Förbindelsen ska synas som tvetydig eller onämnd - inte tyst försvinna och inte bli säker."""
    from vvs_engine.pipeline import reading_coverage
    _, pa = _read(_sheet(str(tmp_path / "d.pdf"), "vanster-forst"))
    c = reading_coverage(pa)
    review = sum(q.get("review_m", 0.0) for q in pa.quantities)
    assert (c["ambiguous_m"] or 0) + (c["unowned_m"] or 0) + review > 1.0, (
        "det ritningen inte avgör ska redovisas - som fråga eller att granska - inte gissas")
