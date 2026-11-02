"""Vector glyph text: components -> size families -> rows -> glyph families -> characters -> TextRows."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..geometry.core import stable_id
from ..pdf.extract import RawPage
from .model import Glyph, TextRow, make_row, row_axes, project
from .recognize import (UNKNOWN_THRESHOLD, FamilyResult, classify, count_holes, decide, family_fingerprint,
                        rasterize_polygon_fill, rasterize_segments_oriented, skeleton_orientation)
from .postprocess import resolve_twins
from .strokes import GlyphCandidate, RowCluster, StrokeComponent, build_components, cluster_rows, size_families


MARK_MAX_PT = 12.0    # ett märke är litet; ett streck som är längre än så är en linje, och linjer hör till geometrin


@dataclass
class Mark:
    """An isolated straight stroke (1-2 segments) on a glyph-carrying layer: a tick/marker candidate, not text."""
    mid: str
    layer: str
    style: str
    bbox: tuple[float, float, float, float]
    segs: list
    path_ids: list[str]


@dataclass
class VectorTextResult:
    rows: list[TextRow]
    families: dict[str, FamilyResult]
    size_families: list[float]
    n_components: int
    n_glyphs: int
    marks: list[Mark] = field(default_factory=list)
    rejected_rows: list[TextRow] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _structural_char(g: GlyphCandidate, rc: RowCluster) -> str | None:
    """Generic typographic structure that pixel matching handles poorly at glyph scale:
    ':' = two tiny disconnected marks stacked across the reading axis; '(' / ')' = a single thin stroke whose
    bulge (sagitta) is significant, bulging left or right of its chord."""
    H = max(rc.height, 1e-6)
    a = math.radians(rc.angle)
    d = (math.cos(a), math.sin(a)); n = (-d[1], d[0])
    if len(g.comps) == 2 and all(max(c.w, c.h) < 0.2 * H for c in g.comps):
        c0, c1 = g.comps
        along = abs((c0.cx - c1.cx) * d[0] + (c0.cy - c1.cy) * d[1])
        across = abs((c0.cx - c1.cx) * n[0] + (c0.cy - c1.cy) * n[1])
        if across > 0.25 * H and along < 0.15 * H:
            return ":"
    if len(g.comps) == 1 and g.kind == "f" and len(g.segs) >= 12:
        # Outlined CAD parentheses are closed polygons, often flattened into
        # straight segments. Their centreline bows; an I or a narrow zero does
        # not. Inspect sections across the ink rather than the closed chord.
        projected = [(s.x0*d[0]+s.y0*d[1], s.x0*n[0]+s.y0*n[1],
                      s.x1*d[0]+s.y1*d[1], s.x1*n[0]+s.y1*n[1]) for s in g.segs]
        xs = [x for s in projected for x in (s[0], s[2])]
        ys = [y for s in projected for y in (s[1], s[3])]
        lo, hi = min(ys), max(ys)
        if 0.75*H <= hi-lo <= 1.5*H and max(xs)-min(xs) <= 0.3*H:
            sections = []
            for fraction in (0.1, 0.25, 0.5, 0.75, 0.9):
                y = lo + fraction*(hi-lo)
                cuts = sorted(x0+(y-y0)*(x1-x0)/(y1-y0)
                              for x0,y0,x1,y1 in projected if min(y0,y1) <= y < max(y0,y1))
                if len(cuts) != 2 or not 0 < cuts[-1]-cuts[0] <= 0.12*H:
                    break
                sections.append((cuts[0]+cuts[-1])/2)
            if len(sections) == 5:
                left, quarter, centre, three_quarters, right = sections
                bow = centre-(left+right)/2
                if (abs(bow) >= 0.045*H and abs(left-right) <= 0.04*H
                        and abs(quarter-three_quarters) <= 0.04*H
                        and 0 < (quarter-left)*bow and 0 < (centre-quarter)*bow):
                    return "(" if bow < 0 else ")"
    if len(g.comps) == 1 and g.comps[0].kind == "s" and any(p.n_curves > 0 for p in g.comps[0].paths):
        segs = g.segs
        pts = [(s.x0, s.y0) for s in segs] + [(segs[-1].x1, segs[-1].y1)]
        # chord = farthest pair of endpoints of the stroke chain
        p0, p1 = pts[0], pts[-1]
        chord = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        width_along = max(x * d[0] + y * d[1] for x, y in pts) - min(x * d[0] + y * d[1] for x, y in pts)
        height_across = max(x * n[0] + y * n[1] for x, y in pts) - min(x * n[0] + y * n[1] for x, y in pts)
        if chord > 0.6 * H and height_across >= 0.6 * H and width_along <= 0.45 * H:
            # sagitta: max signed distance of points from the chord
            ux, uy = (p1[0] - p0[0]) / chord, (p1[1] - p0[1]) / chord
            sags = [(-(x - p0[0]) * uy + (y - p0[1]) * ux) for x, y in pts]
            smax, smin = max(sags), min(sags)
            sag = smax if abs(smax) > abs(smin) else smin
            one_sided = abs(smax) < 0.03 * chord or abs(smin) < 0.03 * chord
            # A shallow arc is still an arc when the stroke is nothing but bow. The parentheses of the condensed
            # CAD typefaces stand off their chord by less than a tenth of it, under the depth a bracket usually
            # has, and read as I - so `160(L)` became `160ILI` and a declaration of hidden connection pipes,
            # `KV/VV(1-2)`, was never found. What tells a shallow paren from a stroke that merely leans: it is
            # thin (its whole width along the row is the bow itself) and its crown sits in the middle of the
            # chord, where a kink or a hook would put it at an end.
            ts = [((x - p0[0]) * ux + (y - p0[1]) * uy) / chord for x, y in pts]
            crown = ts[max(range(len(pts)), key=lambda i: abs(sags[i]))]
            all_bow = width_along <= PAREN_THIN * H and 0.3 <= crown <= 0.7
            if one_sided and (abs(sag) >= 0.12 * chord or (all_bow and abs(sag) >= PAREN_SHALLOW * chord)):
                # bulge direction relative to reading axis: bulge toward -d (left) => '(' ; toward +d => ')'.
                # The bulge is a vector on the page - the chord's normal times the signed sagitta - and it does
                # not depend on which end the stroke was drawn from: turn the chord around and both the normal
                # and the sagitta change sign. (A flip by the chord's direction once stood here, and it turned
                # every ')' drawn bottom-to-top into a '(' - an unpaired one, which was then re-read as an I.)
                bulge_dir = (-uy * sag, ux * sag)     # normal * sag
                side = bulge_dir[0] * d[0] + bulge_dir[1] * d[1]
                return "(" if side < 0 else ")"
    return None


PAREN_SHALLOW = 0.06     # of the chord: the least bow a thin, crowned single stroke needs to be a parenthesis
PAREN_THIN = 0.2         # of the row height: the width along the row of a stroke that is nothing but bow

BRACKETS = "()[]{}"
_PARTNER = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{"}


def _drop_unpaired_brackets(rows) -> None:
    """A bracket comes in pairs. A single curved stroke that reads as one, with no partner anywhere in its row,
    is not a bracket - in these drawings it is a digit of the stroke font ('5' and ')' share an arc). Such a glyph
    is re-read as its best non-bracket candidate, and left unknown when none is close enough."""
    for row in rows:
        chars = [g.char for g in row.glyphs]
        for i, g in enumerate(row.glyphs):
            if g.char not in BRACKETS:
                continue
            partner = _PARTNER[g.char]
            if partner in chars[i + 1:] or partner in chars[:i]:
                continue                        # a real pair: leave it
            alt = next(((c, sc) for c, sc in g.alternatives if c not in BRACKETS), None)
            if alt is not None and alt[1] <= UNKNOWN_THRESHOLD:
                g.char, g.score = alt[0], alt[1]
            else:
                g.char, g.score = "?", 99.0
        row.text = "".join(g.char for g in row.glyphs)
        row.unknown_chars = sum(1 for g in row.glyphs if g.char == "?")


def _restore_separate_brackets(rows: list[TextRow], rejected: list[TextRow]) -> None:
    """Attach independently read parentheses only around actual text on the same baseline.

    Parentheses can be taller than the letters and enter a later size family.
    Punctuation alone is normally rejected as geometry. A matched pair around
    an accepted letter is typographic evidence, unlike an isolated curved line.
    """
    brackets = [r for r in rejected if r.text in ("(", ")") and len(r.glyphs) == 1
                and r.glyphs[0].score == 0.0]
    proposals: dict[tuple[str, str], list[TextRow]] = defaultdict(list)
    for row in rows:
        d, n = row_axes(row.angle)
        alnum = [g for g in row.glyphs if g.char.isalnum()]
        if not alnum:
            continue
        def center(g, axis):
            return project((g.cx, g.cy), axis)
        hs = sorted(abs(n[0]) * g.w + abs(n[1]) * g.h for g in alnum)
        H = max(hs[len(hs)//2], 1.0)
        baseline = sum(center(g, n) for g in alnum) / len(alnum)
        along = [center(g, d) for g in alnum]
        nearby = []
        for b in brackets:
            g = b.glyphs[0]
            bh = abs(n[0]) * g.w + abs(n[1]) * g.h
            if (b.source == row.source and b.layer == row.layer and b.font == row.font
                    and abs((b.angle-row.angle+90) % 180-90) <= 3
                    and .8 * H <= bh <= 1.5 * H
                    and abs(center(g, n)-baseline) <= .2 * H
                    and min(along)-H <= center(g, d) <= max(along)+H):
                nearby.append(b)
        nearby.sort(key=lambda b: center(b.glyphs[0], d))
        for left, right in zip(nearby, nearby[1:]):
            if left.text != '(' or right.text != ')':
                continue
            a, z = center(left.glyphs[0], d), center(right.glyphs[0], d)
            if z-a <= 4*H and any(a < center(g, d) < z for g in alnum):
                proposals[(left.rid, right.rid)].append(row)
    by_id = {r.rid: r for r in brackets}
    used: set[str] = set()
    for pair, targets in sorted(proposals.items()):
        if len(targets) != 1 or used.intersection(pair):
            continue
        row = targets[0]
        d, _ = row_axes(row.angle)
        glyphs = row.glyphs + [by_id[rid].glyphs[0] for rid in pair]
        glyphs.sort(key=lambda g: project((g.cx, g.cy), d))
        rebuilt = make_row(row.page, glyphs, row.angle, row.source,
                           layer=row.layer, font=row.font, family=row.family)
        row.__dict__.update(rebuilt.__dict__)
        used.update(pair)
    rejected[:] = [r for r in rejected if r.rid not in used]


_LAYER_SEP = re.compile(r"[^A-Za-z0-9]+")
_WORD = re.compile(r"[A-Za-z0-9?]+")


def layer_vocabulary(page: RawPage) -> set[str]:
    """The code-like words the file writes about itself.

    A CAD file names its layers, and a layer name is machine-written text, not drawn geometry: it carries the very
    system codes the drawn labels repeat. Only tokens that mix letters and digits are kept, because those are the
    system codes; plain words would match too easily."""
    words: set[str] = set()
    for p in page.paths:
        for t in _LAYER_SEP.split(p.layer or ""):
            if len(t) >= 3 and any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
                words.add(t.upper())
    return words


def _name_unknown_from_layers(page: RawPage, rows: list[TextRow]) -> list[dict[str, Any]]:
    """Name glyph shapes the reference alphabet cannot, from the file's own layer names.

    A drawing may write its labels in a typeface no reference alphabet holds - and embed only the handful of
    glyphs its remaining real text used - so a character can be drawn plainly and still match nothing. Where a
    word is unknown in exactly one place and exactly one layer name of the file spells it out, that name says
    what the shape is. The reading is adopted for the whole glyph family, since one shape is one character
    throughout a drawing, and every character taken this way is recorded with the name it came from.
    """
    vocab = layer_vocabulary(page)
    if not vocab:
        return []
    votes: dict[str, Counter] = defaultdict(Counter)
    evidence: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        text = row.text
        for m in _WORD.finditer(text):
            word = m.group(0).upper()
            if word.count("?") != 1 or len(word) - 1 < 3:
                continue
            k = word.index("?")
            found = {t[k] for t in vocab
                     if len(t) == len(word) and all(t[j] == word[j] for j in range(len(word)) if j != k)}
            if len(found) != 1:
                continue                        # ambiguous, or the file never spells this word: say nothing
            g = row.glyphs[m.start() + k]
            if g.char != "?":
                continue
            ch = found.pop()
            votes[g.family_id][ch] = votes[g.family_id][ch] + 1
            evidence[g.family_id].add(word.replace("?", ch))
    adopted: list[dict[str, Any]] = []
    named: dict[str, str] = {}
    for fid, counter in votes.items():
        if len(counter) != 1:
            continue                            # the same shape read two ways: no reading
        named[fid] = next(iter(counter))
    if not named:
        return []
    for row in rows:
        changed = False
        for g in row.glyphs:
            ch = named.get(g.family_id)
            if ch is None or g.char != "?":
                continue
            g.char, g.score, g.source = ch, 0.0, f"{g.source}+layer"
            changed = True
        if changed:
            row.text = "".join(g.char for g in row.glyphs)
            row.unknown_chars = sum(1 for g in row.glyphs if g.char == "?")
    for fid, ch in sorted(named.items()):
        adopted.append({"family_id": fid, "char": ch, "from_layer_names": sorted(evidence[fid])})
    return adopted


def _is_junk_row(row: TextRow) -> bool:
    """Rows that are not text: mostly unknown shapes, or periodic dash/dot line patterns."""
    chars = [g.char for g in row.glyphs if g.char != " "]
    if not chars:
        return True
    n = len(chars)
    unknown = sum(1 for c in chars if c == "?")
    dashes = sum(1 for c in chars if c in "-.|")
    alnum = sum(1 for c in chars if c.isalnum())
    if unknown > 0.5 * n:
        return True
    if alnum == 0:
        return True          # punctuation-only "rows" (pipe dashes, dots, ticks) are geometry, not text
    if n >= 4 and dashes >= 0.5 * n:
        return True
    if n >= 6 and alnum < 0.4 * n:
        return True
    return False


def _is_mark(rc: RowCluster) -> bool:
    """Ett ensamt rakt streck bland texten: ett bockmärke, inte ett tecken - och inte heller en linje.

    Storleken hör till frågan. Läsningen ställde den på tre ställen och svarade på två av dem att ett märke är
    högst MARK_MAX_PT stort, men här inte alls, och ett streck som var för långt för att vara ett märke blev
    ändå ett. Det är inte ofarligt: ett märke plockas ur geometrin, och var det i själva verket etikettens
    hänvisningslinje står etiketten sedan utan linje och röret utan namn - metrarna hamnar då hos grannröret.
    Samma gräns på alla tre ställena.
    """
    if len(rc.glyphs) != 1:
        return False
    g = rc.glyphs[0]
    if len(g.comps) != 1:
        return False
    c = g.comps[0]
    return len(c.segs) <= 2 and all(p.n_curves == 0 for p in c.paths) and max(c.w, c.h) <= MARK_MAX_PT


def _glyph_image(g: GlyphCandidate, angle: float):
    if g.kind == "s":
        return rasterize_segments_oriented(g.segs, angle)
    img, ar = rasterize_polygon_fill(g.segs, angle)
    return img, ar, skeleton_orientation(img)


def _size_class(g: GlyphCandidate, rc: RowCluster) -> str:
    gh = g.bbox[3] - g.bbox[1] if abs(rc.angle) < 45 else g.bbox[2] - g.bbox[0]
    gs = max(g.bbox[3] - g.bbox[1], g.bbox[2] - g.bbox[0])
    rel_h = gh / rc.height if rc.height else 1.0
    rel_s = gs / rc.height if rc.height else 1.0
    if rel_s < 0.18 and rel_h < 0.18:
        return "dot"
    if rel_h < 0.35:
        return "small"
    if rel_h < 0.55:
        return "mid"
    if rel_h < 0.85:
        return "x"
    return "cap"


def vector_text_rows(page: RawPage, timing: dict | None = None, say=None) -> VectorTextResult:
    """say: an optional line-by-line account of the rebuilding, for a reader watching it happen.

    This is the longest stretch of a reading with nothing to show - half a minute on a dense sheet, in which the
    only thing that had changed on screen was a sweep line. It is called with what has been worked out so far,
    never asked for anything, and nothing it is told comes back."""
    import time
    say = say or (lambda _t: None)
    t0 = time.perf_counter()
    comps = build_components(page)
    t1 = time.perf_counter()
    sizes = size_families(comps)
    say(f"{len(comps)} sammanhängande streckklumpar, i {len(sizes)} storleksfamiljer "
        + ", ".join(f"{h:.1f} pt" for h in sizes[:5]) + ". En bokstav ritas som flera streck, så de sätts ihop först.")
    clusters: list[RowCluster] = []
    marks: list[Mark] = []
    used_comp: set[str] = set()
    singles: list[RowCluster] = []
    # process size families from largest count first; a component belongs to one family only
    reserved: dict[str, float] = {}     # component -> largest text size a deferred lone glyph claimed it at
    for H in sizes:
        # smaller text may not take the strokes of a lone glyph a larger size already saw whole; larger may
        avail = [c for c in comps if c.cid not in used_comp and reserved.get(c.cid, 0.0) <= H]
        rows = cluster_rows(page, avail, H, orientation_rows=clusters)
        for r in rows:
            if _is_mark(r):
                # A short corner can be an L, and a straight stroke can be I.
                # A smaller size family may see either in isolation while the
                # next family can join it to taller parentheses or neighbours.
                # Defer marker ownership until every text size has had a turn;
                # the unused-component pass below still records genuine marks.
                continue
            # keep only rows that look like text: >=2 glyphs or a single glyph with plausible size
            if len(r.glyphs) >= 2:
                clusters.append(r)
                for g in r.glyphs:
                    for c in g.comps:
                        used_comp.add(c.cid)
            elif len(r.glyphs) == 1 and max(r.glyphs[0].bbox[3] - r.glyphs[0].bbox[1], r.glyphs[0].bbox[2] - r.glyphs[0].bbox[0]) >= 0.5 * H:
                # A lone glyph is only taken once every text size has had a turn. Seen by a smaller size family, a
                # piece of a larger character - the bowl of a 5 drawn as three strokes - is a plausible glyph on
                # its own, and claiming it there leaves the larger family half a character. Which family is the
                # commonest decided how a figure read: the same 5 in the same stamp was '5' on one sheet and two
                # '?' on the next (Årjäng, the scale bar 0-1-2-5 METER).
                singles.append(r)
                for g in r.glyphs:
                    for c in g.comps:
                        reserved[c.cid] = max(reserved.get(c.cid, 0.0), H)
    # Where two lone glyphs claim the same strokes, the one that holds more of them is the character and the
    # other is a piece of it; among equals the earlier size family keeps its turn.
    order = sorted(range(len(singles)), key=lambda k: (-sum(len(g.comps) for g in singles[k].glyphs), k))
    for r in (singles[k] for k in order):
        if all(c.cid not in used_comp for g in r.glyphs for c in g.comps):
            clusters.append(r)
            for g in r.glyphs:
                for c in g.comps:
                    used_comp.add(c.cid)
    for c in comps:
        if c.cid in used_comp:
            continue
        if len(c.segs) <= 2 and all(p.n_curves == 0 for p in c.paths) and max(c.w, c.h) <= MARK_MAX_PT:
            marks.append(Mark(mid=stable_id("mark", page.info.index, c.cid), layer=c.layer, style=c.style, bbox=c.bbox,
                              segs=c.segs, path_ids=[p.pid for p in c.paths]))
            used_comp.add(c.cid)
    marks.sort(key=lambda m: m.mid)
    t2 = time.perf_counter()
    say(f"{len(clusters)} rader klumpar sig på en baslinje, {len(marks)} klumpar är för små för att vara tecken "
        f"och läggs undan som märken.")
    # glyph families
    fam_members: dict[str, list[tuple[GlyphCandidate, RowCluster, np.ndarray, float, np.ndarray]]] = defaultdict(list)
    glyph_family: dict[str, str] = {}
    for rc in clusters:
        for g in rc.glyphs:
            img, ar, omap = _glyph_image(g, rc.angle)
            fid = family_fingerprint(img, ar, _size_class(g, rc), g.n_diacritics > 0)
            fam_members[fid].append((g, rc, img, ar, omap))
            glyph_family[g.gid] = fid
    t3 = time.perf_counter()
    say(f"{sum(len(m) for m in fam_members.values())} tecken faller i {len(fam_members)} formfamiljer. "
        f"Samma form är samma bokstav överallt på bladet, så var och en behöver namnges en gång.")
    n_relaxed = 0
    families: dict[str, FamilyResult] = {}
    for fid in sorted(fam_members):
        members = fam_members[fid]
        # representative = member with median stroke count, deterministic by gid
        members_sorted = sorted(members, key=lambda m: (len(m[0].segs), m[0].gid))
        g, rc, img, ar, omap = members_sorted[len(members_sorted) // 2]
        holes = count_holes(img)
        # lowercase allowed only when the glyph is clearly shorter than its row (x-height)
        gh = g.bbox[3] - g.bbox[1] if abs(rc.angle) < 45 else g.bbox[2] - g.bbox[0]
        rel = gh / rc.height if rc.height > 0 else 1.0
        gsize = max(g.bbox[3] - g.bbox[1], g.bbox[2] - g.bbox[0])
        rel_size = gsize / rc.height if rc.height > 0 else 1.0
        allow_lower = rel < 0.85
        ch, score, alts = classify(img, ar, holes, allow_lower=allow_lower, rel_height=rel,
                                   has_diacritic=g.n_diacritics > 0, rel_size=rel_size, omap=omap,
                                   embedded=getattr(page, "embedded_fonts", ()))
        structural = _structural_char(g, rc)
        if structural is not None:
            ch, score, alts = structural, 0.0, [(structural, 0.0)] + alts[:3]
        ch, relaxed = decide(ch, score, alts)
        if relaxed:
            n_relaxed += 1
        families[fid] = FamilyResult(family_id=fid, char=ch, score=score, alternatives=alts, holes=holes, aspect=ar, n_members=len(members))
    t4 = time.perf_counter()
    unread = sum(1 for f in families.values() if f.char == "?")
    say(f"{len(families) - unread} av {len(families)} formfamiljer gick att namnge"
        + (f", {n_relaxed} av dem med lägre krav" if n_relaxed else "")
        + (f". {unread} står som olästa tecken - de mäts inte och gissas inte." if unread else "."))
    rows: list[TextRow] = []
    rejected: list[TextRow] = []
    for rc in clusters:
        glyphs: list[Glyph] = []
        for g in rc.glyphs:
            fr = families[glyph_family[g.gid]]
            glyphs.append(Glyph(gid=g.gid, char=fr.char, bbox=g.bbox, source="stroke" if g.kind == "s" else "outline",
                                layer=g.layer, family_id=fr.family_id, score=fr.score, alternatives=fr.alternatives,
                                path_ids=g.path_ids))
        # inject spaces for large gaps along reading axis
        glyphs = _inject_spaces(glyphs, rc)
        resolve_twins(glyphs)
        row = make_row(page.info.index, glyphs, rc.angle, "stroke" if rc.kind == "s" else "outline", layer=rc.layer,
                       font=f"vector:{rc.style}", family=f"vector:{rc.layer}:{rc.style}:{rc.height:.1f}")
        if _is_junk_row(row):
            rejected.append(row)
            # its components are geometry after all: small straight ones are marker candidates (ticks, dots)
            for g in rc.glyphs:
                for c in g.comps:
                    if len(c.segs) <= 2 and all(p.n_curves == 0 for p in c.paths) and max(c.w, c.h) <= MARK_MAX_PT:
                        marks.append(Mark(mid=stable_id("mark", page.info.index, c.cid), layer=c.layer, style=c.style, bbox=c.bbox,
                                          segs=c.segs, path_ids=[p.pid for p in c.paths]))
            continue
        rows.append(row)
    _restore_separate_brackets(rows, rejected)
    rows.sort(key=lambda r: r.rid)
    marks.sort(key=lambda m: m.mid)
    t5 = time.perf_counter()
    if timing is not None:
        timing.update({"components_ms": (t1 - t0) * 1000, "rows_ms": (t2 - t1) * 1000, "raster_ms": (t3 - t2) * 1000,
                       "classify_ms": (t4 - t3) * 1000, "assemble_ms": (t5 - t4) * 1000})
    _drop_unpaired_brackets(rows)
    named = _name_unknown_from_layers(page, rows)
    return VectorTextResult(rows=rows, families=families, size_families=sizes, n_components=len(comps),
                            n_glyphs=sum(len(rc.glyphs) for rc in clusters), marks=marks, rejected_rows=rejected,
                            stats={"n_clusters": len(clusters), "n_families": len(families),
                                   "unknown_families": sum(1 for f in families.values() if f.char == "?"),
                                   "named_from_layer_names": named})


SPACE_FLOOR = 0.15        # of the row height: below this a gap is how the row sets its own letters
SPACE_TIMES = 2.0         # ...and a space is a gap this many times the row's own letter spacing


def _inject_spaces(glyphs: list[Glyph], rc: RowCluster) -> list[Glyph]:
    """Where the row stops being one word.

    A fixed fraction of the row height cannot say this. An office that sets its labels tight leaves three points
    between two whole designations and none at all between the letters of either - and against a threshold of six
    points the two labels become one word, which is a name the drawing does not contain, standing where two names
    it does contain should have been. Both are then lost: the invented one is measured, and the two real ones are
    missing from the takeoff.

    The row says it itself. Take the spacing it sets its own letters with, and a space is a gap clearly wider
    than that - twice as wide, and at least a fraction of the height so that rounding is not a word break. The
    old fixed threshold is kept as an upper bound: a gap that wide was always a space and still is.
    """
    a = math.radians(rc.angle)
    d = (math.cos(a), math.sin(a))
    H = rc.height
    spans = []
    for g in glyphs:
        corners = [(g.bbox[0], g.bbox[1]), (g.bbox[2], g.bbox[1]), (g.bbox[0], g.bbox[3]), (g.bbox[2], g.bbox[3])]
        ps = [x * d[0] + y * d[1] for x, y in corners]
        spans.append((min(ps), max(ps)))
    gaps = sorted(max(0.0, spans[i][0] - spans[i - 1][1]) for i in range(1, len(spans)))
    typical = gaps[len(gaps) // 2] if gaps else 0.0        # what this row puts between its own letters
    cut = min(0.55 * H, max(SPACE_FLOOR * H, SPACE_TIMES * typical))
    out: list[Glyph] = []
    prev_end = None
    for g, (start, end) in zip(glyphs, spans):
        if prev_end is not None and start - prev_end > cut:
            out.append(Glyph(gid=g.gid + "_sp", char=" ", bbox=(g.bbox[0], g.bbox[1], g.bbox[0], g.bbox[3]), source=g.source, layer=g.layer))
        out.append(g)
        prev_end = end
    return out
