#!/usr/bin/env python3
"""Harvest a Sleeper league's full multi-season history.

Walks the previous_league_id chain from the given league, pulling per
season: league settings, users, rosters (with records), draft + picks,
and all transactions (including failed FAAB claims, which carry bids).
Player ids referenced anywhere are resolved to names via the players
endpoint. Stdlib only; read-only against the Sleeper API.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.sleeper.app/v1"
MAX_WEEKS = 18
REQUEST_GAP = 0.15  # polite spacing between calls


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "hub-league-history/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    time.sleep(REQUEST_GAP)
    return data


def trim_pick(p):
    md = p.get("metadata") or {}
    return {
        "pick_no": p.get("pick_no"),
        "round": p.get("round"),
        "draft_slot": p.get("draft_slot"),
        "roster_id": p.get("roster_id"),
        "picked_by": p.get("picked_by"),
        "player_id": p.get("player_id"),
        "player": "{} {}".format(md.get("first_name") or "", md.get("last_name") or "").strip(),
        "pos": md.get("position"),
        "team": md.get("team"),
        "is_keeper": p.get("is_keeper"),
    }


def trim_transaction(t):
    settings = t.get("settings") or {}
    return {
        "id": t.get("transaction_id"),
        "type": t.get("type"),
        "status": t.get("status"),
        "week": t.get("leg"),
        "creator": t.get("creator"),
        "roster_ids": t.get("roster_ids"),
        "adds": t.get("adds"),
        "drops": t.get("drops"),
        "bid": settings.get("waiver_bid"),
        "seq": settings.get("seq"),
        "created": t.get("created"),
    }


def fetch_season(league):
    league_id = league["league_id"]
    users = get("/league/{}/users".format(league_id)) or []
    rosters = get("/league/{}/rosters".format(league_id)) or []
    drafts = get("/league/{}/drafts".format(league_id)) or []

    draft_data = []
    for d in drafts:
        draft_id = d.get("draft_id")
        picks = get("/draft/{}/picks".format(draft_id)) or []
        draft_data.append(
            {
                "draft_id": draft_id,
                "type": d.get("type"),
                "status": d.get("status"),
                "rounds": (d.get("settings") or {}).get("rounds"),
                "order": d.get("draft_order"),
                "picks": [trim_pick(p) for p in picks],
            }
        )

    transactions = []
    for week in range(1, MAX_WEEKS + 1):
        rows = get("/league/{}/transactions/{}".format(league_id, week)) or []
        transactions.extend(trim_transaction(t) for t in rows)

    settings = league.get("settings") or {}
    return {
        "season": league.get("season"),
        "league_id": league_id,
        "name": league.get("name"),
        "status": league.get("status"),
        "num_teams": league.get("total_rosters"),
        "waiver_type": settings.get("waiver_type"),
        "waiver_budget": settings.get("waiver_budget"),
        "scoring_rec": (league.get("scoring_settings") or {}).get("rec"),
        "users": {
            u.get("user_id"): {
                "display_name": u.get("display_name"),
                "team_name": (u.get("metadata") or {}).get("team_name"),
            }
            for u in users
        },
        "rosters": [
            {
                "roster_id": r.get("roster_id"),
                "owner_id": r.get("owner_id"),
                "wins": (r.get("settings") or {}).get("wins"),
                "losses": (r.get("settings") or {}).get("losses"),
                "fpts": (r.get("settings") or {}).get("fpts"),
            }
            for r in rosters
        ],
        "drafts": draft_data,
        "transactions": transactions,
    }


def referenced_player_ids(seasons):
    ids = set()
    for s in seasons:
        for d in s["drafts"]:
            for p in d["picks"]:
                if p.get("player_id"):
                    ids.add(p["player_id"])
        for t in s["transactions"]:
            for bucket in ("adds", "drops"):
                for pid in (t.get(bucket) or {}):
                    ids.add(pid)
    return ids


def main():
    parser = argparse.ArgumentParser(description="Harvest a Sleeper league's history.")
    parser.add_argument("--league", required=True, help="Current (most recent) league_id")
    parser.add_argument("--slug", required=True, help="Short name used in the output filename")
    parser.add_argument("--max-seasons", type=int, default=10)
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(root, "data", "league-history", args.slug + ".json")

    seasons = []
    league_id = args.league
    while league_id and len(seasons) < args.max_seasons:
        league = get("/league/{}".format(league_id))
        if not league:
            break
        print("fetching {} {} ({})".format(league.get("season"), league.get("name"), league_id))
        seasons.append(fetch_season(league))
        league_id = league.get("previous_league_id")

    if not seasons:
        print("no league found for " + args.league, file=sys.stderr)
        return 1

    ids = referenced_player_ids(seasons)
    players_map = {}
    if ids:
        all_players = get("/players/nfl") or {}
        for pid in ids:
            p = all_players.get(pid) or {}
            players_map[pid] = {
                "name": p.get("full_name")
                or "{} {}".format(p.get("first_name") or "", p.get("last_name") or "").strip()
                or pid,
                "pos": p.get("position"),
                "team": p.get("team"),
            }

    payload = {
        "fetched": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "slug": args.slug,
        "root_league_id": args.league,
        "seasons": seasons,
        "players": players_map,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")

    n_tx = sum(len(s["transactions"]) for s in seasons)
    n_picks = sum(len(d["picks"]) for s in seasons for d in s["drafts"])
    print(
        "wrote {} ({} seasons, {} draft picks, {} transactions, {} players)".format(
            out_path, len(seasons), n_picks, n_tx, len(players_map)
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
