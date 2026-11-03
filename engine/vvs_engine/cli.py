"""Command line interface: analyze a clean vector VVS PDF and write all artifacts."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import __version__
from .contamination import scan_source
from .determinism import run_determinism
from .output.schema import stamp as _stamp
from .output.artifacts import why as why_fn, write_all, write_sheet
from .output.overlays import OverlayWriter
from .pdf.extract import extract_document
from .pipeline import PageAnalysis, analyze_page, prepare_page, reading_coverage, summarize
from .semantics.legend import DrawingLegend, learn_roles, merged

from . import rules as _rules


def _R(rule_id, default):
    """Vad regeln står på för den läsning som körs på den här tråden."""
    return _rules.value(rule_id, default)


CONFIG = {"contact_tolerance_pt": 0.6, "touch_tolerance_pt": 0.15, "unknown_glyph_threshold": 0.14, "grid": 32}


class AnalysisTookTooLong(Exception):
    """A reading that ran past its budget. Raised between pages, so what is reported is a refusal, not a guess."""


VOCAB_HOLD = 2          # readings kept from the search for the list; past that, looking again is cheaper than keeping


def _upright(pdf_path: str, doc, pages, out_dir: str, progress, external_labels=None):
    """The file to read: the one given, or a copy with the pages that lie on their side turned to read.

    Every sheet's lettering is read as it lies; one that names few pipes with a size is tried a quarter turn each
    way (see pdf/orient.py). The readings already paid for on the first sheets are handed on when nothing had to
    be turned, so a sheet that reads as it lies costs nothing more to check."""
    from .pdf.orient import turn_for, write_turned
    turns: dict[int, int] = {}
    held: dict[int, Any] = {}
    for i in range(len(doc.pages)):
        pg = doc.pages[i]
        prep = prepare_page(pg, external_labels=external_labels)
        # a page read from pixels has already read its text both ways round; turning it means reading it again
        from_image = (pg.input_class or {}).get("read_as") == "raster"
        t = 0 if from_image else turn_for(pdf_path, pg.info.index, prep)
        if t:
            turns[pg.info.index] = t
        if len(held) < _R("cli.VOCAB_HOLD", VOCAB_HOLD):
            held[i] = prep
        elif i > 0:
            doc.pages.release(i)
    if not turns:
        return pdf_path, doc, held, {}
    if progress:
        progress("READING_PDF")
    turned = write_turned(pdf_path, turns, os.path.join(out_dir, "upright.pdf"))
    return turned, extract_document(turned, pages, progress=progress, eager=False), {}, turns


def _vocabulary(doc, known_legend: DrawingLegend | None, progress, ocr_assist: bool, film_sink=None,
                held: dict | None = None, external_labels=None):
    """The set's designation list, and the sheet readings the search for it already paid for.

    Sheets are looked at in order and the search stops at the first that carries a list, which on almost every
    set is the front sheet. What it looked at is handed back so the reading proper does not do that work twice -
    but only the first couple of them, because holding a sheet's reading costs more than reading it again.
    """
    from .film import Film
    vocab = known_legend
    held = dict(held or {})
    for i in range(len(doc.pages)):
        # The search for the list reads the front half of a sheet, and that is the half that takes the longest
        # with nothing to show. So it narrates while it goes, on the sheet the film is about.
        prep = held.get(i) if not ocr_assist else None
        if prep is None:
            prep = prepare_page(doc.pages[i], progress if i == 0 else None, ocr_assist,
                                Film(film_sink) if i == 0 else None, external_labels)
        if len(held) < _R("cli.VOCAB_HOLD", VOCAB_HOLD):
            held[i] = prep
        elif i > 0:
            doc.pages.release(i)
        if prep.legend.own and prep.legend.entries:
            vocab = merged(vocab, prep.legend)
            break
    return vocab, held


SET_SCALE_MIN = 2           # sheets that must agree before the set is taken to have one scale
SET_SCALE_TOL = 0.02        # ...and how far apart two of them may be and still be the same scale


def scale_of_the_set(sheets: list[dict]) -> tuple[float, list[int]] | None:
    """The scale the set is drawn in, where its sheets agree about it.

    A set of plan sheets is drawn in one scale and every stamp says so. Two sheets that settled the same figure
    are that statement; one sheet is a sheet, and a set whose sheets disagree has details among its plans and is
    not saying anything about the sheet that failed. Where nothing is agreed, nothing is lent.
    """
    settled = [(sh.get("page"), (sh.get("scale") or {}).get("meters_per_pt")) for sh in sheets
               if (sh.get("scale") or {}).get("state") in ("VERIFIED", "TEXT_ONLY", "BAR_ONLY", "DIMENSIONS_ONLY")]
    got = [(pg, v) for pg, v in settled if v]
    if len(got) < _R("cli.SET_SCALE_MIN", SET_SCALE_MIN):
        return None
    got.sort(key=lambda t: t[1])
    mid = got[len(got) // 2][1]
    if any(abs(v - mid) > _R("cli.SET_SCALE_TOL", SET_SCALE_TOL) * mid for _, v in got):
        return None                 # plans and details in one file: the set has no single scale to lend
    # the sheets that said it travel with the figure: a borrowed scale has to be able to name its source
    return mid, sorted(pg for pg, _ in got if pg is not None)


def sheet_record(pa) -> dict:
    """One sheet of the set, as the takeoff for the whole set needs it.

    A reading is a large object and a set has many sheets. What a set-wide takeoff wants from each is small: what
    was measured, how far the reading got, and under what scale - so that is what is kept, and the reading itself
    is let go of.
    """
    anchors = pa.anchors
    return {
        "page": pa.page.info.index,
        "scale": {"state": pa.scale.state, "meters_per_pt": pa.scale.meters_per_pt, "reason": pa.scale.reason},
        "designations": len(pa.designations),
        "leaders": len(pa.leaders),
        "verified_attachments": sum(1 for a in anchors if a.state == "VERIFIED_PIPE_ATTACHMENT"),
        "ambiguous_attachments": sum(1 for a in anchors if a.state == "AMBIGUOUS_PIPE_ATTACHMENT"),
        "no_attachments": sum(1 for a in anchors if a.state == "NO_PIPE_ATTACHMENT"),
        "legend": {"codes": len(pa.legend.entries), "own": pa.legend.own},
        "coverage": reading_coverage(pa),
        # pipe_ids följer med: utan dem kan handlingens rad inte peka tillbaka på de sträckor den kom ur, och
        # "visa hur mängden räknades" har inget att visa för en handling med flera blad
        "quantities": [{k: q.get(k) for k in ("designation", "base", "dn", "state", "label_count",
                                              "physical_pipe_count", "confirmed_horizontal_m",
                                              "confirmed_vertical_m", "confirmed_total_m", "ambiguous_m",
                                              "in_hatched_area_m", "riser_count", "riser_count_from_labels",
                                              "riser_height_m", "pipe_ids")}
                       for q in pa.quantities],
        "second_reader": pa.second_reader,
        "source_assignment": {k: v for k, v in (pa.source_assignment or {}).items()
                              if k in ("mode", "selected", "comparison_status", "statuses")},
        # varje beteckning bladet skriver - bara i läget "Alla"; en VVS-läsnings blad ser ut som förut
        **({"register": pa.register} if getattr(pa, "register", None) is not None else {}),
    }


def analyze_pdf(pdf_path: str, out_dir: str, name: str | None = None, determinism: bool = True, contamination: bool = True,
                progress=None, pages: list[int] | None = None, review: bool = True, review_ocr: bool = True,
                film_sink=None,
                ocr_assist: bool = False, deadline_s: float | None = None, second_reader=None, known_families: dict | None = None,
                known_legend: DrawingLegend | None = None, given_scale: dict[int, float] | float | None = None,
                source_mode: str | None = None, source_ask=None, source_style="auto", source_detector=None,
                external_labels=None) -> dict:
    """external_labels: etiketter som en annan läsare redan läst (text/external_labels.py, `--labels`). Texten
    byts i motorns rader, geometrin behålls; filens sha256 står i frysmanifestets konfiguration.

    deadline_s: a wall-clock budget for the whole document, checked between pages.

    A drawing set can carry a page dense enough that reading it takes longer than anyone will wait, and without a
    budget that page does not just delay itself - it holds the worker thread and everything queued behind it. The
    budget is checked between pages rather than inside one, so a single page that runs long still finishes; what
    it bounds is a document that would never end.

    known_legend: a designation list the rest of the project already wrote, for sheets that carry none of their
    own. The document's own sheets are the first source of it - see below - and this is what a project that spans
    several files can pass in besides.
    """
    t_all = time.perf_counter()
    name = name or os.path.splitext(os.path.basename(pdf_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    timings: dict[str, float] = {}
    if progress:
        progress("READING_PDF")
    t0 = time.perf_counter()
    doc = extract_document(pdf_path, pages, progress=progress, eager=False)
    timings["extract_ms"] = (time.perf_counter() - t0) * 1000
    n_pages = len(doc.pages)
    done = 0

    def check_budget():
        if deadline_s is not None and time.perf_counter() - t_all > deadline_s and done:
            raise AnalysisTookTooLong(
                f"läsningen hann {done} av {n_pages} sidor inom {deadline_s:.0f} s och avbröts; "
                f"en halv mängd är sämre än ingen, så inget delresultat sparas")

    # First the set's own vocabulary, then the sheets read against it. A set writes its designation list once, on
    # the sheet that has room for it, and lets the rest stand on that; a reading that takes each sheet as it comes
    # would have to go back and read the early ones again once the list turned up. Looking for the list first
    # costs nothing where it is on the front sheet, because that sheet's reading is kept and used.
    # A sheet lying on its side is turned to read first: everything after reads, and shows, the upright sheet.
    pdf_path, doc, checked, turns = _upright(pdf_path, doc, pages, out_dir, progress, external_labels)
    if turns and external_labels is not None:
        # etiketterna lästes på bladet som det låg; ett vridet blad har andra koordinater och får dem inte
        external_labels = external_labels.without(turns)
    vocab, held = _vocabulary(doc, known_legend, progress, ocr_assist, film_sink, held=checked,
                              external_labels=external_labels)
    if progress:
        progress("READING_PDF")
    overlay = OverlayWriter(pdf_path, out_dir)
    first: PageAnalysis | None = None
    sheets: list[dict] = []
    # skalan någon har skrivit in: ett tal för hela handlingen, eller ett per blad
    by_hand = given_scale if isinstance(given_scale, dict) else ({} if given_scale is None else None)
    for i in range(n_pages):
        check_budget()
        pg = doc.pages[i]
        pa = analyze_page(pg, progress, ocr_assist=ocr_assist,
                          film_sink=film_sink if pg.info.index == 0 else None,
                          second_reader=second_reader, known_families=known_families, known_legend=vocab,
                          given_scale=(given_scale if by_hand is None else by_hand.get(i)),
                          prepared=held.pop(i, None), source_mode=source_mode, source_ask=source_ask, source_style=source_style, source_detector=source_detector,
                          external_labels=external_labels)
        # what this sheet showed about its codes goes back into the set's list, so the sheets after it start
        # from what has been read rather than from a column of words with no verdict on any row
        learn_roles(vocab, pa.legend)
        overlay.add(pa)
        sheets.append(sheet_record(pa))
        # The sheet is written down while it is still in hand. Keeping every reading until the end is what makes
        # a fifty-sheet set need fifty readings' worth of geometry at once, and reporting only the one sheet that
        # was kept is what makes a set of twenty-six drawings look like an empty first page.
        write_sheet(pa, doc, out_dir, vocab)
        done += 1
        # A sheet's reading is used the moment it exists - drawn onto the overlays, written down as a row - and
        # then let go of. Keeping all of them is what makes a fifty-sheet set need a reading's worth of geometry
        # per sheet all at once; the first sheet is kept because the artifacts and the checks are about it.
        if i == 0:
            first = pa
        else:
            doc.pages.release(i)
    # A sheet whose own stamp settled nothing is not unmeasurable when its siblings all say the same thing about
    # how big the drawing is. Those sheets - and only those - are read again with the set's scale, which is a
    # bounded amount of work: on a set where one stamp is unclear, it is one sheet.
    lent = scale_of_the_set(sheets)
    set_scale, set_scale_pages = lent if lent is not None else (None, [])
    rescaled = 0
    if set_scale is not None:
        for i, sh in enumerate(sheets):
            if (sh.get("scale") or {}).get("state") not in ("NONE", "CONFLICT"):
                continue
            if (given_scale if by_hand is None else by_hand.get(i)):
                continue        # någon har redan sagt vad bladet är ritat i; omgången får inte skriva över det
            check_budget()
            pa = analyze_page(doc.pages[i], progress, ocr_assist=ocr_assist,
                              film_sink=film_sink if i == 0 else None, second_reader=second_reader,
                              known_families=known_families, known_legend=vocab, known_scale=set_scale,
                              known_scale_pages=[p for p in set_scale_pages if p != i], source_mode=source_mode, source_ask=source_ask, source_style=source_style, source_detector=source_detector,
                              external_labels=external_labels)
            learn_roles(vocab, pa.legend)
            overlay.replace(pa)
            sheets[i] = sheet_record(pa)
            write_sheet(pa, doc, out_dir, vocab)      # bladet lästes om under handlingens skala; det som stod är inte längre sant
            rescaled += 1
            if i == 0:
                first = pa
            else:
                doc.pages.release(i)
    if progress:
        progress("GENERATING_OVERLAYS")
    t0 = time.perf_counter()
    overlays = overlay.close()
    timings["overlays_ms"] = (time.perf_counter() - t0) * 1000
    rev = None
    if review:
        if progress:
            progress("REVIEWING")
        t0 = time.perf_counter()
        from .review import run_review
        rev = run_review(first, ocr=review_ocr,
                         progress=(lambda t: progress(f"REVIEWING {t}")) if progress else None)
        timings["review_ms"] = (time.perf_counter() - t0) * 1000
    # A second reader is asked only about cases the geometry already declared open, and only among candidates the
    # drawing itself offers - but it is still a machine outside this one, so a reading that consulted it is not
    # the same kind of answer as one that did not, and the determinism check is meaningless over it.
    consulted = any((sh.get("second_reader") or {}).get("asked") for sh in sheets)
    det = run_determinism(doc, 0, first, known_families=known_families, known_legend=vocab,
                          external_labels=external_labels) \
        if determinism and not consulted and source_mode is None else None
    cont = scan_source(os.path.dirname(os.path.abspath(__file__))) if contamination else None
    t0 = time.perf_counter()
    timings["total_s"] = time.perf_counter() - t_all
    config = dict(CONFIG)
    if external_labels is not None:
        config["external_labels"] = {"sha256": external_labels.sha256, "reader": external_labels.meta,
                                     "pages": sorted(external_labels.by_page)}
    files = write_all(pdf_path, doc, [first], out_dir, name, timings, det, cont, overlays, config, rev,
                      sheets=sheets, doc_legend=vocab)
    timings["artifacts_ms"] = (time.perf_counter() - t0) * 1000
    summary = {"name": name, "pages": n_pages, "summary": summarize(first),
               "source_assignment": {"mode": source_mode, "pages": [sh["source_assignment"] for sh in sheets]},
               "determinism": det["state"] if det else ("NOT_APPLICABLE_A_SECOND_READER_WAS_CONSULTED" if consulted else None),
               "second_reader": {"consulted": consulted,
                                 "asked": sum((sh.get("second_reader") or {}).get("asked", 0) for sh in sheets),
                                 "settled": sum((sh.get("second_reader") or {}).get("settled", 0) for sh in sheets),
                                 "refused": sum((sh.get("second_reader") or {}).get("refused", 0) for sh in sheets)},
               "legend": {"codes": len(vocab.entries) if vocab else 0,
                          "own_sheet": bool(first.legend.own and first.legend.entries),
                          "from_sheet": next((e.page for e in (vocab.entries if vocab else []) if e.page is not None), None)},
               # registret i läget "Alla" står i all-designations.json; här bara dess summor
               "sheets": [{**{k: v for k, v in sh.items() if k != "register"},
                           **({"register": {"totals": sh["register"]["totals"]}} if sh.get("register") else {})}
                          for sh in sheets],
               "scale": {"of_the_set": set_scale, "sheets_reread_with_it": rescaled},
               "contamination": cont["state"] if cont else None, "files": files, "total_seconds": round(timings["total_s"], 2),
               "input": getattr(doc.pages[0], "input_class", None), "skipped_pages": doc.skipped_pages,
               "review": {"state": rev["state"], "n_findings": rev["n_findings"], "agents": rev["agents"]} if rev else None,
               "ocr_assist": first.ocr_assist,
               # sidor som låg på sidan och vreds för att kunna läsas: filen som lästes, och som ska visas, är den vridna
               "turned_pages": {str(k): v for k, v in turns.items()},
               "upright_file": os.path.basename(pdf_path) if turns else None}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(_stamp(summary), fh, indent=1, default=str)
    if progress:
        progress("COMPLETED")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vvs-takeoff", description="Drawing-adaptive VVS pipe takeoff engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="analyze a clean vector VVS PDF")
    a.add_argument("pdf"); a.add_argument("--out", required=True); a.add_argument("--name")
    a.add_argument("--no-determinism", action="store_true"); a.add_argument("--no-contamination", action="store_true")
    a.add_argument("--labels", help="etiketter lästa av en annan läsare (JSON, se vvs_engine/text/external_labels.py)")
    w = sub.add_parser("why", help="evidence chain for a physical pipe id (re-analyzes the PDF)")
    w.add_argument("pdf"); w.add_argument("pipe_id")
    sub.add_parser("version")
    args = ap.parse_args(argv)
    if args.cmd == "version":
        print(__version__); return 0
    if args.cmd == "analyze":
        labels = None
        if args.labels:
            from .text.external_labels import ExternalLabels
            labels = ExternalLabels.load(args.labels)
        s = analyze_pdf(args.pdf, args.out, args.name, not args.no_determinism, not args.no_contamination, progress=lambda st: print(f"[{st}]", file=sys.stderr),
                        external_labels=labels)
        print(json.dumps(s["summary"], indent=1, default=str)); return 0
    if args.cmd == "why":
        doc = extract_document(args.pdf)
        pa = analyze_page(doc.pages[0])
        print(json.dumps(why_fn(pa, args.pipe_id), indent=1, default=str)); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
