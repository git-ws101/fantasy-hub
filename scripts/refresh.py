#!/usr/bin/env python3
"""Refresh data/players.json from Sleeper 2026 season projections.

Stdlib only. On any failure, players.json is left untouched and
status.json records the error; exit code is always 0 so the workflow
can still commit the status file.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SEASON = "2026"
BASE_URL = "https://api.sleeper.com/projections/nfl/" + SEASON

# Positions and how many players to keep per position (by projected points).
POSITION_LIMITS = {"QB": 40, "RB": 100, "WR": 130, "TE": 65}

SOURCE = "Sleeper API 2026 season projections, half PPR ADP"

STAT_KEYS = (
    "pass_yd",
    "pass_td",
    "pass_int",
    "rush_yd",
    "rush_td",
    "rec",
    "rec_yd",
    "rec_td",
)


def fetch_position(pos):
    params = {
        "season_type": "regular",
        "position[]": pos,
        "order_by": "adp_half_ppr",
    }
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "hub-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def half_ppr_points(s):
    return (
        0.04 * s["pass_yd"]
        + 4 * s["pass_td"]
        - 2 * s["pass_int"]
        + 0.1 * s["rush_yd"]
        + 6 * s["rush_td"]
        + 0.5 * s["rec"]
        + 0.1 * s["rec_yd"]
        + 6 * s["rec_td"]
        - 2 * s["fum"]
    )


def extract_players(rows, pos):
    out = []
    for row in rows:
        stats = row.get("stats") or {}
        player = row.get("player") or {}
        team = row.get("team") or player.get("team")
        if stats.get("pts_half_ppr") is None or not team:
            continue

        s = {k: float(stats.get(k) or 0) for k in STAT_KEYS}
        s["fum"] = float(stats.get("fum_lost") or 0)

        adp = stats.get("adp_half_ppr")
        if adp is None or float(adp) >= 999:
            adp = None
        else:
            adp = float(adp)

        name = "{} {}".format(
            player.get("first_name") or "", player.get("last_name") or ""
        ).strip()
        if not name:
            continue

        out.append(
            {
                "name": name,
                "team": team,
                "pos": pos,
                "stats": s,
                "adp": adp,
                "_pts": half_ppr_points(s),
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Refresh data/players.json from the Sleeper API."
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repo root (default: parent of this script's directory)",
    )
    args = parser.parse_args()

    root = args.root or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    data_dir = os.path.join(root, "data")
    players_path = os.path.join(data_dir, "players.json")
    status_path = os.path.join(data_dir, "status.json")

    now = datetime.now(timezone.utc)

    try:
        players = []
        for pos, limit in POSITION_LIMITS.items():
            rows = fetch_position(pos)
            extracted = extract_players(rows, pos)
            extracted.sort(key=lambda p: p["_pts"], reverse=True)
            players.extend(extracted[:limit])

        if not players:
            raise RuntimeError("Sleeper API returned no usable players")

        for p in players:
            del p["_pts"]

        os.makedirs(data_dir, exist_ok=True)
        payload = {
            "updated": now.date().isoformat(),
            "source": SOURCE,
            "players": players,
        }
        tmp = players_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=1)
            f.write("\n")
        os.replace(tmp, players_path)

        status = {
            "last_run": now.isoformat().replace("+00:00", "Z"),
            "status": "ok",
            "detail": "Refreshed {} players from Sleeper".format(len(players)),
            "players": len(players),
        }
    except Exception as exc:  # noqa: BLE001 — record any failure in status
        count = None
        try:
            with open(players_path) as f:
                count = len(json.load(f).get("players", []))
        except Exception:
            pass
        status = {
            "last_run": now.isoformat().replace("+00:00", "Z"),
            "status": "error",
            "detail": "{}: {}".format(type(exc).__name__, exc)[:200],
            "players": count,
        }
        print("refresh failed: {}".format(exc), file=sys.stderr)

    os.makedirs(data_dir, exist_ok=True)
    with open(status_path, "w") as f:
        json.dump(status, f, indent=1)
        f.write("\n")

    print("status: {} ({} players)".format(status["status"], status["players"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
