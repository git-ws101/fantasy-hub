#!/usr/bin/env python3
"""Look up a Sleeper user's NFL leagues across recent seasons.

Stdlib only. Writes data/league-lookup.json with the user object and
per-season league summaries. Read-only against the Sleeper API.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.sleeper.app/v1"


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "hub-league-lookup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def summarize_league(lg):
    settings = lg.get("settings") or {}
    return {
        "league_id": lg.get("league_id"),
        "name": lg.get("name"),
        "season": lg.get("season"),
        "status": lg.get("status"),
        "total_rosters": lg.get("total_rosters"),
        "previous_league_id": lg.get("previous_league_id"),
        "draft_id": lg.get("draft_id"),
        "waiver_type": settings.get("waiver_type"),
        "waiver_budget": settings.get("waiver_budget"),
        "playoff_teams": settings.get("playoff_teams"),
        "num_teams": settings.get("num_teams"),
        "scoring_rec": (lg.get("scoring_settings") or {}).get("rec"),
    }


def main():
    parser = argparse.ArgumentParser(description="List a Sleeper user's NFL leagues.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--seasons", default="2026,2025,2024")
    parser.add_argument("--out", default=None, help="Output path (default: data/league-lookup.json)")
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = args.out or os.path.join(root, "data", "league-lookup.json")

    user = get("/user/" + urllib.parse.quote(args.username))
    if not user or not user.get("user_id"):
        print("user not found: " + args.username, file=sys.stderr)
        return 1

    seasons = {}
    for season in [s.strip() for s in args.seasons.split(",") if s.strip()]:
        leagues = get("/user/{}/leagues/nfl/{}".format(user["user_id"], season)) or []
        seasons[season] = [summarize_league(lg) for lg in leagues]

    payload = {
        "fetched": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "user": {
            "user_id": user.get("user_id"),
            "username": user.get("username"),
            "display_name": user.get("display_name"),
        },
        "seasons": seasons,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")

    total = sum(len(v) for v in seasons.values())
    print("wrote {} ({} leagues across {} seasons)".format(out_path, total, len(seasons)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
