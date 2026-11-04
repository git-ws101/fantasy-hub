#!/usr/bin/env python3
"""Snapshot current NFL state and trending waiver adds from Sleeper.

Writes data/week-snapshot.json with the current week and the top trending
adds (player id, name, pos, team, add count). Stdlib only; read-only.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.sleeper.app/v1"


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "hub-week-snapshot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    time.sleep(0.15)
    return data


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(root, "data", "week-snapshot.json")

    state = get("/state/nfl")
    trending = get("/players/nfl/trending/add?lookback_hours=24&limit=50") or []
    all_players = get("/players/nfl") or {}

    rows = []
    for t in trending:
        pid = t.get("player_id")
        p = all_players.get(pid) or {}
        rows.append(
            {
                "player_id": pid,
                "name": p.get("full_name")
                or "{} {}".format(p.get("first_name") or "", p.get("last_name") or "").strip()
                or pid,
                "pos": p.get("position"),
                "team": p.get("team"),
                "injury_status": p.get("injury_status"),
                "adds_24h": t.get("count"),
            }
        )

    payload = {
        "fetched": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "season": state.get("season"),
        "week": state.get("week"),
        "season_type": state.get("season_type"),
        "trending_adds": rows,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")

    print("wrote {} (season {} week {}, {} trending)".format(
        out_path, payload["season"], payload["week"], len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
