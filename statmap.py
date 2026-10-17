#!/usr/bin/env python3
"""
Work out what ESPN's hockey stat ids mean, from this league's own scoring.

ESPN does not document hockey stat ids, and guessing them is how a tool starts
confidently reporting the wrong column. We don't guess: for every stat id the
API returns both a raw count and the points this league's scoring awarded it,
and the ratio between them IS the league's weight for that stat.

Paul's weights: G=2, A=1, PPP=0.5, SHP=0.5, GWG=1, HAT=1, SOG=0.1, HIT=0.2,
BLK=0.4; goalies W=4, GA=-2.3, SV=0.3, SO=4, OTL=1. So a ratio of 0.4 is
blocked shots, 0.2 is hits, -2.3 is goals against, and so on. Two stats share
0.5 (PPP and SHP) and three share 1.0 (A, GWG, HAT); those are separated by
frequency, since power-play points are common and short-handed points are rare.

Run after a fetch. Writes data/statmap.json.
"""
import json, pathlib, collections

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"

WEIGHTS = {2.0:["G"], 1.0:["A","GWG","HAT"], 0.5:["PPP","SHP"], 0.4:["BLK"],
           0.3:["SV"], 0.2:["HIT"], 0.1:["SOG"], 4.0:["W","SO"], -2.3:["GA"]}

def main():
    snap = DATA / "latest.json"
    if not snap.exists():
        raise SystemExit("no data/latest.json -- run fetch_espn.py first")
    d = json.loads(snap.read_text())
    pop = [p for t in d.get("teams", []) for p in t.get("roster", [])] + \
          (d.get("free_agents") or [])

    ratios = collections.defaultdict(list)
    freq = collections.Counter()
    for p in pop:
        for l in (p.get("stat_lines") or []):
            raw, app = l.get("raw") or {}, l.get("applied") or {}
            for sid, cnt in raw.items():
                if sid in app and cnt:
                    ratios[sid].append(round(app[sid] / cnt, 3))
                    freq[sid] += 1

    if not ratios:
        raise SystemExit(
            "No per-stat breakdown in the snapshot.\n"
            "This snapshot predates the breakdown capture -- re-run "
            "fetch_espn.py (the daily cron will do it) and try again."
        )

    out = {}
    for sid, rs in sorted(ratios.items(), key=lambda kv: -freq[kv[0]]):
        w = collections.Counter(rs).most_common(1)[0][0]
        cands = WEIGHTS.get(w, ["?"])
        out[sid] = {"weight": w, "candidates": cands, "seen": freq[sid]}

    # PPP vs SHP: both weight 0.5, but power-play points are far more common
    half = [(s, v) for s, v in out.items() if v["weight"] == 0.5]
    if len(half) >= 2:
        half.sort(key=lambda kv: -kv[1]["seen"])
        out[half[0][0]]["candidates"] = ["PPP"]
        out[half[-1][0]]["candidates"] = ["SHP"]

    DATA.mkdir(exist_ok=True)
    (DATA / "statmap.json").write_text(json.dumps(out, indent=2))
    print(f"{'stat id':<10} {'weight':>8} {'seen':>7}  likely")
    for sid, v in out.items():
        print(f"{sid:<10} {v['weight']:>8} {v['seen']:>7}  {'/'.join(v['candidates'])}")
    print("\nwrote data/statmap.json")

if __name__ == "__main__":
    main()
