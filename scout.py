#!/usr/bin/env python3
"""
Waiver-wire scout for "puck-knowers (and alice)".

The job: find available players who are actually producing right now, compare
them position-for-position against the weak end of Paul's roster, and propose
specific swaps. Also flag breakouts nobody drafted, and Paul's own players who
are running well under what they were drafted to do. Paul sets his own
lineups; this does not touch that.

Deliberate choices:
  * Rates, not totals. A guy who missed three weeks is not "bad", he is
    unsampled -- ranking by raw season points punishes injury returns and
    flatters anyone who merely showed up a lot.
  * Recent form is the signal, season total is the context. A breakout is
    current production above prior expectation, which is a comparison between
    a rolling window and the season, not a big cumulative number.
  * A sample floor. Two good games is noise. Below the floor a player is
    reported as "small sample", never as a recommendation.
  * Legality. This league observes ESPN's undroppable list and runs waivers,
    so an illegal drop or an unavailable add is not a suggestion.

Writes data/scout.md and data/scout.json.
"""

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"

MY_TEAM_MATCH = "ice hole"
LOCAL_TZ = timezone(timedelta(hours=-6))       # Mountain

ACTUAL, PROJECTED = 0, 1
MIN_GAMES = 8            # below this, current-season rate is not trustworthy
EDGE = 0.15              # FA must beat my guy by 15% per-game to be worth a move
TOP_N = 6                # how many swaps to surface

# Breakouts: an available player producing well above ESPN's preseason
# projection for him. Looser floor than a swap, because the point is to hear
# about him before the rest of the league does -- but still never a
# recommendation on its own.
BREAKOUT_MIN_GAMES = 5
BREAKOUT_LIFT = 0.30     # 30% over his projected per-game rate
UNDRAFTED_ADP = 200      # ESPN-wide ADP at/over this = nobody drafted him
TOP_BREAKOUTS = 8

# Roster check: one of Paul's players running this far under his own
# projection, with a real sample, is worth a second look.
SLUMP = 0.25

# ESPN position ids for hockey. Only the bucket matters here: you replace a
# forward with a forward, a goalie with a goalie.
POS_NAME = {1: "C", 2: "LW", 3: "RW", 4: "D", 5: "G"}
def bucket(pos_id):
    if pos_id == 5:
        return "G"
    if pos_id == 4:
        return "D"
    if pos_id in (1, 2, 3):
        return "F"
    return "?"


def load_statmap():
    """
    ESPN stat id -> meaning, derived from this league's own scoring by
    statmap.py. Absent on snapshots taken before the breakdown was captured;
    everything below degrades quietly to "no PP data" in that case.
    """
    f = DATA / "statmap.json"
    if not f.exists():
        # Self-heal rather than make Paul remember a step.
        try:
            import subprocess
            subprocess.run([sys.executable, str(HERE / "statmap.py")],
                           capture_output=True, timeout=60)
        except Exception:
            pass
    if not f.exists():
        return {}
    try:
        m = json.loads(f.read_text())
    except Exception:
        return {}
    return {v["candidates"][0]: sid for sid, v in m.items()
            if len(v.get("candidates") or []) == 1}


STATMAP = load_statmap()


def pp_points(p, season_id):
    """
    Power-play points this season, in league currency.

    Never added to any score -- power-play production is already inside the
    applied total. It is reported because it says something the total cannot:
    a player drawing power-play time has a role that tends to persist, while
    the same production at even strength is easier to lose.
    """
    sid = STATMAP.get("PPP")
    if not sid:
        return None
    for l in this_season(p, season_id):
        if l["src"] == ACTUAL and l["split"] == 0:
            return (l.get("applied") or {}).get(sid)
    return None


def healthy(p):
    return (p.get("injury_status") or "ACTIVE").upper() in ("ACTIVE", "NORMAL")


def this_season(p, season_id):
    """
    Stat lines for the current season only. ESPN also returns last season's
    lines, identical apart from the season tag; mixing them in would rank
    players on what they did a year ago.
    """
    return [l for l in (p.get("stat_lines") or []) if l.get("season") == season_id]


def form(p, season_id):
    """
    Per-game production now, plus the sample it rests on.

    Prefers an ESPN-supplied appliedAverage on the season split; falls back to
    total/gp. Returns (rate, games, source_label) or (None, games, reason).
    """
    lines = this_season(p, season_id)
    season = next((l for l in lines if l["src"] == ACTUAL and l["split"] == 0), None)
    if season:
        gp = season.get("gp")
        if season.get("avg") is not None and (gp or 0) >= 1:
            return season["avg"], gp or 0, "season avg"
        if gp and gp > 0:
            return round(season["total"] / gp, 2), gp, "total/gp"
    return None, (season or {}).get("gp") or 0, "no season rate yet"


def projected(p, season_id):
    """ESPN's preseason per-game projection: the expectation a breakout beats."""
    line = next(
        (l for l in this_season(p, season_id) if l["src"] == PROJECTED and l["split"] == 0),
        None,
    )
    return line.get("avg") if line else None


def recent(p, season_id):
    """
    Best rolling-window rate available, if ESPN gave us one.

    Split 0 is the season; any other split is some rolling window. We do not
    hardcode which is which (ESPN does not document hockey's split ids), we
    just take the non-season split with the most recent period and report
    which id it came from so the mapping can be confirmed against real data.
    """
    cands = [
        l for l in this_season(p, season_id)
        if l["src"] == ACTUAL and l["split"] not in (0, None) and l.get("avg") is not None
    ]
    if not cands:
        return None, None
    best = max(cands, key=lambda l: (l.get("period") or 0, l["split"]))
    return best["avg"], best["split"]


def describe(p, rate, games, rlabel=None, rrate=None):
    bits = [p.get("name") or "?", bucket(p.get("default_position_id"))]
    if rate is not None:
        bits.append(f"{rate}/gm over {games}gp")
    if rrate is not None and rate is not None and rrate > rate:
        bits.append(f"trending up ({rrate}/gm recent)")
    if (p.get("adp") or 0) >= UNDRAFTED_ADP:
        bits.append("undrafted")
    elif p.get("adp"):
        bits.append(f"ADP {p['adp']:.0f}")
    if p.get("_pp"):
        share = (p["_pp"] / p["_total"]) if p.get("_total") else None
        bits.append(f"PP {p['_pp']}"
                    + (f" ({int(share * 100)}% of his pts)" if share else ""))
    if p.get("percent_owned_change", 0) >= 1:
        bits.append(f"+{p['percent_owned_change']}% rostered")
    if not healthy(p):
        bits.append(p.get("injury_status"))
    return " · ".join(str(b) for b in bits)


def main():
    snap = DATA / "latest.json"
    if not snap.exists():
        sys.exit("No data/latest.json -- run fetch_espn.py first.")
    d = json.loads(snap.read_text())

    mine = next(
        (t for t in (d.get("teams") or []) if MY_TEAM_MATCH in (t.get("name") or "").lower()),
        None,
    )
    if mine is None:
        sys.exit("Could not find your team; edit MY_TEAM_MATCH.")

    # ---- snapshot schema check -------------------------------------------
    # Distinguish "the season hasn't started" from "this snapshot predates the
    # fetcher that captures stat splits". Both look like zero production, but
    # the first is a healthy scout with nothing to say and the second is a
    # blind one that would report preseason forever. Exiting non-zero here
    # makes the scheduled task speak up instead of staying silent.
    population = (mine.get("roster") or []) + (d.get("free_agents") or [])
    have_key = [p for p in population if "stat_lines" in p]
    total_lines = sum(len(p.get("stat_lines") or []) for p in population)
    untagged = any(
        "season" not in l for p in population for l in (p.get("stat_lines") or [])
    )

    if population and (not have_key or untagged):
        msg = (
            "Snapshot schema is out of date: stat lines are missing or carry no\n"
            "season tag, so this season cannot be told apart from last season.\n"
            "This snapshot was written by an older fetch_espn.py, so there are no\n"
            "per-game rates to work from and the scout cannot see anything.\n"
            f"Snapshot written: {d.get('fetched_at')}\n"
            "Fix: re-run fetch_espn.py to regenerate data/latest.json."
        )
        DATA.mkdir(exist_ok=True)
        (DATA / "scout.md").write_text(
            f"# Waiver scout — cannot run\n\n{msg}\n"
        )
        sys.exit(msg)

    if population and total_lines == 0:
        msg = (
            "Snapshot contains no stat lines at all for any player.\n"
            "Expected at least ESPN projections even before opening night, so the\n"
            f"fetch likely failed or returned an empty player pool ({d.get('fetched_at')}).\n"
            "Fix: re-run fetch_espn.py and check its output for errors."
        )
        DATA.mkdir(exist_ok=True)
        (DATA / "scout.md").write_text(
            f"# Waiver scout — cannot run\n\n{msg}\n"
        )
        sys.exit(msg)

    season_id = d.get("season")

    # what stat splits actually exist -- so the recent-form mapping can be
    # verified against live data instead of assumed
    splits = {}
    for p in (mine.get("roster") or []) + (d.get("free_agents") or []):
        for l in this_season(p, season_id):
            splits.setdefault((l["src"], l["split"]), 0)
            splits[(l["src"], l["split"])] += 1

    def annotate(p):
        r, g, lbl = form(p, season_id)
        rr, rs = recent(p, season_id)
        season = next((l for l in this_season(p, season_id)
                        if l["src"] == ACTUAL and l["split"] == 0), None)
        return {**p, "_rate": r, "_gp": g, "_how": lbl, "_recent": rr,
                "_proj": projected(p, season_id),
                "_pp": pp_points(p, season_id),
                "_total": (season or {}).get("total")}

    roster = [annotate(p) for p in mine.get("roster") or []]
    fas = [annotate(p) for p in d.get("free_agents") or []]

    season_started = any((p["_gp"] or 0) > 0 for p in roster + fas)

    suggestions, watch, breakouts, slumps = [], [], [], []
    for buck in ("F", "D", "G"):
        mine_b = [p for p in roster if bucket(p.get("default_position_id")) == buck]
        fa_b = [p for p in fas if bucket(p.get("default_position_id")) == buck]

        # my droppable, adequately-sampled guys, worst first
        droppable = [
            p for p in mine_b
            if p["_rate"] is not None and (p["_gp"] or 0) >= MIN_GAMES
            and p.get("droppable", True)
        ]
        droppable.sort(key=lambda p: p["_rate"])
        if not droppable:
            continue

        # available guys who are producing, best first
        hot = [p for p in fa_b if p["_rate"] is not None and (p["_gp"] or 0) >= MIN_GAMES and healthy(p)]
        hot.sort(key=lambda p: p["_rate"], reverse=True)

        # pair best add with worst drop, next best with next worst, so two
        # suggestions never ask to drop the same player
        for fa, mine_p in zip(hot, droppable):
            if fa["_rate"] <= mine_p["_rate"] * (1 + EDGE):
                break
            suggestions.append(
                {
                    "bucket": buck,
                    "add": fa,
                    "drop": mine_p,
                    "gain_per_game": round(fa["_rate"] - mine_p["_rate"], 2),
                }
            )
        weak = droppable[0]

        # promising but under-sampled -- named, never recommended
        for fa in fa_b:
            if (
                fa["_rate"] is not None
                and 0 < (fa["_gp"] or 0) < MIN_GAMES
                and healthy(fa)
                and fa["_rate"] > (weak["_rate"] or 0)
            ):
                watch.append({"bucket": buck, "player": fa, "vs": weak["_rate"]})

    # ---- breakouts: available, producing well over expectation ----------
    # Measured against his own preseason projection, not against Paul's
    # roster, so a nobody going from 0.8 to 1.6/gm shows up before he is good
    # enough to beat anyone. The rosterable bar keeps fourth-liners going from
    # 0.2 to 0.4 out: he must at least match the lowest projection among
    # Paul's players at his position.
    for buck in ("F", "D", "G"):
        bar = [p["_proj"] for p in roster
               if bucket(p.get("default_position_id")) == buck and p["_proj"]]
        if not bar:
            continue
        floor = min(bar)
        for fa in fas:
            if bucket(fa.get("default_position_id")) != buck or not healthy(fa):
                continue
            if fa["_rate"] is None or (fa["_gp"] or 0) < BREAKOUT_MIN_GAMES:
                continue
            proj = fa["_proj"] or 0
            if fa["_rate"] < floor or fa["_rate"] < proj * (1 + BREAKOUT_LIFT):
                continue
            breakouts.append({
                "bucket": buck,
                "player": fa,
                "lift": round(fa["_rate"] / proj - 1, 2) if proj > 0 else None,
                "undrafted": (fa.get("adp") or 0) >= UNDRAFTED_ADP,
            })
    # biggest surprise first; a player with no projection at all is the
    # biggest surprise there is
    breakouts.sort(key=lambda b: (b["lift"] is None, b["lift"] or 0, b["player"]["_rate"]),
                   reverse=True)

    # ---- roster check: Paul's players well under their own projection --
    for p in roster:
        if (p["_rate"] is not None and (p["_gp"] or 0) >= MIN_GAMES
                and p["_proj"] and p["_rate"] < p["_proj"] * (1 - SLUMP)):
            slumps.append({
                "bucket": bucket(p.get("default_position_id")),
                "player": p,
                "shortfall": round(1 - p["_rate"] / p["_proj"], 2),
            })
    slumps.sort(key=lambda x: x["shortfall"], reverse=True)

    suggestions.sort(key=lambda s: s["gain_per_game"], reverse=True)
    # a breakout already covers anyone who is also on the small-sample list
    flagged = {b["player"].get("espn_id") for b in breakouts[:TOP_BREAKOUTS]}
    watch = [w for w in watch if w["player"].get("espn_id") not in flagged]

    out = {
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "snapshot_fetched_at": d.get("fetched_at"),
        "season_started": season_started,
        "min_games": MIN_GAMES,
        "edge_required": EDGE,
        "statmap_loaded": bool(STATMAP),
        "stat_splits_seen": {f"src{k[0]}_split{k[1]}": v for k, v in sorted(splits.items())},
        "suggestions": [
            {
                "bucket": s["bucket"],
                "add": s["add"]["name"],
                "add_rate": s["add"]["_rate"],
                "add_gp": s["add"]["_gp"],
                "add_owned_change": s["add"].get("percent_owned_change"),
                "drop": s["drop"]["name"],
                "drop_rate": s["drop"]["_rate"],
                "drop_gp": s["drop"]["_gp"],
                "gain_per_game": s["gain_per_game"],
            }
            for s in suggestions[:TOP_N]
        ],
        "watchlist": [
            {"bucket": w["bucket"], "player": w["player"]["name"],
             "rate": w["player"]["_rate"], "gp": w["player"]["_gp"]}
            for w in watch[:8]
        ],
        "breakouts": [
            {"bucket": b["bucket"], "player": b["player"]["name"],
             "rate": b["player"]["_rate"], "gp": b["player"]["_gp"],
             "projected_rate": b["player"]["_proj"], "lift": b["lift"],
             "adp": b["player"].get("adp"), "undrafted": b["undrafted"],
             "percent_owned": b["player"].get("percent_owned"),
             "owned_change": b["player"].get("percent_owned_change")}
            for b in breakouts[:TOP_BREAKOUTS]
        ],
        "roster_check": [
            {"bucket": x["bucket"], "player": x["player"]["name"],
             "rate": x["player"]["_rate"], "gp": x["player"]["_gp"],
             "projected_rate": x["player"]["_proj"], "shortfall": x["shortfall"],
             "adp": x["player"].get("adp"),
             "droppable": x["player"].get("droppable", True)}
            for x in slumps
        ],
        "fetch_errors": d.get("errors") or [],
    }
    DATA.mkdir(exist_ok=True)
    (DATA / "scout.json").write_text(json.dumps(out, indent=2))

    # ---- readable digest -------------------------------------------------
    L = [f"# Waiver scout — {mine.get('name')} — {datetime.now(LOCAL_TZ):%a %b %d}"]
    if d.get("fetched_at"):
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(d["fetched_at"])).total_seconds() / 3600
            L.append(f"\nSnapshot {age:.1f}h old.")
        except Exception:
            pass
    if out["fetch_errors"]:
        L.append("\n**Fetch errors:** " + "; ".join(out["fetch_errors"]))

    if not season_started:
        L.append(
            "\n**No games played yet this season.** Nothing here is a real read —"
            " current-season rates are what this tool runs on, and there aren't any."
            f" Come back once players have {MIN_GAMES}+ games."
        )
    if not suggestions:
        L.append(
            f"\n## No swaps worth making\nNobody available beats the weak end of your"
            f" roster by more than {int(EDGE*100)}% per game at {MIN_GAMES}+ games played."
        )
    else:
        L.append("\n## Suggested swaps")
        for s in suggestions[:TOP_N]:
            a, dr = s["add"], s["drop"]
            L.append(
                f"\n**{s['bucket']}: add {a['name']}, drop {dr['name']}** "
                f"(+{s['gain_per_game']}/gm)\n"
                f"- available: {describe(a, a['_rate'], a['_gp'], rrate=a.get('_recent'))}\n"
                f"- yours: {describe(dr, dr['_rate'], dr['_gp'], rrate=dr.get('_recent'))}"
            )
    if breakouts:
        L.append(
            "\n## Breakouts on the wire\nAvailable, producing at least"
            f" {int(BREAKOUT_LIFT*100)}% over ESPN's preseason projection for them"
            f" ({BREAKOUT_MIN_GAMES}+ games). Not a recommendation until the reason checks out."
        )
        for b in breakouts[:TOP_BREAKOUTS]:
            p = b["player"]
            vs = (f"projected {p['_proj']}/gm, +{int(b['lift']*100)}%"
                  if b["lift"] is not None else "no preseason projection")
            L.append(
                f"- {b['bucket']}: {describe(p, p['_rate'], p['_gp'], rrate=p.get('_recent'))}"
                f" · {vs} · {p.get('percent_owned')}% rostered ESPN-wide"
            )
    if slumps:
        L.append(
            f"\n## Your players under projection\n{MIN_GAMES}+ games and at least"
            f" {int(SLUMP*100)}% below ESPN's preseason per-game projection."
        )
        for x in slumps:
            p = x["player"]
            tag = "" if p.get("droppable", True) else " · undroppable"
            L.append(
                f"- {x['bucket']}: {describe(p, p['_rate'], p['_gp'], rrate=p.get('_recent'))}"
                f" · projected {p['_proj']}/gm, -{int(x['shortfall']*100)}%{tag}"
            )
    if watch:
        L.append("\n## Small sample, worth watching")
        for w in watch[:8]:
            p = w["player"]
            L.append(f"- {w['bucket']}: {describe(p, p['_rate'], p['_gp'])}")

    L.append(
        "\n---\nAdds go through waivers in this league, so a claim is not instant."
        " Undroppable players are excluded from the drop side automatically."
    )
    (DATA / "scout.md").write_text("\n".join(L) + "\n")
    print(
        f"wrote data/scout.md  suggestions={len(suggestions)}  breakouts={len(breakouts)}"
        f"  roster_check={len(slumps)}  watch={len(watch)}  season_started={season_started}"
    )


if __name__ == "__main__":
    main()
