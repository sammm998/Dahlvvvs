"""En råkontakt är ingen anslutning, och en flödesbudget är stråkets egen.

Två saker som kostade meter åt fel håll. Den ena: varje onämnd linje som rörde vid ett etiketterat stråk fick
stråkets namn - måttlinjer, väggkanter, fixturkonturer på samma penna mättes som rör. Nu måste grenen sluta i
något som säger att den är ett rör: en komponent, bladets kant, en annan pennas fortsättning eller bläck, eller
ett stråk som redan bär samma namn. Slutar den i tomma luften är den en fråga med ett svar som kandidat.

Den andra: budgeten för hur långt ett namn får rinna förbi sina etiketter summerades över hela pennan. Ett
stråk som vandrat in i ett väggnät tog de flödade metrarna av varje annat stråk på samma penna. Nu vägs varje
sammanhängande stråk mot sina egna etiketter, och grannen lämnas i fred.
"""
import pymupdf

from vvs_engine.pdf.extract import extract_document
from vvs_engine.pipeline import analyze_page, reading_coverage
from vvs_engine.pipes import frontier as F

PEN = 1.44
LEAD = 0.72


def _label(page, x, y, text, to):
    page.insert_text((x, y), text, fontsize=10, fontname="helv")
    page.draw_line((x, y + 3), (x + 62, y + 3), width=LEAD, color=(0, 0, 0))
    page.draw_line((x + 62, y + 3), to, width=LEAD, color=(0, 0, 0))
    page.draw_line((to[0] - 1, to[1] - 1), (to[0] + 1, to[1] + 1), width=LEAD, color=(0, 0, 0))


def _scale(page):
    page.insert_text((100, 560), "SKALA 1:50", fontsize=10, fontname="helv")
    for i in range(6):
        page.insert_text((300 + i * 56.69, 560), str(i), fontsize=8, fontname="helv")
    page.draw_line((302, 566), (302 + 5 * 56.69, 566), width=1.0, color=(0, 0, 0))


def _sheet(path, draw):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    draw(page)
    _scale(page)
    doc.save(path); doc.close()
    return analyze_page(extract_document(path).pages[0])


class _States:
    """Ägandet vid en punkt på bladet: primitiven som går genom punkten, vad den än delats i."""

    def __init__(self, pa):
        self.pa = pa

    def __getitem__(self, xy):
        from vvs_engine.geometry.core import point_seg_distance
        x, y = xy
        best = None
        for fk, sts in self.pa.ownership.prim_states.items():
            g = self.pa.graphs[fk]
            for pid, st in sts.items():
                d, _ = point_seg_distance(x, y, g.prims[pid].seg)
                if d <= 0.5 and (best is None or d < best[0]):
                    best = (d, st.state, st.reason)
        if best is None:
            raise KeyError(xy)
        return best[1], best[2]

    def __repr__(self):
        out = []
        for fk, sts in self.pa.ownership.prim_states.items():
            g = self.pa.graphs[fk]
            for pid, st in sts.items():
                s = g.prims[pid].seg
                out.append(f"({s.x0:.0f},{s.y0:.0f})-({s.x1:.0f},{s.y1:.0f}) {st.state} {st.reason}")
        return "\n".join(out)


def _states(pa):
    return _States(pa)


def _run_with_branch(branch_end_draw=None):
    def draw(page):
        page.draw_line((100, 300), (500, 300), width=PEN, color=(0, 0, 0))     # etiketterat stråk
        page.draw_line((300, 300), (300, 120), width=PEN, color=(0, 0, 0))     # onämnd gren i T
        for x in (120.0, 400.0):
            _label(page, x, 220.0, "KV11-16", (x + 60, 300.0))
        if branch_end_draw:
            branch_end_draw(page)
    return draw


def test_a_branch_that_ends_in_empty_air_inherits_with_an_uncertain_size(tmp_path):
    """VS-Kalkyl SPEC 6.4.3 h.3 (beslut 2026-11-03): en gren utan egen etikett ärver system och material, med
    osäker dimension. Den mäts som ett eget rör i granskningsnivån - aldrig som säker - och huvudstråket står.
    Med regeln av är grenen en fråga, som förut."""
    from vvs_engine.rules import using
    pa = _sheet(str(tmp_path / "luft.pdf"), _run_with_branch())
    st = _states(pa)
    assert st[(300, 210)][0] == "CONFIRMED", st
    assert st[(200, 300)][0] == "CONFIRMED" and st[(400, 300)][0] == "CONFIRMED", "huvudstråket är helt"
    pipes = [p for p in pa.ownership.pipes if p.identity.display == "KV11-16"]
    main = [p for p in pipes if not p.needs_review]
    assert len(main) == 1 and abs(main[0].raw_length_pt - 400) < 1.0, "stråkets kontinuitet bevaras genom T:t"
    assert [p for p in pipes if p.needs_review], "grenen är ett eget rör, att granska"
    q = {r["designation"]: r for r in pa.quantities}
    assert abs(q["KV11-16"]["review_m"] - 180 / 56.69) < 0.1, "grenens meter redovisas, att granska"
    assert q["KV11-16"]["ambiguous_m"] == 0
    with using({"pipes.ownership.PROPAGATE_TO_STOPS": False}):
        pa = _sheet(str(tmp_path / "luft2.pdf"), _run_with_branch())
    assert _states(pa)[(300, 210)] == ("AMBIGUOUS", "UNLABELLED_BRANCH_WITHOUT_END_EVIDENCE")


def test_a_branch_that_ends_in_a_component_takes_the_name(tmp_path):
    def comp(page):
        page.draw_rect(pymupdf.Rect(296, 112, 304, 120), color=(0, 0, 1), width=0.5)
        page.draw_line((296, 112), (304, 120), width=0.5, color=(0, 0, 1))
    pa = _sheet(str(tmp_path / "komp.pdf"), _run_with_branch(comp))
    st = _states(pa)
    assert st[(300, 210)] == ("CONFIRMED", "unlabeled_branch_takes_the_only_junction_identity"), st
    q = {r["designation"]: r for r in pa.quantities}
    assert abs(q["KV11-16"]["confirmed_horizontal_m"] - 580 / 56.69) < 0.1 and q["KV11-16"]["ambiguous_m"] == 0


def test_a_branch_that_ends_against_another_pens_ink_takes_the_name(tmp_path):
    def fixture(page):
        # en tvättställskontur på arkitektens penna, som grenen når fram till
        page.draw_rect(pymupdf.Rect(270, 80, 330, 120), color=(0.4, 0.4, 0.4), width=0.3)
    pa = _sheet(str(tmp_path / "fixtur.pdf"), _run_with_branch(fixture))
    st = _states(pa)
    assert st[(300, 210)][0] == "CONFIRMED", st
    ev = [e for sts in pa.ownership.prim_states.values() for s in sts.values() for e in s.evidence]
    assert any(e.startswith("branch_ends_at_ENDS_AT_OTHER_INK") or e.startswith("branch_ends_at_SYMBOL") for e in ev), ev[:12]


def test_a_branch_that_leaves_the_sheet_takes_the_name(tmp_path):
    def draw(page):
        page.draw_line((100, 300), (500, 300), width=PEN, color=(0, 0, 0))
        page.draw_line((300, 300), (300, 4), width=PEN, color=(0, 0, 0))       # ut ur bladet
        for x in (120.0, 400.0):
            _label(page, x, 220.0, "KV11-16", (x + 60, 300.0))
    pa = _sheet(str(tmp_path / "kant.pdf"), draw)
    st = _states(pa)
    assert st[(300, 152)][0] == "CONFIRMED", st


def test_a_branch_into_an_unnamed_grid_stays_a_question(tmp_path):
    def draw(page):
        page.draw_line((100, 300), (500, 300), width=PEN, color=(0, 0, 0))
        page.draw_line((300, 300), (300, 100), width=PEN, color=(0, 0, 0))     # gren in i ett nät
        for y in (100, 160, 220):                                              # nätet: väggar på samma penna
            page.draw_line((200, y), (700, y), width=PEN, color=(0, 0, 0))
        page.draw_line((700, 100), (700, 220), width=PEN, color=(0, 0, 0))
        page.draw_line((200, 100), (200, 220), width=PEN, color=(0, 0, 0))
        for x in (120.0, 400.0):
            _label(page, x, 220.0, "KV11-16", (x + 60, 300.0))
    pa = _sheet(str(tmp_path / "nat.pdf"), draw)
    q = {r["designation"]: r for r in pa.quantities}
    assert abs(q["KV11-16"]["confirmed_horizontal_m"] - 400 / 56.69) < 0.1, "bara det etiketterade stråket är säkert"
    st = _states(pa)
    assert st[(300, 260)][0] == "AMBIGUOUS", st[(300, 260)]
    assert st[(450, 160)][0] != "CONFIRMED", "nätet får inte bli rör"


def test_the_flow_budget_is_the_runs_own_not_the_pens(tmp_path):
    """Två stråk på samma penna. A rinner 150 pt förbi sin etikett, B 400 pt. Med en budget summerad över
    pennan (550 mot 200 etiketterat, 2,75 gånger) förlorade båda sina flödade meter; med stråkets egen budget
    förlorar bara B. Gäller med spridningsregeln av; med den på (SPEC 6.4.3 h.3) har ett träd av rör ingen
    längdbudget, och båda stråken står - se nedan."""
    from vvs_engine.rules import using
    def draw(page):
        # A: etiketterad del 100..200, T-stump vid 200, rakt fortsättning 200..350 (150 pt, 1,5 gånger)
        page.draw_line((100, 200), (200, 200), width=PEN, color=(0, 0, 0))
        page.draw_line((200, 200), (350, 200), width=PEN, color=(0, 0, 0))
        page.draw_line((200, 200), (200, 170), width=PEN, color=(0, 0, 0))
        _label(page, 60.0, 120.0, "KV11-16", (150.0, 200.0))
        # B: etiketterad del 100..200, T-stump vid 200, rakt fortsättning 200..600 (400 pt, 4 gånger)
        page.draw_line((100, 400), (200, 400), width=PEN, color=(0, 0, 0))
        page.draw_line((200, 400), (600, 400), width=PEN, color=(0, 0, 0))
        page.draw_line((200, 400), (200, 370), width=PEN, color=(0, 0, 0))
        _label(page, 60.0, 320.0, "VV11-16", (150.0, 400.0))
    with using({"pipes.ownership.PROPAGATE_TO_STOPS": False}):
        pa = _sheet(str(tmp_path / "budget.pdf"), draw)
    st = _states(pa)
    assert st[(275, 200)] == ("CONFIRMED", "collinear_through_junction"), st[(275, 200)]
    assert st[(400, 400)] == ("AMBIGUOUS", "AMBIGUOUS_FLOW_BEYOND_THE_LABELLED_RUNS"), st[(400, 400)]
    runs = [r for r in pa.ownership.ambiguous_runs if r["reason"] == "AMBIGUOUS_FLOW_BEYOND_THE_LABELLED_RUNS"]
    assert len(runs) == 1 and runs[0]["identities"] == ["VV11|DN16"] and runs[0]["labelled_pt"] < runs[0]["flowed_pt"]
    fr = reading_coverage(pa)["frontiers"]["by_reason"]
    assert fr.get(F.FLOW_BUDGET, 0) >= 1
    # spridning till stopp: en rak fortsättning i ett träd har ingen längdbudget
    pa = _sheet(str(tmp_path / "budget2.pdf"), draw)
    assert _states(pa)[(400, 400)] == ("CONFIRMED", "collinear_through_junction")
    assert not [r for r in pa.ownership.ambiguous_runs if r["reason"] == "AMBIGUOUS_FLOW_BEYOND_THE_LABELLED_RUNS"]
