"""The facit corpus as a gate: every sheet read the way production reads it, held against its facit takeoff, and
the result held against the last accepted run.

A change to the reading is judged sheet by sheet, never on a total: a gain on one sheet can hide a loss on another.
Beside the facit's metres - right, under the wrong size or system, missed - each sheet reports what the reading
measured where the facit has no pipe, as a share of the facit's metres. That share is shown and kept with the
baseline, and the gate does not refuse on it yet: a takeoff does not always take off everything the sheet draws,
and what the share does from one run to the next has not been measured.
The manifest names the sheets and their facit PDFs (the drawings themselves are client material and stay outside
the repository); the baseline is the per-sheet result of the last run that was accepted.

    python engine/tools/corpus.py run manifest.json out_dir [--jobs 4] [--rules-off twin,terminal] [--labels]
    python engine/tools/corpus.py gate out_dir/corpus.json engine/tests/data/corpus_baseline.json [--only-read]
    python engine/tools/corpus.py accept out_dir/corpus.json engine/tests/data/corpus_baseline.json

manifest.json: {"sheets": [{"name": "A0011", "pdf": "/path/A0011.pdf", "facit": "/path/A0011_facit.pdf",
                            "labels": "/path/t5.json"}, ...]}
(the facit may be the drawing itself, when the takeoff was drawn on it; "labels" is optional)

Two modes. Without `--labels` every sheet is read as production reads it, with no external labels - the layer
rule is then off, and the result must not be worse than the baseline. With `--labels` only the sheets that carry a
"labels" file are read, each with its labels (VS-Kalkyl's T5, vvs_engine/text/external_labels.py), and the layer
rule on; gate those with `--only-read`, which compares only the sheets the run read.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import facit_gap  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEEP = {"physical-pipes.json", "quantities.json", "source-assignment.json", "layer-name-rule.json",
        "external-labels.json"}
RIGHT_DROP = 0.5      # percentage points of facit metres a sheet may lose before the gate refuses the change
RISER_RISE = 1        # ...and risers further off the facit count

READ = """
import os, sys
sys.path.insert(0, {backend!r}); sys.path.insert(0, {root!r})
from app.analysis_worker import analyze_isolated
pdf, out, labels = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
analyze_isolated(pdf, out, name=os.path.basename(pdf), rule_values={{}}, label_audit=False, deadline_s=1800,
                 external_labels_path=labels or None,
                 determinism=False, contamination=True, progress=lambda *a: None, review=True, review_ocr=False,
                 ocr_assist=False, second_reader_enabled=False, known_families=None, known_legend=None, given_scale=None,
                 source_mode="combined", native_detection=True, native_cache_dir=out + "/cache", source_style="auto")
"""


def read_sheet(sheet, out_dir, rules_off, use_labels=False):
    """One sheet through the production reading, in its own process, without a model; only the JSON kept."""
    run = os.path.join(out_dir, sheet["name"])
    os.makedirs(run, exist_ok=True)
    env = {**os.environ, "OPENAI_API_KEY": "", "VVS_RULES_OFF": rules_off or ""}
    script = READ.format(backend=os.path.join(ROOT, "backend"), root=ROOT)
    with open(os.path.join(out_dir, sheet["name"] + ".log"), "w") as log:
        labels = sheet.get("labels", "") if use_labels else ""
        code = subprocess.run([sys.executable, "-c", script, sheet["pdf"], run, labels], env=env, stdout=log,
                              stderr=subprocess.STDOUT).returncode
    for dirpath, dirnames, files in os.walk(run, topdown=False):
        for f in files:
            if not (dirpath == run and f in KEEP):
                os.remove(os.path.join(dirpath, f))
        if dirpath != run:
            os.rmdir(dirpath)
    return code


def score(sheet, run):
    out, _ = facit_gap.gap(sheet["facit"], run)
    f = out["facit"] or 1
    rf, ro, off = facit_gap.riser_gap(sheet["facit"], run)
    return {"right": round(100 * out["right"] / f, 2), "wrong_dn": round(100 * out["wrong_dn"] / f, 2),
            "wrong_system": round(100 * out["wrong_system"] / f, 2), "missed": round(100 * out["missed"] / f, 2),
            "outside": round(100 * out["outside"] / f, 2),
            "facit_pt": round(out["facit"], 1), "risers_facit": rf, "risers_ours": ro, "risers_off": off}


def run(args):
    with open(args.manifest, encoding="utf-8") as fh:
        sheets = json.load(fh)["sheets"]
    if args.labels:
        sheets = [s for s in sheets if s.get("labels")]
        if not sheets:
            sys.exit("--labels: no sheet in the manifest carries a \"labels\" file")
    os.makedirs(args.out, exist_ok=True)
    with ThreadPoolExecutor(args.jobs) as pool:
        codes = list(pool.map(lambda s: read_sheet(s, args.out, args.rules_off, args.labels), sheets))
    result = {"rules_off": args.rules_off or "", "labels": bool(args.labels), "sheets": {}}
    for sheet, code in zip(sheets, codes):
        run_dir = os.path.join(args.out, sheet["name"])
        if code or not os.path.exists(os.path.join(run_dir, "physical-pipes.json")):
            result["sheets"][sheet["name"]] = {"failed": True}
            continue
        result["sheets"][sheet["name"]] = score(sheet, run_dir)
        rule = os.path.join(run_dir, "layer-name-rule.json")
        if os.path.exists(rule):
            with open(rule, encoding="utf-8") as fh:
                result["sheets"][sheet["name"]]["layer_name_families"] = json.load(fh).get("families_added", [])
    path = os.path.join(args.out, "corpus.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
    report(result["sheets"])
    print(path)


def report(sheets, base=None):
    for name, s in sorted(sheets.items()):
        if s.get("failed"):
            print(f"{name:8} FAILED")
            continue
        b = (base or {}).get(name) or {}
        o = f", utanför {s['outside'] - b['outside']:+.1f}" if "outside" in b and "outside" in s else ""
        d = f"  ({s['right'] - b['right']:+.1f}{o}, stigare {s['risers_off'] - b['risers_off']:+d})" if "right" in b else ""
        u = f"  utanför facit {s['outside']:4.1f}" if "outside" in s else ""
        print(f"{name:8} rätt {s['right']:5.1f} %  fel DN {s['wrong_dn']:4.1f}  fel system {s['wrong_system']:4.1f}  "
              f"missad {s['missed']:4.1f}{u}  stigare {s['risers_ours']}/{s['risers_facit']} (fel {s['risers_off']}){d}")
        if s.get("layer_name_families"):
            print(f"{'':8} lagerregeln tog in: {', '.join(f.split('|s|')[0] for f in s['layer_name_families'])}")
    ok = [s for s in sheets.values() if not s.get("failed")]
    f = sum(s["facit_pt"] for s in ok) or 1
    u = (f"utanför facit {sum(s['outside'] * s['facit_pt'] for s in ok) / f:.1f} %  "
         if all("outside" in s for s in ok) else "")
    print(f"TOTALT rätt {sum(s['right'] * s['facit_pt'] for s in ok) / f:.1f} %  {u}"
          f"stigare fel {sum(s['risers_off'] for s in ok)} av {sum(s['risers_facit'] for s in ok)}")


def gate(args):
    with open(args.result, encoding="utf-8") as fh:
        now = json.load(fh)["sheets"]
    with open(args.baseline, encoding="utf-8") as fh:
        base = json.load(fh)["sheets"]
    if getattr(args, "only_read", False):
        base = {k: v for k, v in base.items() if k in now}
    report(now, base)
    worse = []
    for name, b in sorted(base.items()):
        s = now.get(name)
        if s is None or s.get("failed"):
            worse.append(f"{name}: not read")
        elif s["right"] < b["right"] - RIGHT_DROP:
            worse.append(f"{name}: right {b['right']} -> {s['right']}")
        elif s["risers_off"] > b["risers_off"] + RISER_RISE:
            worse.append(f"{name}: risers off {b['risers_off']} -> {s['risers_off']}")
    for w in worse:
        print("WORSE", w)
    sys.exit(1 if worse else 0)


def accept(args):
    with open(args.result, encoding="utf-8") as fh:
        result = json.load(fh)
    with open(args.baseline, "w", encoding="utf-8") as fh:
        json.dump({"sheets": result["sheets"]}, fh, indent=1, sort_keys=True)
        fh.write("\n")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("manifest"); r.add_argument("out")
    r.add_argument("--jobs", type=int, default=4); r.add_argument("--rules-off", default="")
    r.add_argument("--labels", action="store_true", help="read only the sheets with a labels file, with it")
    r.set_defaults(fn=run)
    for name, fn in (("gate", gate), ("accept", accept)):
        g = sub.add_parser(name)
        g.add_argument("result"); g.add_argument("baseline")
        if name == "gate":
            g.add_argument("--only-read", action="store_true", help="compare only the sheets the run read")
        g.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
