"""Overlay PDFs drawn on top of the original drawing (actual geometry only; no synthetic rays)."""
from __future__ import annotations

import os

import pymupdf

COLORS = {
    "confirmed": (0.0, 0.55, 0.0), "ambiguous": (1.0, 0.55, 0.0), "unowned": (0.6, 0.6, 0.6), "designation": (0.0, 0.3, 1.0),
    "leader": (0.8, 0.0, 0.8), "endpoint": (1.0, 0.0, 0.0), "no_attach": (1.0, 0.0, 0.0), "unsupported": (0.5, 0.0, 0.0),
    "node": (0.0, 0.6, 0.6), "outside_contract": (0.35, 0.2, 0.75),
}


def _rot_shape(page):
    """Shapes are drawn in page (rotated display) coordinates; PyMuPDF's Shape expects unrotated coordinates."""
    return page.derotation_matrix if page.rotation else None


def _pt(page, x, y):
    M = _rot_shape(page)
    if M is None:
        return pymupdf.Point(x, y)
    return pymupdf.Point(x, y) * M


def _rect(page, b):
    p0 = _pt(page, b[0], b[1]); p1 = _pt(page, b[2], b[3])
    return pymupdf.Rect(min(p0.x, p1.x), min(p0.y, p1.y), max(p0.x, p1.x), max(p0.y, p1.y))


SPECS = {
    "production-overlay.pdf": lambda: _draw_production,
    "designation-overlay.pdf": lambda: _draw_designations,
    "leader-overlay.pdf": lambda: _draw_leaders,
    "endpoint-pipe-attachment-overlay.pdf": lambda: _draw_attachments,
    "topology-overlay.pdf": lambda: _draw_topology,
    "ambiguous-overlay.pdf": lambda: _draw_ambiguous,
    "unsupported-style-overlay.pdf": lambda: _draw_unsupported,
    "frontier-overlay.pdf": lambda: _draw_frontiers,
}


class OverlayWriter:
    """The marked-up copies of the drawing, drawn a sheet at a time.

    They used to be drawn a copy at a time: open the whole PDF, walk every page's reading, save, and again for
    the next of the seven. That needs every sheet's reading to still be in hand at the end, which is what makes a
    fifty-sheet set hold fifty readings' worth of geometry - and it opens the file seven times over. Drawn this
    way each sheet's reading is used the moment it exists and can be let go of straight after."""

    def __init__(self, pdf_path: str, out_dir: str):
        self.out_dir = out_dir
        self.pdf_path = pdf_path
        self.docs = {name: pymupdf.open(pdf_path) for name in SPECS}

    def add(self, pa) -> None:
        for name, fn in SPECS.items():
            page = self.docs[name][pa.page.info.index]
            shape = page.new_shape()
            fn()(page, shape, pa)
            shape.commit()

    def replace(self, pa) -> None:
        """Draw this sheet again from scratch: a page read a second time must not carry the first reading's ink.

        The overlays are the source PDF with marks drawn on top, so the page is reloaded from the file - which is
        the only way to take marks off it - and the new reading drawn onto that."""
        idx = pa.page.info.index
        for name, doc in self.docs.items():
            fresh = pymupdf.open(self.pdf_path)
            doc.delete_page(idx)
            doc.insert_pdf(fresh, from_page=idx, to_page=idx, start_at=idx)
            fresh.close()
        self.add(pa)

    def close(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, doc in self.docs.items():
            path = os.path.join(self.out_dir, name)
            doc.save(path, garbage=3, deflate=True)
            doc.close()
            out[name] = path
        return out


def write_overlays(pdf_path: str, analyses: list, out_dir: str) -> dict[str, str]:
    """analyses: list of PageAnalysis (one per analyzed page). Returns {name: path}."""
    w = OverlayWriter(pdf_path, out_dir)
    for pa in analyses:
        w.add(pa)
    return w.close()


def _draw_polylines(page, shape, polylines, color, width):
    for pts in polylines:
        if len(pts) < 2:
            continue
        shape.draw_polyline([_pt(page, x, y) for x, y in pts])
        shape.finish(color=color, width=width, lineCap=1, lineJoin=1, closePath=False)


def _pipe_polylines(pa, state):
    out = []
    own = pa.ownership
    for fk, g in pa.graphs.items():
        for pid, st in own.prim_states[fk].items():
            if st.state == state:
                s = g.prims[pid].seg
                out.append([(s.x0, s.y0), (s.x1, s.y1)])
    return out


# one colour per identity; none of them may be dark enough to read as the drawing's own black line work
PALETTE = [(0.05, 0.6, 0.1), (0.0, 0.35, 0.9), (0.85, 0.1, 0.1), (0.55, 0.0, 0.7), (0.0, 0.6, 0.6), (0.8, 0.45, 0.0),
           (0.3, 0.3, 0.9), (0.6, 0.35, 0.1), (0.9, 0.0, 0.5), (0.1, 0.45, 0.3), (0.5, 0.5, 0.0), (0.45, 0.75, 0.0)]


def sheet_colors(keys) -> dict:
    """One colour per designation on the sheet, none shared: the designations sorted, each given the next hue a
    golden angle on, in three lightness steps. The viewer (palette.ts sheetColors) does the same over the same
    keys, so a pipe has one colour on the screen and in the marked PDF. A colour drawn from a hash over a small
    palette gave two designations the same orange on one sheet (W-50-1-A-0122: S2-P5-75, VS1-S13-12/W and
    VVC1-X7-32/W), and the sheet could not be checked by eye."""
    import colorsys
    out = {}
    for i, k in enumerate(sorted(set(keys))):
        hue = (i * 0.381966) % 1.0
        light = (0.42, 0.32, 0.52)[i % 3]
        out[k] = colorsys.hls_to_rgb(hue, light, 0.9)
    return out


def identity_color(key: str, colors: dict | None = None):
    """The sheet's colour for a designation+DN, or a deterministic one when the sheet's list is not at hand."""
    if colors and key in colors:
        return colors[key]
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return PALETTE[h % len(PALETTE)]


def _draw_production(page, shape, pa):
    from ..profile.hatch import inside_hatch
    colors = sheet_colors(m.pipe.identity.key for m in pa.measures)
    for m in pa.measures:
        col = identity_color(m.pipe.identity.key, colors)
        g = pa.graphs[m.pipe.family]
        inside, outside = [], []
        for pid in m.pipe.prim_ids:
            s = g.prims[pid].seg
            (inside if pa.hatch_families and inside_hatch(pa.hatch_families, *s.mid) else outside).append([(s.x0, s.y0), (s.x1, s.y1)])
        _draw_polylines(page, shape, outside, col, 2.6)
        for pts in inside:
            shape.draw_polyline([_pt(page, x, y) for x, y in pts])
            shape.finish(color=COLORS["unowned"], width=2.6, lineCap=1, dashes="[3 2] 0", closePath=False)
    _draw_polylines(page, shape, _pipe_polylines(pa, "AMBIGUOUS"), COLORS["ambiguous"], 2.0)
    # utanför entreprenadgräns: egen färg, streckad; gränspunkten som ett kryss
    for pts in _pipe_polylines(pa, "OUTSIDE_CONTRACT"):
        shape.draw_polyline([_pt(page, x, y) for x, y in pts])
        shape.finish(color=COLORS["outside_contract"], width=2.6, lineCap=1, dashes="[1 1.5] 0", closePath=False)
    for r in (getattr(pa.ownership, "outside_contract", None) or []):
        if r.get("cut_segment"):
            shape.draw_polyline([_pt(page, x, y) for x, y in r["cut_segment"]])
            shape.finish(color=COLORS["outside_contract"], width=2.6, lineCap=1, dashes="[1 1.5] 0", closePath=False)
        x, y = r["point"]
        shape.draw_line(_pt(page, x - 3, y - 3), _pt(page, x + 3, y + 3))
        shape.draw_line(_pt(page, x - 3, y + 3), _pt(page, x + 3, y - 3))
        shape.finish(color=COLORS["outside_contract"], width=0.9)
    # legend
    y = 14.0
    seen = {}
    for m in pa.measures:
        seen.setdefault(m.pipe.identity.key, m.pipe.identity.display + (f" DN{m.pipe.identity.dn}" if m.pipe.identity.dn is not None else ""))
    for key, label in sorted(seen.items()):
        shape.draw_line(_pt(page, 8, y), _pt(page, 28, y)); shape.finish(color=identity_color(key, colors), width=2.4)
        shape.insert_text(_pt(page, 31, y + 1.5), label, fontsize=4.5, color=(0, 0, 0))
        y += 7
    shape.draw_line(_pt(page, 8, y), _pt(page, 28, y)); shape.finish(color=COLORS["unowned"], width=2.4, dashes="[3 2] 0")
    shape.insert_text(_pt(page, 31, y + 1.5), "i skrafferat omrade (vagg), ej i mangd", fontsize=4.5, color=(0, 0, 0))
    if getattr(pa.ownership, "outside_contract", None):
        y += 7
        shape.draw_line(_pt(page, 8, y), _pt(page, 28, y))
        shape.finish(color=COLORS["outside_contract"], width=2.4, dashes="[1 1.5] 0")
        shape.insert_text(_pt(page, 31, y + 1.5), "utanfor entreprenadgrans, ej i mangd", fontsize=4.5, color=(0, 0, 0))
    for d in pa.designations:
        if any(a.designation_id == d.did and a.state == "VERIFIED_PIPE_ATTACHMENT" for a in pa.anchors):
            shape.draw_rect(_rect(page, d.bbox)); shape.finish(color=COLORS["designation"], width=0.5)
    for a in pa.anchors:
        if a.state == "VERIFIED_PIPE_ATTACHMENT":
            shape.draw_circle(_pt(page, a.endpoint[0], a.endpoint[1]), 1.6); shape.finish(color=COLORS["endpoint"], width=0.6)
    if getattr(pa, "register", None):
        _draw_register(page, shape, pa.register, y + 7)


# Läget "Alla": varje beteckning bladet skriver får en ruta, i en färg för sitt slag. Heldragen är säker, streckad
# ska granskas: en kod bladet själv inte förklarar, eller en ledning med meter att granska.
KIND_COLORS = {"ledning": (0.0, 0.3, 1.0), "komponent": (0.0, 0.5, 0.25), "klass": (0.55, 0.3, 0.0),
               "rum": (0.0, 0.55, 0.6), "okänd": (0.85, 0.35, 0.0)}
KIND_TEXT = {"ledning": "ledning (m)", "komponent": "komponent (st)", "klass": "klass (st)", "rum": "rum (m2)",
             "okänd": "okand kod (st)"}


def _draw_register(page, shape, register, y):
    for e in register.get("entries") or []:
        col = KIND_COLORS.get(e["kind"], COLORS["designation"])
        # another discipline's run, measured in the widened reading: drawn where it was measured, dashed - it is
        # to be reviewed - and in the colour of a run
        for run in e.get("runs") or []:
            if len(run) >= 2:
                shape.draw_polyline([_pt(page, x, y_) for x, y_ in run])
                shape.finish(color=col, width=2.0, lineCap=1, dashes="[4 2] 0", closePath=False)
        for p in e.get("places") or []:
            shape.draw_rect(_rect(page, p["bbox"]))
            if e.get("state") == "säker":
                shape.finish(color=col, width=0.7)
            else:
                shape.finish(color=col, width=0.7, dashes="[2 1] 0")
    for kind in ("ledning", "komponent", "klass", "rum", "okänd"):
        shape.draw_rect(_rect(page, (8, y - 2, 28, y + 2)))
        shape.finish(color=KIND_COLORS[kind], width=0.7)
        shape.insert_text(_pt(page, 31, y + 1.5), KIND_TEXT[kind], fontsize=4.5, color=(0, 0, 0))
        y += 7
    shape.insert_text(_pt(page, 8, y + 1.5), "streckad ruta: granskas", fontsize=4.5, color=(0, 0, 0))


def _draw_designations(page, shape, pa):
    for d in pa.designations:
        shape.draw_rect(_rect(page, d.bbox)); shape.finish(color=COLORS["designation"], width=0.6)
        shape.insert_text(_pt(page, d.bbox[0], d.bbox[1] - 1), f"{d.text} DN{d.dn if d.dn is not None else '?'}", fontsize=3, color=COLORS["designation"])


def _draw_leaders(page, shape, pa):
    for ld in pa.leaders:
        _draw_polylines(page, shape, [ld.points], COLORS["leader"], 1.0)
        shape.draw_circle(_pt(page, ld.end[0], ld.end[1]), 1.2); shape.finish(color=COLORS["endpoint"], width=0.5)
        for m in ld.crossing_marks:
            shape.draw_rect(_rect(page, m.bbox)); shape.finish(color=COLORS["endpoint"], width=0.4)


def _draw_attachments(page, shape, pa):
    for a in pa.anchors:
        col = COLORS["confirmed"] if a.state == "VERIFIED_PIPE_ATTACHMENT" else (COLORS["ambiguous"] if a.state.startswith("AMBIGUOUS") else COLORS["no_attach"])
        shape.draw_circle(_pt(page, a.endpoint[0], a.endpoint[1]), 2.2); shape.finish(color=col, width=0.8)
        for c in a.contacts:
            p = pa.page.paths[0] if False else None
            shape.draw_circle(_pt(page, c.point[0], c.point[1]), 1.0); shape.finish(color=col, width=0.5)
        shape.insert_text(_pt(page, a.endpoint[0] + 2.5, a.endpoint[1] - 1), a.designation, fontsize=2.6, color=col)


def _draw_topology(page, shape, pa):
    for fk, g in pa.graphs.items():
        for n in g.nodes.values():
            if n.degree >= 3:
                shape.draw_circle(_pt(page, n.x, n.y), 1.5); shape.finish(color=COLORS["node"], width=0.6)
            elif n.degree == 1:
                shape.draw_rect(_rect(page, (n.x - 0.8, n.y - 0.8, n.x + 0.8, n.y + 0.8))); shape.finish(color=COLORS["node"], width=0.4)
        for b in g.bridges:
            n = g.nodes.get(b["from_node"])
            if n:
                shape.draw_circle(_pt(page, n.x, n.y), 0.9); shape.finish(color=COLORS["leader"], width=0.4)


def _draw_ambiguous(page, shape, pa):
    _draw_polylines(page, shape, _pipe_polylines(pa, "AMBIGUOUS"), COLORS["ambiguous"], 2.4)
    _draw_polylines(page, shape, _pipe_polylines(pa, "UNOWNED"), COLORS["unowned"], 1.4)
    for a in pa.anchors:
        if a.state != "VERIFIED_PIPE_ATTACHMENT":
            shape.draw_circle(_pt(page, a.endpoint[0], a.endpoint[1]), 2.4); shape.finish(color=COLORS["ambiguous"], width=0.8)


def _draw_unsupported(page, shape, pa):
    for d in pa.designations:
        if d.unknown_chars > 0:
            shape.draw_rect(_rect(page, d.bbox)); shape.finish(color=COLORS["unsupported"], width=0.8)
    for a in pa.anchors:
        if a.state == "NO_PIPE_ATTACHMENT":
            shape.draw_circle(_pt(page, a.endpoint[0], a.endpoint[1]), 2.0); shape.finish(color=COLORS["unsupported"], width=0.6)


# var rören slutar: gröna riktiga gränser, röda där meter sannolikt tappas, orange där något lämnats öppet
FRONTIER_COLORS = {"REAL": (0.0, 0.55, 0.0), "LOSSY": (0.85, 0.1, 0.1), "OPEN": (1.0, 0.55, 0.0)}
FRONTIER_SHORT = {"REAL_DN_BOUNDARY": "DN", "REAL_SYSTEM_BOUNDARY": "SYS", "REAL_DESIGNATION_BOUNDARY": "NAMN",
                  "DECLARED_BOUNDARY": "DEKL", "AMBIGUOUS_JUNCTION": "TVET", "FLOW_BUDGET": "FLOD",
                  "UNOWNED_CONTINUATION": "OAGD", "REPRESENTATION_CHANGE": "PENNA", "BROKEN_CONTINUITY": "GAP",
                  "VERTICAL": "STIG", "SYMBOL": "SYMB", "SHEET_EDGE": "KANT", "FREE_END": "SLUT",
                  "CLOSED_LOOP": "LOOP", "UNSUPPORTED_STRUCTURE": "?"}


def _draw_frontiers(page, shape, pa):
    from ..pipes.frontier import REAL, LOSSY
    _draw_polylines(page, shape, [pl for m in pa.measures for pl in m.pipe.points], (0.75, 0.75, 0.75), 1.0)
    for f in pa.frontiers:
        cls = "REAL" if f["reason"] in REAL else "LOSSY" if f["reason"] in LOSSY else "OPEN"
        col = FRONTIER_COLORS[cls]
        c = _pt(page, f["x"], f["y"])
        shape.draw_circle(c, 2.6); shape.finish(color=col, width=0.9)
        try:
            shape.insert_text(pymupdf.Point(c.x + 3.2, c.y - 1.5), FRONTIER_SHORT.get(f["reason"], "?"),
                              fontsize=4.0, color=col)
        except Exception:
            pass
