"""Post-validation generic rules: tick marks as drawn DN boundaries, DN rows with qualifiers, leader start
conflicts, bundle end-marker clusters. Synthetic PDFs only."""
import os

import pymupdf

from vvs_engine.measure.scale import discover_scale
from vvs_engine.pdf.extract import extract_document
from vvs_engine.pipeline import analyze_page
from vvs_engine.semantics.annotation import _row_role
from vvs_engine.text.vector_text import vector_text_rows
from tests.conftest import draw_hershey_text, make_dashed_line


def _label(page, shape, x, y, text, dn_row=None, leader_to=None, tick=True):
    page.insert_text((x, y), text, fontsize=10, fontname="helv")
    ul_y = y + 2
    if dn_row is not None:
        page.insert_text((x + 20, y + 12), dn_row, fontsize=10, fontname="helv")
        ul_y = y + 14
    shape.draw_line((x, ul_y), (x + 70, ul_y)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    if leader_to is not None:
        shape.draw_line((x + 70, ul_y), leader_to); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        if tick:
            ex, ey = leader_to
            shape.draw_line((ex - 1, ey - 1), (ex + 1, ey + 1)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)


def _scale(page):
    page.insert_text((100, 560), "SKALA 1:50", fontsize=10, fontname="helv")


def test_tick_on_dead_end_stub_is_dn_boundary(tmp_path):
    """Main run S3-R8-110 (two labels). A stub branches off to a dead end; its S3-R8-75 label tick sits 60 pt
    down the stub. The junction's DN flows into the stub up to the tick; beyond the tick the stub is 75."""
    path = os.path.join(tmp_path, "stub.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 300), (700, 300))            # main run, y = 300
    make_dashed_line(shape, (400, 300), (400, 420))            # stub down to a dead end at y = 420
    _label(page, shape, 120, 200, "S3-R8-110", leader_to=(200, 300))
    _label(page, shape, 560, 200, "S3-R8-110", leader_to=(640, 300))
    _label(page, shape, 470, 380, "S3-R8", dn_row="75", leader_to=(400, 360))   # tick 60 pt below the junction
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    assert ("S3-R8", 110) in q and ("S3-R8", 75) in q
    # 600 pt main run + 60 pt of stub = 660 pt of DN110 (11.64 m); 60 pt of DN75 (1.06 m); dashes lose the last gap
    assert abs(q[("S3-R8", 110)]["confirmed_horizontal_m"] - 660 / 56.69) < 0.35
    assert abs(q[("S3-R8", 75)]["confirmed_horizontal_m"] - 60 / 56.69) < 0.3
    assert sum(r["ambiguous_m"] for r in pa.quantities) == 0
    reasons = {st.reason for sts in pa.ownership.prim_states.values() for st in sts.values()}
    assert "through_junction_up_to_tick_boundary" in reasons


def test_tick_mid_branch_that_continues_keeps_its_label(tmp_path):
    """Same as above but the branch continues past the tick to another junction: the tick is the label's pointer
    onto this branch, so the whole branch keeps the label's DN (never the main run's DN)."""
    path = os.path.join(tmp_path, "branch.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 300), (700, 300))
    make_dashed_line(shape, (400, 300), (400, 480))
    make_dashed_line(shape, (300, 480), (500, 480))            # the branch ends in a T (continues)
    _label(page, shape, 120, 200, "S3-R8-110", leader_to=(200, 300))
    _label(page, shape, 560, 200, "S3-R8-110", leader_to=(640, 300))
    _label(page, shape, 470, 380, "S3-R8", dn_row="75", leader_to=(400, 360))
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    assert abs(q[("S3-R8", 110)]["confirmed_horizontal_m"] - 600 / 56.69) < 0.35
    assert q[("S3-R8", 75)]["confirmed_horizontal_m"] > 180 / 56.69 - 0.4     # the whole branch (180 pt) stays DN75
    ev = [e for sts in pa.ownership.prim_states.values() for st in sts.values() for e in st.evidence]
    assert any("taken_as_label_pointer" in e for e in ev)


def test_dn_row_with_qualifier():
    class R:  # minimal stand-ins: _row_role only reads the text for these cases
        underline = []
    assert _row_role("75(L)", R(), None) == "dn"
    assert _row_role("110", R(), None) == "dn"
    assert _row_role("2ST", R(), None) != "dn"
    assert _row_role("S3-R8-75", R(), None) == "designation"


def test_leader_from_shared_frame_corner_goes_to_block_it_leaves(tmp_path):
    """A designation block and a note block have frame lines meeting at one point; the leader starting there
    belongs to the block it points away from, not to the first block by id."""
    path = os.path.join(tmp_path, "shared.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 400), (700, 400))
    page.insert_text((150, 300), "VS21-S13-15-F50", fontsize=10, fontname="helv")
    shape.draw_line((150, 302), (240, 302)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    page.insert_text((400, 300), "SE PLAN", fontsize=10, fontname="helv")
    shape.draw_line((240, 302), (480, 302)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    # leader from the shared point (240, 302) down-right to the pipe: it leaves the LEFT block only
    shape.draw_line((240, 302), (300, 400)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    shape.draw_line((299, 399), (301, 401)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    # a second label of the same designation elsewhere (tick votes for the pipe family)
    page.insert_text((500, 200), "VS21-S13-15-F50", fontsize=10, fontname="helv")
    shape.draw_line((500, 202), (590, 202)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    shape.draw_line((590, 202), (640, 400)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    shape.draw_line((639, 399), (641, 401)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    ver = [a for a in pa.anchors if a.state == "VERIFIED_PIPE_ATTACHMENT"]
    assert len(ver) == 2, [(a.designation, a.state, a.reason) for a in pa.anchors]
    assert {a.designation for a in ver} == {"VS21-S13-15-F50"}
    starts = sorted(tuple(round(v) for v in a.endpoint) for a in ver)
    assert starts == [(300, 400), (640, 400)]


def test_hatched_area_is_reported_separately(tmp_path):
    """A pipe whose east half runs through a hatched area (regularly spaced 45-degree strokes) is measured in
    full, and the part inside the hatch is reported separately."""
    path = os.path.join(tmp_path, "hatch.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 300), (700, 300))
    _label(page, shape, 120, 200, "S3-R8-110", leader_to=(200, 300))
    _label(page, shape, 300, 200, "S3-R8-110", leader_to=(380, 300))
    # hatch: 45-degree strokes every 9 pt covering x 400..700, y 150..450
    x = 400 - 300
    while x < 700 + 300:
        x0, y0 = max(400, x), 450 - (max(400, x) - x)
        x1, y1 = min(700, x + 300), 450 - (min(700, x + 300) - x)
        if x1 > x0:
            shape.draw_line((x0, y0), (x1, y1)); shape.finish(width=0.36, color=(0.5, 0.5, 0.5), closePath=False)
        x += 9
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    assert len(pa.hatch_families) == 1 and abs(pa.hatch_families[0].spacing - 9.0 / 2 ** 0.5) < 1.0
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    row = q[("S3-R8", 110)]
    # horizontal quantity = drawn length outside the hatch (300 pt); the hatched half is reported separately
    assert abs(row["confirmed_horizontal_m"] - 300 / 56.69) < 0.5
    assert abs(row["in_hatched_area_m"] - 300 / 56.69) < 0.5


def test_scale_bar_is_measured_from_its_own_drawn_extent(tmp_path):
    """The bar is drawn for a whole number of metres; the label glyph centres sit a fraction of a character off
    the graduations. The bar's own extent is the measurement, so the ratio comes out exact."""
    path = os.path.join(tmp_path, "bar.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    span = 5 * 56.69                       # 5 m at 1:50
    for i in range(6):
        page.insert_text((300 + i * span / 5, 560), str(i), fontsize=8, fontname="helv")
    shape.draw_line((301.5, 566), (301.5 + span, 566)); shape.finish(width=1.0, color=(0, 0, 0), closePath=False)
    shape.commit(); doc.save(path); doc.close()
    pg = extract_document(path).pages[0]
    from vvs_engine.text.searchable import searchable_rows
    sc = discover_scale(pg, searchable_rows(pg))
    bar = [e for e in sc.evidence if e.kind == "scale_bar"]
    assert bar and bar[0].detail["measured_from"] == "bar_extent"
    assert abs(bar[0].detail["implied_ratio"] - 50.0) < 0.5


def test_scale_text_selected_by_sheet_format(tmp_path):
    """'SKALA A1 (A3)' over '1:50 (1:100)': the k-th format belongs to the k-th ratio, so an A1 sheet is 1:50.
    Without that pairing two ratios are a conflict and no scale may be assumed."""
    def build(fmt_row):
        path = os.path.join(tmp_path, f"fmt{abs(hash(fmt_row))}.pdf")
        doc = pymupdf.open()
        page = doc.new_page(width=2384, height=1684)          # A1
        page.insert_text((2048, 1600), fmt_row, fontsize=6, fontname="helv")
        page.insert_text((2050, 1612), "1:50 (1:100)", fontsize=9, fontname="helv")
        doc.save(path); doc.close()
        return extract_document(path).pages[0]
    from vvs_engine.text.searchable import searchable_rows
    pg = build("SKALA A1 (A3)")
    sc = discover_scale(pg, searchable_rows(pg))
    assert sc.state == "TEXT_ONLY" and abs(sc.meters_per_pt - 50 * 25.4 / 72 / 1000) < 1e-9
    assert "sheet_format_A1" in sc.reason
    pg = build("SKALA")
    assert discover_scale(pg, searchable_rows(pg)).meters_per_pt is None


def test_long_stroke_never_joins_a_text_row(tmp_path):
    """A frame edge or scale-bar end passing next to a label is drawing geometry: gluing it onto the label as a
    dash would destroy the label (here: the '0' of a scale bar)."""
    path = os.path.join(tmp_path, "stroke.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    draw_hershey_text(shape, "0", 300, 300, 9.0)
    shape.draw_line((300.5, 302), (300.5, 319)); shape.finish(width=0.96, color=(0, 0, 0), closePath=False)
    draw_hershey_text(shape, "2", 340, 300, 9.0)
    draw_hershey_text(shape, "3", 380, 300, 9.0)
    shape.commit(); doc.save(path); doc.close()
    rows = vector_text_rows(extract_document(path).pages[0]).rows
    texts = {r.text for r in rows}
    assert "0" in texts, f"the digit must stay its own row, got {texts}"
    assert not any(len(t) > 1 and t[0] == "-" for t in texts)


def test_designation_clipped_by_a_drawing_boundary_is_completed_from_the_drawing(tmp_path):
    """A sheet-part boundary cuts a label in half: the last character's ink stops dead at the line. The reading
    is completed from what this drawing overwhelmingly writes, never invented, and only for truncated ink."""
    path = os.path.join(tmp_path, "clip.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    for i in range(4):
        y = 120 + i * 40
        b = draw_hershey_text(shape, "KV01-X7-40", 100, y, 7.0)
        shape.draw_line((100, y + 2), (b[2], y + 2)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    y = 300
    b = draw_hershey_text(shape, "KV01-X7-4", 100, y, 7.0)
    cx = b[2]
    half = [(cx + 1.4, y - 7.0), (cx + 0.3, y - 5.6), (cx, y - 3.5), (cx + 0.3, y - 1.4), (cx + 1.4, y)]
    for k in range(len(half) - 1):
        shape.draw_line(half[k], half[k + 1]); shape.finish(width=0.5, color=(0, 0, 0), closePath=False)
    shape.draw_line((cx + 1.5, y - 9), (cx + 1.5, y + 2)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    shape.draw_line((100, y + 2), (cx + 1.5, y + 2)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    page.insert_text((100, 560), "SKALA 1:50", fontsize=10, fontname="helv")
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    at_clip = [d for d in pa.designations if abs(d.bbox[1] - 293) < 6]
    assert at_clip, "the clipped label must still be read"
    assert at_clip[0].text == "KV01-X7-40" and at_clip[0].dn == 40
    assert sum(1 for d in pa.designations if d.text == "KV01-X7-40") == 5


def test_an_unlabeled_branch_takes_the_only_junction_identity_only_with_evidence_at_its_end(tmp_path):
    """A branch with no size label of its own, off a junction where every labelled arm carries the same identity,
    has no competing candidate: a size change is always drawn with its own label. But a raw contact is not a
    connection: a dimension line, a wall edge or a fixture outline on the same pen reaches the run exactly as a
    branch does. So the branch takes the name only when its far end says it is a pipe - it ends in a component,
    at the sheet edge, against another pen's ink - and a branch that ends in empty air stays ambiguous with the
    junction's name as its one candidate: the metres are reported, as a question, not as an answer."""
    def build(right_label, fixture=False):
        path = os.path.join(tmp_path, f"br{right_label}{int(fixture)}.pdf")
        doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
        make_dashed_line(shape, (100, 300), (700, 300))          # main run through the tee at x = 400
        make_dashed_line(shape, (400, 300), (400, 480))          # branch with no label of its own
        if fixture:
            # a small drawn component on another pen where the branch ends: the evidence that it is a pipe
            shape.draw_rect(pymupdf.Rect(396, 480, 404, 488)); shape.finish(width=0.5, color=(0, 0, 1), closePath=True)
        _label(page, shape, 120, 200, "S3-R8-110", leader_to=(200, 300))
        _label(page, shape, 560, 200, right_label, leader_to=(640, 300))
        _scale(page)
        shape.commit(); doc.save(path); doc.close()
        return analyze_page(extract_document(path).pages[0])

    # ends in empty air. With the rule off: the main run is whole and confirmed, the branch is a question with one
    # candidate. With it on (VS-Kalkyl SPEC 6.4.3 h.3): the branch inherits system and material with an uncertain
    # size - its own pipe, to review - and the main run is untouched.
    from vvs_engine.rules import using
    with using({"pipes.ownership.PROPAGATE_TO_STOPS": False}):
        pa = build("S3-R8-110")
    reasons = {st.reason for sts in pa.ownership.prim_states.values() for st in sts.values()}
    assert "UNLABELLED_BRANCH_WITHOUT_END_EVIDENCE" in reasons and "unlabeled_branch_takes_the_only_junction_identity" not in reasons
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    assert abs(q[("S3-R8", 110)]["confirmed_horizontal_m"] - 600 / 56.69) < 0.4
    # a dashed branch: the ambiguous metres are the drawn ink (144 pt of dashes), gaps are only bridged into a
    # confirmed run
    assert abs(sum(r["ambiguous_m"] for r in pa.quantities) - 144 / 56.69) < 0.4
    assert len([p for p in pa.ownership.pipes if p.identity.dn == 110]) == 1, "the main run stays one pipe through the tee"
    assert any(f["reason"] == "AMBIGUOUS_JUNCTION" and abs(f["x"] - 400) < 4 for f in pa.frontiers)   # noden sitter vid dashens ände, tre punkter från T:t
    pa = build("S3-R8-110")
    assert sum(r["ambiguous_m"] for r in pa.quantities) == 0
    assert sum(r.get("review_m", 0.0) for r in pa.quantities) > 0, "the branch is measured, to review"
    assert len([p for p in pa.ownership.pipes if p.identity.dn == 110 and not p.needs_review]) == 1

    # ends in a component: the branch is a pipe, and it takes the only name the junction offers
    pa = build("S3-R8-110", fixture=True)
    reasons = {st.reason for sts in pa.ownership.prim_states.values() for st in sts.values()}
    assert "unlabeled_branch_takes_the_only_junction_identity" in reasons
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    assert abs(q[("S3-R8", 110)]["confirmed_horizontal_m"] - (600 + 180) / 56.69) < 0.4
    assert sum(r["ambiguous_m"] for r in pa.quantities) == 0

    # with a second size on the run the DN boundary is drawn at its tick, past the tee, so the tee still sees one
    # identity and the branch is still 110: what the branch may never do is take a size nobody drew on it
    pa = build("S3-R8-75", fixture=True)
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    assert abs(q[("S3-R8", 110)]["confirmed_horizontal_m"] - (600 - 60 + 180) / 56.69) < 0.25
    assert abs(q[("S3-R8", 75)]["confirmed_horizontal_m"] - 60 / 56.69) < 0.25


def test_dimension_on_the_row_below_is_a_vertical_pipe(tmp_path):
    """Two label forms mean two different things: the dimension inline names the horizontal run, the dimension on
    the row below names the vertical pipe at that point. A count prefix is the exception - it bundles parallel
    pipes along the run, and the reference takeoff gives such a label no vertical metres at all."""
    from vvs_engine.pipeline import _is_vertical_label

    class D:
        def __init__(self, src, dn, mult=1):
            self.dn_source, self.dn, self.multiplier = src, dn, mult

    assert _is_vertical_label(D("row", 75))
    assert not _is_vertical_label(D("inline", 75))
    assert not _is_vertical_label(D("row", 16, mult=2)), "2xKV1-X31 over 16 is a horizontal bundle"
    assert not _is_vertical_label(D("row", None))


def test_both_riser_sources_are_reported_side_by_side(tmp_path):
    """The drawn symbols and the row-below labels disagree, so the quantity carries both counts and the operator
    picks; neither is silently merged into the other."""
    path = os.path.join(tmp_path, "riser.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 300), (700, 300))
    _label(page, shape, 120, 200, "S3-R8-110", leader_to=(200, 300))
    _label(page, shape, 560, 200, "S3-R8", dn_row="75", leader_to=(640, 300))
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    assert all("riser_count" in r and "riser_count_from_labels" in r for r in pa.quantities)
    assert sum(r["riser_count_from_labels"] for r in pa.quantities) >= 1


def test_dimension_row_is_folded_into_the_name():
    """A dimension written on the row below belongs to the code above it: one identity, named with the size."""
    from vvs_engine.semantics.annotation import Designation

    def des(text, dn, src, row_text):
        return Designation(did="d", page=0, block_id="b", row_index=0, text=text, raw_text=text, pattern="",
                           tokens=[], system_token=text.split("-")[0], dn=dn, dn_source=src, dn_row_index=1,
                           dn_row_text=row_text, multiplier=1, bbox=(0, 0, 1, 1), angle=0.0, layer="", source="",
                           glyph_scores=[], unknown_chars=0)
    assert des("KV01-X7", 16, "row", "16").display_text == "KV01-X7-16"
    assert des("KV01-X7", 50, "row", "50/W").display_text == "KV01-X7-50/W"
    assert des("KV01-X7-16", 16, "inline", None).display_text == "KV01-X7-16"
    assert des("KV01-X7", None, None, None).display_text == "KV01-X7"


def test_one_pipe_written_two_ways_is_one_identity():
    """The same pipe labelled inline and with the dimension on the row below is one pipe.

    What the drawing writes after the dimension - a medium letter, an insulation code like F50 or W40 - belongs
    to the run rather than to its name, and a draughtsman writes it where there is room and leaves it off where
    there is not. So it is kept beside the name and not inside it: a label that says nothing about it agrees
    with one that names it, and the run is reported the fuller way.
    """
    from vvs_engine.pipes.ownership import complete_identities, identity_of
    from vvs_engine.semantics.attachment import PipeCodeAnchor

    def anc(designation, display, dn, aid="a"):
        return PipeCodeAnchor(anchor_id=aid, page=0, designation_id="d", designation=designation,
                              designation_display=display, system_token=designation.split("-")[0], dn=dn,
                              multiplier=1, block_id="b", leader_id="l", leader_paths=[], endpoint=(0.0, 0.0),
                              state="VERIFIED_PIPE_ATTACHMENT", reason="")
    inline = identity_of(anc("KV01-X7-50/W", "KV01-X7-50/W", 50), 2)
    row = identity_of(anc("KV01-X7", "KV01-X7-50/W", 50), None)
    assert inline.stem == row.stem == "KV01-X7"
    assert inline.compatible(row) and row.compatible(inline)
    assert inline.display == "KV01-X7-50/W"
    misread = identity_of(anc("KV01-X7", "KV01-X7-50ILI", 50), None)
    assert misread.key == "KV01-X7|DN50"

    # and on the sheet they become one entry, written the way the label that wrote it out wrote it
    done = complete_identities({"a": inline, "b": identity_of(anc("KV01-X7", "KV01-X7", 50, "b"), None)})
    assert done["a"].key == done["b"].key
    assert done["b"].display == "KV01-X7-50/W"


def test_two_media_on_one_dimension_are_two_pipes():
    """The rule that joins a short name to a full one may never join two full ones that disagree.

    A run insulated 50 mm and a run insulated 60 mm are the same system at the same dimension and two different
    things to order. The reading used to drop the code entirely and report them as one; it now keeps them apart,
    and leaves a label that states neither under its own short name rather than picking one of them.
    """
    from vvs_engine.pipes.ownership import complete_identities, identity_of
    from vvs_engine.semantics.attachment import PipeCodeAnchor

    def anc(text, dn, aid):
        return PipeCodeAnchor(anchor_id=aid, page=0, designation_id="d", designation=text,
                              designation_display=text, system_token=text.split("-")[0], dn=dn,
                              multiplier=1, block_id="b", leader_id="l", leader_paths=[], endpoint=(0.0, 0.0),
                              state="VERIFIED_PIPE_ATTACHMENT", reason="")
    f50 = identity_of(anc("VV01-X7-25-F50", 25, "a"), 2)
    f60 = identity_of(anc("VV01-X7-25-F60", 25, "b"), 2)
    assert not f50.compatible(f60)
    assert f50.key != f60.key

    bare = identity_of(anc("VV01-X7", 25, "c"), None)
    done = complete_identities({"a": f50, "b": f60, "c": bare})
    assert done["c"].qualifier is None, "with two answers on the sheet, silence picks neither"
    assert done["c"].key not in (done["a"].key, done["b"].key)


def test_a_word_that_starts_with_a_digit_is_not_a_code():
    """The leading letter separates a designation from a misread word or a date in the title block."""
    from vvs_engine.semantics.grammar import is_code_like
    assert is_code_like("KV01-X7-40") and is_code_like("2xKV01-X7")
    assert not is_code_like("53-R8-75") and not is_code_like("2024-O4-19") and not is_code_like("5PILLVAT")


def test_a_system_with_its_own_layer_is_not_an_abbreviation():
    """A layer token that a longer system name merely ends with is another system when the file names that
    system in full on a layer of its own."""
    from vvs_engine.semantics.attachment import system_layer_match
    # lagrets klass (52BB = tappkallvatten) namnger KV före det korta tecknet V1 i svansen - och en klass är
    # ingen förkortning, så den står kvar även när systemet är utskrivet på ett eget lager
    assert system_layer_match("KV1", "V-52BB-FE--V1-") == "52BB"
    assert system_layer_match("KV1", "V-52BB-FE--V1-", frozenset({"V1"})) == "52BB"
    assert system_layer_match("KV1", "V-52BB-FE--V1-", frozenset({"V1", "KV1"})) == "52BB"
    assert system_layer_match("KV1", "V-52B--FE--V1-") == "V1"                       # utan klass: svansen
    assert system_layer_match("KV1", "V-52B--FE--V1-", frozenset({"V1", "KV1"})) is None
    assert system_layer_match("FJV1", "V-52BB-FE--V1-", frozenset({"V1", "FJV1"})) is None
    assert system_layer_match("FJV1", "V-56B--FE--FJV1-", frozenset({"V1", "FJV1"})) == "FJV1"


def test_legend_is_read_from_its_shape_and_roles_from_how_the_drawing_uses_it():
    """A designation list is a stack of short codes each with a description; a code's role comes from the
    drawing, not from the words: opening a dimensioned label makes it a system, standing alone makes it a tag."""
    from vvs_engine.semantics.legend import read_legend, assign_roles, code_matches, is_code_token
    from vvs_engine.text.model import Glyph, TextRow

    def row(text, x, y, h=8.0):
        gl = [Glyph(gid=f"g{i}", char=c, bbox=(x + i * 5.0, y, x + i * 5.0 + 4.0, y + h), source="text")
              for i, c in enumerate(text)]
        return TextRow(rid=f"r{x}_{y}_{text[:4]}", page=0, glyphs=gl, text=text, angle=0.0, height=h,
                       bbox=(x, y, x + 5.0 * len(text), y + h), source="text", font="f", family="f")

    lines = [row("TAPPVATTENSYSTEM", 500, 10)]
    for i, (code, desc) in enumerate([("KV01", "Tappkallvatten"), ("VV01", "Tappvarmvatten"),
                                      ("S01", "Spillvatten"), ("X31", "PEX-ror"), ("G3", "MA-ror"),
                                      ("BLxxx", "Blandare"), ("TS1", "Tvattstall")]):
        lines.append(row(f"{code} {desc}", 500, 30 + 12 * i))
    class D:
        def __init__(self, text, head, dn, bbox):
            self.text, self.system_token, self.dn, self.bbox = text, head, dn, bbox
    out_on_the_drawing = (50.0, 400.0, 90.0, 408.0)
    drawn = [D("KV01-X31-16", "KV01", 16, out_on_the_drawing),
             D("S01-G3-110", "S01", 110, out_on_the_drawing),
             D("BL3", "BL3", None, out_on_the_drawing),
             D("TS1", "TS1", None, out_on_the_drawing)]
    # the list is read against what the drawing writes: a stack of short words with sentences beside them is a
    # note until its codes turn up out on the paper
    lg = read_legend(lines, drawn)
    assert {e.code for e in lg.entries} == {"KV01", "VV01", "S01", "X31", "G3", "BLxxx", "TS1"}
    assert all(e.heading == "TAPPVATTENSYSTEM" for e in lg.entries)

    assign_roles(lg, drawn)
    # VV01 hör hit fast bladet aldrig ritar det: raden säger "TAPPVARMVATTEN", och det avgör vad koden är.
    assert lg.systems() == {"KV01", "S01", "VV01"}
    assert lg.components() == {"BLXXX", "TS1"}          # BL3 matches the placeholder code BLxxx
    assert {e.code for e in lg.entries if e.role == "material"} == {"X31", "G3"}
    assert lg.names_a_pipe(D("S01-G3-110", "S01", 110, out_on_the_drawing))
    assert not lg.names_a_pipe(D("TS1", "TS1", None, out_on_the_drawing))
    assert code_matches("BL3", "BLxxx") and not code_matches("BL3", "TS1")
    assert is_code_token("KV01") and not is_code_token("TAPPVATTENSYSTEM")


def test_a_legend_placeholder_covers_the_suffix_an_office_writes():
    """`Bxxx GOLVBRUNN` is written B1 and B10 - and B12ML, B12KL, B221BL, because offices hang letters off the
    number. Reading the placeholder as digits alone left the drawing naming a floor drain and the reading having
    it down as something that might be a pipe."""
    from vvs_engine.semantics.legend import code_matches

    for tag in ("B1", "B10", "B12ML", "B12KL", "B21M", "B221BL", "B241BL"):
        assert code_matches(tag, "BXXX"), tag


def test_a_placeholder_still_cannot_reach_a_designation():
    """The widening is a suffix on the varying part, not a licence: everything outside the placeholder run must
    still agree character for character, or a floor drain's code starts swallowing pipes."""
    from vvs_engine.semantics.legend import code_matches

    for not_a_tag in ("S3-R8-110", "KV1-X31-16", "AB12", "B", "BX12", "B12MLXX", "12B"):
        assert not code_matches(not_a_tag, "BXXX"), not_a_tag
    # and a placeholder is not a wildcard over the letters that identify the family
    assert not code_matches("TS1", "BXXX")
    assert code_matches("TS12A", "TSXXX") and not code_matches("TS12A", "BLXXX")


def test_a_label_written_on_a_line_that_runs_to_the_pipe_speaks_for_its_own_row(tmp_path):
    """Two labels stacked close enough to read as one block, each written on a line that carries on to its own
    pipe. The line names the row it runs from, so each pipe gets the label written above it, not both."""
    path = os.path.join(tmp_path, "baseline.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (300, 300), (700, 300))            # upper run
    make_dashed_line(shape, (300, 380), (700, 380))            # lower run
    for y, text, to in ((200, "S3-R8-110", (400, 300)), (216, "S3-R8-75", (400, 380))):
        page.insert_text((100, y), text, fontsize=10, fontname="helv")
        base = y + 2
        shape.draw_line((100, base), (180, base)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        shape.draw_line((180, base), to); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        shape.draw_line((to[0] - 1, to[1] - 1), (to[0] + 1, to[1] + 1))
        shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    per_leader = {}
    for a in pa.anchors:
        per_leader.setdefault(a.leader_id, set()).add(a.designation)
    reaching = [ds for lid, ds in per_leader.items()
                if any(a.state == "VERIFIED_PIPE_ATTACHMENT" for a in pa.anchors if a.leader_id == lid)]
    assert reaching, "no leader reached a pipe"
    assert all(len(ds) == 1 for ds in reaching), f"a leader spoke for several labels: {reaching}"
    assert {next(iter(ds)) for ds in reaching} == {"S3-R8-110", "S3-R8-75"}


def test_a_sheet_exported_without_layers_still_finds_its_pipes(tmp_path):
    """No layer names, and the leaders drawn with a pen of their own. The pen carrying the leaders goes; the pen
    carrying the pipes stays, and a leader that strays onto the pipes does not take them with it."""
    path = os.path.join(tmp_path, "flat.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (300, 300), (700, 300), width=1.44)
    make_dashed_line(shape, (400, 300), (400, 450), width=1.44)
    for i, y in enumerate((120, 150, 180, 210)):
        page.insert_text((80, y), "S3-R8-110", fontsize=10, fontname="helv")
        shape.draw_line((80, y + 2), (150, y + 2)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        tx = 320 + 40 * i
        shape.draw_line((150, y + 2), (tx, 300)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        shape.draw_line((tx - 1, 299), (tx + 1, 301)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    assert all(f.split("|s|")[0] == "" for f in pa.pipe_families), pa.pipe_families
    assert any("w1.44" in f for f in pa.pipe_families), f"the pipes' own pen was dropped: {pa.pipe_families}"
    assert not any("w0.72" in f for f in pa.pipe_families), f"the leaders' pen was measured: {pa.pipe_families}"


def test_the_faintest_pen_on_a_layerless_sheet_is_not_read_as_pipe(tmp_path):
    """With no layer names, a pen thinner than half the ink on the sheet draws its background. Leaders ending
    there do not make it pipe - otherwise a plan's construction lines are measured as tap water."""
    path = os.path.join(tmp_path, "faint.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    for y in range(260, 460, 8):                       # background ruled with the faintest pen
        shape.draw_line((200, y), (760, y)); shape.finish(width=0.12, color=(0, 0, 0), closePath=False)
    for i, y in enumerate((120, 150, 180, 210)):
        page.insert_text((60, y), "S3-R8-110", fontsize=10, fontname="helv")
        shape.draw_line((60, y + 2), (130, y + 2)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        tx = 300 + 60 * i
        shape.draw_line((130, y + 2), (tx, 300)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
        shape.draw_line((tx - 1, 299), (tx + 1, 301)); shape.finish(width=0.72, color=(0, 0, 0), closePath=False)
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    assert not any("w0.12" in f for f in pa.pipe_families), f"the sheet's faintest pen was read as pipe: {pa.pipe_families}"


def test_a_second_route_may_add_a_run_but_never_rename_one(tmp_path):
    """The routes are put side by side. One may name a run the others missed; where two name it differently the
    run leaves the quantity. What no route reached is listed with its reason rather than passed over."""
    from vvs_engine.routes import ROUTES
    path = os.path.join(tmp_path, "routes.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (200, 300), (700, 300))
    make_dashed_line(shape, (200, 420), (700, 420))           # a second run no label points at
    _label(page, shape, 120, 200, "S3-R8-110", leader_to=(400, 300))
    _label(page, shape, 120, 230, "S3-R8-110", leader_to=(500, 300))
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    cross, rev = pa.crosscheck, pa.review_findings
    assert set(cross["routes"]) == set(ROUTES)
    assert cross["in_conflict_m"] == 0.0
    assert cross["applied"]["made_ambiguous_m"] == 0.0
    # the unlabelled run is reported, not silently dropped
    assert rev["n_unnamed_runs"] >= 1 and rev["unnamed_m"] > 0
    assert rev["named_m"] > 0 and 0 <= rev["coverage_pct"] <= 100


def test_geometry_the_sheets_own_labels_cannot_reach_is_not_read_as_pipe(tmp_path):
    """With no layer name to vouch for a family, the sheet's own pipe labels have to reach it. A page whose
    labels almost all fail to land on what was accepted accepted the wrong geometry - had it been the pipes,
    the labels would have found it."""
    path = os.path.join(tmp_path, "reach.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    # a mesh of rooms, and two leaders that happen to end on it
    for x in range(200, 700, 100):
        shape.draw_line((x, 240), (x, 470)); shape.finish(width=1.44, color=(0, 0, 0), closePath=False)
    for y in range(240, 500, 60):
        shape.draw_line((200, y), (660, y)); shape.finish(width=1.44, color=(0, 0, 0), closePath=False)
    for i in range(2):
        _label(page, shape, 60, 120 + 22 * i, "S3-R8-110", leader_to=(300 + 100 * i, 240))
    # and many more pipe labels that reach nothing at all
    for i in range(24):
        page.insert_text((60 + 90 * (i % 8), 520 + 14 * (i // 8)), f"S3-R8-{[75, 110, 160][i % 3]}", fontsize=9, fontname="helv")
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    assert pa.pipe_families == {}, f"a mesh its own labels cannot reach was read as pipe: {pa.pipe_families}"
    assert not pa.quantities


def test_the_two_sides_of_a_thin_drawn_object_are_not_two_pipes(tmp_path):
    """A radiator, a bench, a duct seen edge on: drawn as two long sides a hair apart, on the same pen as the
    pipes. A label whose leader lands on one of them must not turn the object's outline into measured pipe -
    the drawing gives no room to read the two sides apart, so their length is nobody's."""
    path = os.path.join(tmp_path, "sliver.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 300), (400, 300))                  # a real run
    for dy in (0, 1):                                # the object: two sides 1 pt apart, on the pipes' own pen
        shape.draw_line((450, 200 + dy), (700, 200 + dy)); shape.finish(width=1.44, color=(0, 0, 0), closePath=False)
    _label(page, shape, 120, 200, "S3-R8-110", leader_to=(200, 300))
    _label(page, shape, 300, 150, "S3-R8-110", leader_to=(460, 200))  # this one lands on the object
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    q = {(r["base"], r["dn"]): r for r in pa.quantities}
    assert ("S3-R8", 110) in q
    # the 300 pt run is measured; the 2 x 250 pt of outline is not
    assert abs(q[("S3-R8", 110)]["confirmed_horizontal_m"] - 300 / 56.69) < 0.35
    reasons = {s.reason for st in pa.ownership.prim_states.values() for s in st.values()}
    assert "AMBIGUOUS_SLIVER_PAIR_READS_AS_A_DRAWN_OUTLINE" in reasons


def test_parallel_pipes_a_readable_gap_apart_are_still_two_pipes(tmp_path):
    """The other side of the same rule: a tap-water bundle runs parallel for its whole length, and those are
    real pipes. Only a gap too small to read apart marks an outline."""
    path = os.path.join(tmp_path, "bundle.pdf")
    doc = pymupdf.open(); page = doc.new_page(width=842, height=595); shape = page.new_shape()
    make_dashed_line(shape, (100, 300), (500, 300))
    make_dashed_line(shape, (100, 306), (500, 306))                  # 6 pt apart: a reader tells them apart
    _label(page, shape, 120, 200, "KV1-X31-16", leader_to=(250, 300))
    _label(page, shape, 120, 400, "VV1-X31-16", leader_to=(250, 306))
    _scale(page)
    shape.commit(); doc.save(path); doc.close()
    pa = analyze_page(extract_document(path).pages[0])
    q = {r["designation"]: r for r in pa.quantities}
    assert "KV1-X31-16" in q and "VV1-X31-16" in q
    for name in ("KV1-X31-16", "VV1-X31-16"):
        assert abs(q[name]["confirmed_horizontal_m"] - 400 / 56.69) < 0.5, (name, q[name])


def test_a_legend_section_settles_a_code_this_page_did_not_use():
    """The same project must not read differently from one sheet to the next.

    A code's role comes from how the drawing uses it, and a sheet only shows what a sheet shows: a page with no
    dimensioned VV label loses VV as a system, and then every VV label on it is refused as not naming a pipe at
    all. Silent, and invisible in the counts, because a refused label never becomes a pipe label to be missing.

    The legend's own grouping settles it: a heading is the sheet saying "these belong together".
    """
    from vvs_engine.semantics.legend import DrawingLegend, LegendEntry, assign_roles

    def entry(code, heading):
        return LegendEntry(code=code, description="d", heading=heading, bbox=(500.0, 10.0, 560.0, 18.0))

    class D:
        def __init__(self, text, head, dn):
            self.text, self.system_token, self.dn = text, head, dn
            self.bbox = (50.0, 400.0, 90.0, 408.0)

    lg = DrawingLegend(entries=[entry("KV1", "SYSTEM TAPPVATTEN"), entry("VV1", "SYSTEM TAPPVATTEN"),
                                entry("VVC1", "SYSTEM TAPPVATTEN"), entry("KB1", "SYSTEM TAPPVATTEN"),
                                entry("S1", "SYSTEM SPILLVATTEN"), entry("S3", "SYSTEM SPILLVATTEN"),
                                entry("X31", "MATERIAL ROR"), entry("P2", "MATERIAL ROR")])
    # the page happens to carry dimensioned labels for two of the tappvatten codes and one spillvatten code
    assign_roles(lg, [D("KV1-X31-16", "KV1", 16), D("VV1-X31-16", "VV1", 16), D("S3-P2-110", "S3", 110)])
    assert lg.systems() >= {"KV1", "VV1", "S3"}, "vad sidan själv visar står kvar"
    assert {"VVC1", "KB1"} <= lg.systems(), "två använda system i sektionen talar för resten av den"
    assert "S1" in lg.systems(), "en sektion om två koder där den ena används är sektionen som talar"
    assert "X31" not in lg.systems() and "P2" not in lg.systems(), "materialrubriken har inga belägg alls"
    assert {e.role_from for e in lg.entries if e.code == "VVC1"} == {"heading"}
    assert {e.role_from for e in lg.entries if e.code == "KV1"} == {"usage"}


def test_one_used_code_in_a_long_section_does_not_speak_for_it():
    """Half of a section of two is evidence. One of ten is a coincidence, and promoting the other nine on it
    would hand the drawing nine systems it never showed."""
    from vvs_engine.semantics.legend import DrawingLegend, LegendEntry, assign_roles

    def entry(code, heading):
        return LegendEntry(code=code, description="d", heading=heading, bbox=(500.0, 10.0, 560.0, 18.0))

    class D:
        def __init__(self, text, head, dn):
            self.text, self.system_token, self.dn = text, head, dn
            self.bbox = (50.0, 400.0, 90.0, 408.0)

    lg = DrawingLegend(entries=[entry(f"K{i}", "LANG SEKTION") for i in range(1, 11)]
                       + [entry("Z9", "ANNAN SEKTION")])
    assign_roles(lg, [D("K1-X31-16", "K1", 16)])
    assert lg.systems() == {"K1"}


def test_a_section_that_holds_both_kinds_settles_nothing():
    """A heading only speaks where it speaks with one voice; a mixed section is not the drawing grouping."""
    from vvs_engine.semantics.legend import DrawingLegend, LegendEntry, assign_roles

    def entry(code, heading):
        return LegendEntry(code=code, description="d", heading=heading, bbox=(500.0, 10.0, 560.0, 18.0))

    class D:
        def __init__(self, text, head, dn):
            self.text, self.system_token, self.dn = text, head, dn
            self.bbox = (50.0, 400.0, 90.0, 408.0)

    lg = DrawingLegend(entries=[entry("KV1", "ALLT"), entry("S1", "ALLT"), entry("BLXXX", "ALLT"),
                                entry("TS1", "ALLT"), entry("X31", "ALLT"), entry("Q9", "ANNAT")])
    assign_roles(lg, [D("KV1-X31-16", "KV1", 16), D("S1-X31-110", "S1", 110),
                      D("BL3", "BL3", None), D("TS1", "TS1", None)])
    assert lg.systems() == {"KV1", "S1"}
    assert lg.components() == {"BLXXX", "TS1"}
    assert "X31" not in lg.systems() and "X31" not in lg.components()
