#!/usr/bin/env python3
"""Compute per-manager draft and FAAB tendencies from harvested history.

Reads data/league-history/<slug>.json (from league_history.py) and writes
data/analysis/<slug>.json. Pure local computation; no network calls.

Attribution: draft picks use picked_by (user_id); transactions use
creator (user_id). Roster ids are mapped to owners per season as a
fallback. Managers are keyed by user_id so renames don't split history.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

EARLY_WEEKS = range(1, 5)
MID_WEEKS = range(5, 10)


def week_bucket(week):
    if week in EARLY_WEEKS:
        return "early"
    if week in MID_WEEKS:
        return "mid"
    return "late"


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def analyze(history):
    seasons = sorted(
        [s for s in history["seasons"]], key=lambda s: s["season"]
    )
    players = history.get("players") or {}

    managers = {}

    def manager(uid, season):
        if uid not in managers:
            managers[uid] = {
                "display_name": None,
                "team_names": {},
                "seasons": [],
                "records": {},
                "draft": {"seasons": {}},
                "faab": {"seasons": {}},
            }
        m = managers[uid]
        if season not in m["seasons"]:
            m["seasons"].append(season)
        return m

    league_price_rows = []
    contested = []

    for s in seasons:
        season = s["season"]
        completed = s["status"] == "complete"
        users = s.get("users") or {}
        roster_owner = {
            r["roster_id"]: r.get("owner_id") for r in (s.get("rosters") or [])
        }

        for uid, u in users.items():
            m = manager(uid, season)
            m["display_name"] = u.get("display_name") or m["display_name"]
            if u.get("team_name"):
                m["team_names"][season] = u["team_name"]

        if completed:
            for r in s.get("rosters") or []:
                uid = r.get("owner_id")
                if uid is None:
                    continue
                manager(uid, season)["records"][season] = {
                    "wins": r.get("wins", 0),
                    "losses": r.get("losses", 0),
                    "fpts": r.get("fpts", 0),
                }

        # ---- draft ----
        for d in s.get("drafts") or []:
            order = d.get("order") or {}
            for uid, slot in order.items():
                manager(uid, season)["draft"]["seasons"].setdefault(
                    season, {}
                )["slot"] = slot
            by_uid = defaultdict(list)
            for p in d.get("picks") or []:
                uid = p.get("picked_by") or roster_owner.get(p.get("roster_id"))
                if not uid:
                    continue
                by_uid[uid].append(p)
            for uid, picks in by_uid.items():
                picks.sort(key=lambda p: p.get("pick_no") or 0)
                ds = manager(uid, season)["draft"]["seasons"].setdefault(season, {})
                pos_rounds = defaultdict(list)
                for p in picks:
                    if p.get("pos"):
                        pos_rounds[p["pos"]].append(p.get("round"))
                ds["picks"] = [
                    {
                        "pick_no": p.get("pick_no"),
                        "round": p.get("round"),
                        "pos": p.get("pos"),
                        "player": p.get("player"),
                    }
                    for p in picks
                ]
                ds["first_round_by_pos"] = {
                    pos: min(rs) for pos, rs in pos_rounds.items() if rs
                }
                ds["pos_counts"] = {pos: len(rs) for pos, rs in pos_rounds.items()}
                ds["opening_sequence"] = [p.get("pos") for p in picks[:5]]

        # ---- transactions ----
        if not completed:
            continue
        tx = s.get("transactions") or []

        # group claims by (week, player) to find contested prices
        claims_by_target = defaultdict(list)

        for t in tx:
            uid = t.get("creator")
            if not uid and t.get("roster_ids"):
                uid = roster_owner.get(t["roster_ids"][0])
            if not uid:
                continue
            m = manager(uid, season)
            fs = m["faab"]["seasons"].setdefault(
                season,
                {
                    "won": [],
                    "lost": [],
                    "free_agent_adds": 0,
                    "trades": 0,
                    "spent": 0,
                },
            )
            ttype = t.get("type")
            if ttype == "trade":
                fs["trades"] += 1
                continue
            if ttype == "free_agent":
                fs["free_agent_adds"] += 1
                continue
            if ttype != "waiver":
                continue
            adds = list((t.get("adds") or {}).keys())
            pid = adds[0] if adds else None
            pinfo = players.get(pid) or {}
            row = {
                "week": t.get("week"),
                "bid": t.get("bid"),
                "player": pinfo.get("name") or pid,
                "pos": pinfo.get("pos"),
                "status": t.get("status"),
            }
            if pid is not None and t.get("bid") is not None:
                claims_by_target[(t.get("week"), pid)].append(
                    {"uid": uid, "bid": t["bid"], "status": t.get("status")}
                )
            if t.get("status") == "complete":
                fs["won"].append(row)
                fs["spent"] += t.get("bid") or 0
                if row["pos"] and t.get("bid") is not None:
                    league_price_rows.append(
                        {
                            "season": season,
                            "week": t.get("week"),
                            "bucket": week_bucket(t.get("week") or 0),
                            "pos": row["pos"],
                            "bid": t["bid"],
                            "player": row["player"],
                        }
                    )
            elif t.get("status") == "failed":
                fs["lost"].append(row)

        for (week, pid), bids in claims_by_target.items():
            if len(bids) < 2:
                continue
            bids.sort(key=lambda b: b["bid"], reverse=True)
            winner = next((b for b in bids if b["status"] == "complete"), None)
            if not winner:
                continue
            runner_up = max(
                (b["bid"] for b in bids if b is not winner), default=None
            )
            pinfo = players.get(pid) or {}
            contested.append(
                {
                    "season": season,
                    "week": week,
                    "player": pinfo.get("name") or pid,
                    "pos": pinfo.get("pos"),
                    "winning_bid": winner["bid"],
                    "second_bid": runner_up,
                    "margin": winner["bid"] - (runner_up or 0),
                    "n_bidders": len(bids),
                }
            )

    # ---- roll up per-manager summaries ----
    for uid, m in managers.items():
        draft_seasons = m["draft"]["seasons"]
        first_qb = [
            ds["first_round_by_pos"].get("QB")
            for ds in draft_seasons.values()
            if ds.get("first_round_by_pos", {}).get("QB")
        ]
        first_te = [
            ds["first_round_by_pos"].get("TE")
            for ds in draft_seasons.values()
            if ds.get("first_round_by_pos", {}).get("TE")
        ]
        m["draft"]["summary"] = {
            "first_qb_rounds": first_qb,
            "first_te_rounds": first_te,
            "opening_sequences": {
                season: ds.get("opening_sequence")
                for season, ds in draft_seasons.items()
                if ds.get("opening_sequence")
            },
        }

        all_won, all_lost = [], []
        burn = {}
        for season, fs in m["faab"]["seasons"].items():
            all_won.extend(fs["won"])
            all_lost.extend(fs["lost"])
            cum, curve = 0, {}
            for w in sorted(fs["won"], key=lambda r: r["week"] or 0):
                cum += w["bid"] or 0
                curve[str(w["week"])] = cum
            burn[season] = curve
        bids_won = [r["bid"] for r in all_won if r["bid"] is not None]
        bids_lost = [r["bid"] for r in all_lost if r["bid"] is not None]
        m["faab"]["summary"] = {
            "claims_won": len(all_won),
            "claims_lost": len(all_lost),
            "total_spent_by_season": {
                season: fs["spent"] for season, fs in m["faab"]["seasons"].items()
            },
            "avg_winning_bid": round(sum(bids_won) / len(bids_won), 1)
            if bids_won
            else None,
            "max_winning_bid": max(bids_won) if bids_won else None,
            "median_winning_bid": median(bids_won),
            "avg_losing_bid": round(sum(bids_lost) / len(bids_lost), 1)
            if bids_lost
            else None,
            "max_losing_bid": max(bids_lost) if bids_lost else None,
            "burn_curve": burn,
            "spend_by_bucket": _bucketize(all_won),
            "top_buys": sorted(
                all_won, key=lambda r: r["bid"] or 0, reverse=True
            )[:5],
        }

    price_book = defaultdict(list)
    for row in league_price_rows:
        price_book[(row["pos"], row["bucket"])].append(row["bid"])
    price_book_out = {
        "{}|{}".format(pos, bucket): {
            "n": len(bids),
            "median": median(bids),
            "max": max(bids),
        }
        for (pos, bucket), bids in price_book.items()
    }

    return {
        "generated": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "slug": history["slug"],
        "league_name": seasons[-1]["name"] if seasons else None,
        "seasons": [
            {
                "season": s["season"],
                "num_teams": s["num_teams"],
                "status": s["status"],
                "waiver_budget": s.get("waiver_budget"),
            }
            for s in seasons
        ],
        "managers": managers,
        "league": {
            "price_book": price_book_out,
            "contested_claims": sorted(
                contested, key=lambda c: c["winning_bid"], reverse=True
            ),
        },
    }


def _bucketize(won_rows):
    out = {"early": 0, "mid": 0, "late": 0}
    for r in won_rows:
        out[week_bucket(r["week"] or 0)] += r["bid"] or 0
    return out


def main():
    parser = argparse.ArgumentParser(description="Analyze harvested league history.")
    parser.add_argument("--slug", required=True)
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    in_path = os.path.join(root, "data", "league-history", args.slug + ".json")
    out_path = os.path.join(root, "data", "analysis", args.slug + ".json")

    with open(in_path) as f:
        history = json.load(f)

    result = analyze(history)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)
        f.write("\n")

    print(
        "wrote {} ({} managers, {} contested claims)".format(
            out_path,
            len(result["managers"]),
            len(result["league"]["contested_claims"]),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
