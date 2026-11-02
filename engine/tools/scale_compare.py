"""Skala och radstatus i två korpuskörningar, blad för blad.

corpus.py mäter rörens geometri mot facit i punkter och säger därför ingenting om skalan: ett blad i fel skala
eller med en overifierad skala får samma "rätt %". Det här verktyget läser `quantities.json` som corpus.py
sparar per blad och jämför skalans tillstånd, förhållande och skäl, och hur många meter som står CONFIRMED
respektive under en skala som inte är verifierad.

    python engine/tools/scale_compare.py FÖRE_KATALOG EFTER_KATALOG

där katalogerna är utkatalogerna från `corpus.py run`.
"""
import json
import os
import sys

MM_PER_PT = 25.4 / 72.0


def _sheet(run_dir: str, name: str) -> dict:
    path = os.path.join(run_dir, name, "quantities.json")
    if not os.path.isfile(path):
        return {"missing": True}
    with open(path, encoding="utf-8") as fh:
        q = json.load(fh)
    sc = q.get("scale") or {}
    mpp = sc.get("meters_per_pdf_point") or sc.get("meters_per_pt")
    by_state: dict[str, float] = {}
    for r in q.get("rows") or []:
        st = r.get("state") or "?"
        by_state[st] = round(by_state.get(st, 0.0) + float(r.get("confirmed_horizontal_m") or 0.0), 2)
    return {"state": sc.get("state"), "ratio": round(mpp * 1000 / MM_PER_PT, 1) if mpp else None,
            "reason": sc.get("reason"), "metres_by_row_state": by_state}


def main(before: str, after: str) -> int:
    names = sorted({n for d in (before, after) for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))})
    changed = 0
    for name in names:
        b, a = _sheet(before, name), _sheet(after, name)
        same = (b.get("state"), b.get("ratio")) == (a.get("state"), a.get("ratio"))
        changed += not same
        mark = "  " if same else "≠ "
        print(f"{mark}{name:10} före {b.get('state')!s:16} 1:{b.get('ratio')!s:7}  efter {a.get('state')!s:16} "
              f"1:{a.get('ratio')!s:7}")
        if not same:
            print(f"     före:  {b.get('reason')}")
            print(f"     efter: {a.get('reason')}")
        if b.get("metres_by_row_state") != a.get("metres_by_row_state"):
            print(f"     meter per radstatus före {b.get('metres_by_row_state')}")
            print(f"     meter per radstatus efter {a.get('metres_by_row_state')}")
    print(f"{changed} av {len(names)} blad har ändrat skaltillstånd eller förhållande")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
