#!/usr/bin/env python3
"""Refresh data/league.json — manager analysis for Blitz: The Fantasy League.

Pulls the league, its members, and the previous season's rosters and
transactions from the public Sleeper API, then computes a per-manager
profile: record, points, FAAB bidding behavior, and activity style tags.

Stdlib only. Follows the same failure contract as refresh.py: on any
error the existing league.json is left untouched (or a status-only file
is written if none exists) and the exit code stays 0.

The league is found from the emailed invite link. Once resolved, the
league_id is cached inside league.json and reused on later runs.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

INVITE_CODE = "0NxbMkOLBPbaA"
LEAGUE_NAME_HINT = "Blitz"
API = "https://api.sleeper.app/v1"
WEEKS = range(1, 19)
EARLY_WEEKS = {1, 2, 3, 4}


def get(url, timeout=60, as_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": "hub-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if as_json else body.decode("utf-8", "replace")


def resolve_league_id(cached):
    if cached:
        return cached, "cached"
    # Strategy 1: invite page HTML embeds the league id for link previews.
    try:
        html = get("https://sleeper.com/i/" + INVITE_CODE, as_json=False)
        ids = re.findall(r'league[s_id"\':/-]{0,10}?(\d{15,20})', html, re.I)
        if ids:
            return ids[0], "invite page"
    except Exception as exc:
        print("invite page failed: {}".format(exc), file=sys.stderr)
    # Strategy 2: undocumented invite endpoint.
    try:
        data = get(API + "/invite/" + INVITE_CODE)
        if isinstance(data, dict) and data.get("league_id"):
            return data["league_id"], "invite api"
    except Exception as exc:
        print("invite api failed: {}".format(exc), file=sys.stderr)
    raise RuntimeError("could not resolve league id from invite " + INVITE_CODE)


def faab_profile(transactions, roster_to_user):
    """Per-user FAAB and activity stats from one season of transactions."""
    prof = {}

    def u(tx):
        rid = (tx.get("roster_ids") or [None])[0]
        return roster_to_user.get(rid)

    for tx in transactions:
        uid = u(tx)
        if uid is None or tx.get("status") != "complete":
            continue
        p = prof.setdefault(uid, {
            "faab_spent": 0, "bids": [], "early_spent": 0,
            "adds": 0, "drops": 0, "trades": 0, "top_bids": [],
        })
        ttype = tx.get("type")
        if ttype == "trade":
            p["trades"] += 1
            continue
        if tx.get("adds"):
            p["adds"] += len(tx["adds"])
        if tx.get("drops"):
            p["drops"] += len(tx["drops"])
        if ttype == "waiver":
            bid = int((tx.get("settings") or {}).get("waiver_bid") or 0)
            if bid > 0:
                p["faab_spent"] += bid
                p["bids"].append(bid)
                if tx.get("leg") in EARLY_WEEKS:
                    p["early_spent"] += bid
                for pid in (tx.get("adds") or {}):
                    p["top_bids"].append({"player_id": pid, "bid": bid,
                                          "week": tx.get("leg")})
    for p in prof.values():
        p["top_bids"] = sorted(p["top_bids"], key=lambda b: -b["bid"])[:3]
    return prof


def style_tags(p, budget, wins, losses):
    tags = []
    spent, bids = p["faab_spent"], p["bids"]
    if budget and spent >= 0.7 * budget:
        tags.append("big FAAB spender")
    elif budget and spent <= 0.25 * budget:
        tags.append("FAAB hoarder")
    if spent and p["early_spent"] >= 0.5 * spent:
        tags.append("spends early")
    if bids and max(bids) >= 0.4 * (budget or 100):
        tags.append("haymaker bids")
    if p["trades"] >= 3:
        tags.append("active trader")
    elif p["trades"] == 0:
        tags.append("never trades")
    if p["adds"] >= 30:
        tags.append("churns the wire")
    elif p["adds"] < 8:
        tags.append("set-and-forget")
    if wins + losses > 0 and wins / (wins + losses) >= 0.6:
        tags.append("contender")
    return tags


def player_names(pids):
    """Resolve a small set of player ids to names via the full player dump."""
    if not pids:
        return {}
    try:
        allp = get(API + "/players/nfl", timeout=120)
        return {pid: (allp.get(pid) or {}).get("full_name")
                or (allp.get(pid) or {}).get("last_name") or pid
                for pid in pids}
    except Exception as exc:
        print("player names failed: {}".format(exc), file=sys.stderr)
        return {pid: pid for pid in pids}


def build():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(root, "data", "league.json")

    cached = None
    try:
        with open(out_path) as f:
            cached = json.load(f).get("league_id")
    except Exception:
        pass

    league_id, how = resolve_league_id(cached)
    league = get(API + "/league/" + str(league_id))
    if LEAGUE_NAME_HINT.lower() not in (league.get("name") or "").lower():
        print("warning: league name is {!r}".format(league.get("name")),
              file=sys.stderr)

    users = get(API + "/league/" + str(league_id) + "/users")
    members = {u["user_id"]: u for u in users}

    prev_id = league.get("previous_league_id")
    prev = prev_users = prev_rosters = None
    prof, records, budget = {}, {}, None
    if prev_id:
        prev = get(API + "/league/" + str(prev_id))
        prev_users = get(API + "/league/" + str(prev_id) + "/users")
        prev_rosters = get(API + "/league/" + str(prev_id) + "/rosters")
        budget = (prev.get("settings") or {}).get("waiver_budget") or 100
        roster_to_user = {r["roster_id"]: r.get("owner_id")
                          for r in prev_rosters}
        for r in prev_rosters:
            s = r.get("settings") or {}
            records[r.get("owner_id")] = {
                "wins": s.get("wins", 0), "losses": s.get("losses", 0),
                "ties": s.get("ties", 0),
                "pf": round(s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100.0, 1),
                "pa": round(s.get("fpts_against", 0)
                            + s.get("fpts_against_decimal", 0) / 100.0, 1),
            }
        txs = []
        for wk in WEEKS:
            try:
                txs.extend(get("{}/league/{}/transactions/{}".format(
                    API, prev_id, wk)))
            except Exception:
                break
        prof = faab_profile(txs, roster_to_user)

    need_names = {b["player_id"] for p in prof.values() for b in p["top_bids"]}
    names = player_names(need_names)

    prev_members = {u["user_id"]: u for u in (prev_users or [])}
    all_ids = set(members) | set(prev_members)
    managers = []
    for uid in all_ids:
        cur, old = members.get(uid), prev_members.get(uid)
        src = cur or old
        rec = records.get(uid) or {}
        p = prof.get(uid) or {"faab_spent": 0, "bids": [], "early_spent": 0,
                              "adds": 0, "drops": 0, "trades": 0, "top_bids": []}
        managers.append({
            "user_id": uid,
            "name": src.get("display_name") or "unknown",
            "team_name": (src.get("metadata") or {}).get("team_name"),
            "avatar": src.get("avatar"),
            "in_current": cur is not None,
            "in_previous": old is not None,
            "record": rec,
            "faab": {
                "budget": budget,
                "spent": p["faab_spent"],
                "bid_count": len(p["bids"]),
                "max_bid": max(p["bids"]) if p["bids"] else 0,
                "avg_bid": round(sum(p["bids"]) / len(p["bids"]), 1)
                if p["bids"] else 0,
                "early_share": round(p["early_spent"] / p["faab_spent"], 2)
                if p["faab_spent"] else 0,
                "top_bids": [{"player": names.get(b["player_id"], b["player_id"]),
                              "bid": b["bid"], "week": b["week"]}
                             for b in p["top_bids"]],
            },
            "activity": {"adds": p["adds"], "drops": p["drops"],
                         "trades": p["trades"]},
            "tags": style_tags(p, budget, rec.get("wins", 0),
                               rec.get("losses", 0)),
        })
    managers.sort(key=lambda m: (-int(m["in_current"]),
                                 -(m["record"].get("wins", 0)),
                                 -(m["record"].get("pf", 0))))
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "status": "ok",
        "league_id": str(league_id),
        "league_name": league.get("name"),
        "season": league.get("season"),
        "teams": (league.get("settings") or {}).get("num_teams"),
        "resolved_via": how,
        "previous_season": prev.get("season") if prev else None,
        "faab_budget": budget,
        "managers": managers,
    }, out_path


def main():
    try:
        payload, out_path = build()
    except Exception as exc:  # noqa: BLE001 — record any failure, keep old data
        print("league refresh failed: {}".format(exc), file=sys.stderr)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out_path = os.path.join(root, "data", "league.json")
        if os.path.exists(out_path):
            print("keeping existing league.json")
            return 0
        payload = {
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "status": "error",
            "detail": "{}: {}".format(type(exc).__name__, exc)[:200],
            "managers": [],
        }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")
    os.replace(tmp, out_path)
    print("league status: {} ({} managers)".format(
        payload["status"], len(payload.get("managers", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
