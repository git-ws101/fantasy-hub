#!/usr/bin/env python3
"""Mock of the cross-venue shadow pipeline: read Polymarket flow, trade on Kalshi.

Runs the full cycle on synthetic data so the loop is testable end to end with
zero API keys and zero dependencies. Each stage is timed, and alongside the
measured mock time we project what the stage costs against live APIs
(call counts x typical latency + rate-limit sleeps).

Stages:
  1. load market map        (Polymarket id <-> Kalshi ticker, curated by hand)
  2. fetch Polymarket trades (last 24h fills per mapped market, wallet-attributed)
  3. fetch Kalshi prices     (current yes-price per mapped ticker)
  4. update wallet scores    (track records from resolved-market history)
  5. compute flow signals    (score-weighted net flow per market)
  6. build proposals         (edge filter + quarter-Kelly sizing, capped)

Usage: python3 mock_pipeline.py [bankroll_usd]
"""

import json
import math
import random
import time
from pathlib import Path

random.seed(42)

BANKROLL_DEFAULT = 2_000.0
SHADOW_CAP_PCT = 0.02        # max 2% of bankroll per shadow-bucket trade
KELLY_FRACTION = 0.25        # quarter Kelly
MIN_EDGE = 0.04              # skip if Kalshi already within 4c of PM price
MIN_SIGNAL_Z = 1.5           # flow anomaly threshold
MIN_WALLET_RESOLVED = 8      # ignore wallets with thin track records

# Live-API cost model (per stage projections, seconds)
API_LATENCY = 0.20           # typical REST round trip
PM_TRADE_PAGES = 3           # pages of fills per market per 24h window
PM_RATE_SLEEP = 0.10         # polite spacing between Polymarket calls
KALSHI_BATCH = 20            # tickers per Kalshi markets call
WALLETS_TRACKED = 400        # wallets with positions in mapped markets
WALLET_CALLS_PER = 1         # incremental position refresh per wallet
COLD_START_RESOLVED_MARKETS = 3000   # 90-day backfill universe
COLD_START_CALLS_PER_MARKET = 4      # trades + positions pagination

TIMINGS = []  # (stage, measured_s, projected_live_s)


def timed(stage, projected):
    def wrap(fn):
        def inner(*a, **kw):
            t0 = time.perf_counter()
            out = fn(*a, **kw)
            TIMINGS.append((stage, time.perf_counter() - t0, projected))
            return out
        return inner
    return wrap


# ---------------------------------------------------------------- stage 1
@timed("1. load market map", projected=0.0)
def load_market_map():
    raw = json.loads((Path(__file__).parent / "market_map.json").read_text())
    return [p for p in raw["pairs"] if p["resolution_equivalent"]]


# ---------------------------------------------------------------- stage 2
def _synth_wallets(n=WALLETS_TRACKED):
    """Synthetic wallet universe: mostly noise, a few sharps."""
    wallets = {}
    for i in range(n):
        addr = f"0x{i:040x}"
        sharp = i < 12  # ~3% of wallets are genuinely sharp
        resolved = random.randint(3, 60) if not sharp else random.randint(15, 80)
        winrate = random.betavariate(8, 3) if sharp else random.betavariate(5, 5)
        wins = int(round(resolved * winrate))
        wallets[addr] = {"resolved": resolved, "wins": wins, "sharp": sharp}
    return wallets


WALLETS = _synth_wallets()

_pm_trades_projected = 0.0  # filled in below


@timed("2. fetch Polymarket trades (24h)",
       projected=len(json.loads((Path(__file__).parent / "market_map.json")
                     .read_text())["pairs"]) * PM_TRADE_PAGES * (API_LATENCY + PM_RATE_SLEEP))
def fetch_polymarket_trades(pairs):
    """Synthetic 24h fill tape per mapped market. Live: CLOB /trades, paginated."""
    addrs = list(WALLETS)
    tape = {}
    for p in pairs:
        n_fills = random.randint(40, 400)
        fills = []
        # plant an informed-flow anomaly in two markets
        anomalous = p["pm_id"] in ("pm-emmys-drama", "pm-gov-shutdown-oct")
        for _ in range(n_fills):
            if anomalous and random.random() < 0.25:
                w = addrs[random.randint(0, 11)]          # sharp wallet
                side, usd = "buy_yes", random.uniform(500, 5000)
            else:
                w = random.choice(addrs)
                side = random.choice(["buy_yes", "buy_no"])
                usd = random.expovariate(1 / 120)
            fills.append({"wallet": w, "side": side, "usd": usd})
        tape[p["pm_id"]] = fills
    return tape


# ---------------------------------------------------------------- stage 3
@timed("3. fetch Kalshi prices",
       projected=math.ceil(8 / KALSHI_BATCH) * API_LATENCY + API_LATENCY)
def fetch_kalshi_prices(pairs):
    """Synthetic current yes-prices. Live: GET /markets?tickers=..., batched."""
    prices = {}
    for p in pairs:
        base = random.uniform(0.15, 0.75)
        prices[p["kalshi_ticker"]] = round(base, 2)
    return prices


def fetch_polymarket_prices(pairs, tape):
    """PM price = base drifted by the planted flow (informed money moved it)."""
    prices = {}
    for p in pairs:
        net = sum(f["usd"] * (1 if f["side"] == "buy_yes" else -1)
                  for f in tape[p["pm_id"]])
        drift = max(-0.10, min(0.10, net / 300_000))
        prices[p["pm_id"]] = round(min(0.97, max(0.03, 0.45 + drift + random.uniform(-0.05, 0.05))), 2)
    return prices


# ---------------------------------------------------------------- stage 4
def wilson_lower(wins, n, z=1.96):
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (centre - margin) / denom


@timed("4. update wallet scores (incremental)",
       projected=WALLETS_TRACKED * WALLET_CALLS_PER * (API_LATENCY + PM_RATE_SLEEP))
def update_wallet_scores():
    """Score = Wilson lower bound on resolved-market win rate.
    Live: refresh positions for wallets seen in today's tape; full history
    already cached locally from the one-time cold-start backfill."""
    scores = {}
    for addr, w in WALLETS.items():
        if w["resolved"] < MIN_WALLET_RESOLVED:
            scores[addr] = 0.0
            continue
        # center on 0.5: only above-coin-flip track records add signal weight
        scores[addr] = max(0.0, wilson_lower(w["wins"], w["resolved"]) - 0.5) * 2
    return scores


# ---------------------------------------------------------------- stage 5
@timed("5. compute flow signals", projected=2.0)
def compute_signals(pairs, tape, scores):
    signals = []
    for p in pairs:
        fills = tape[p["pm_id"]]
        weighted = sum(f["usd"] * scores.get(f["wallet"], 0.0)
                       * (1 if f["side"] == "buy_yes" else -1) for f in fills)
        raw = sum(f["usd"] for f in fills)
        # z-proxy: weighted net flow vs sqrt(total volume) baseline noise
        z = weighted / (math.sqrt(max(raw, 1)) * 3.0)
        signals.append({"pair": p, "z": z,
                        "side": "yes" if z > 0 else "no",
                        "sharp_usd": abs(weighted)})
    return signals


# ---------------------------------------------------------------- stage 6
@timed("6. build proposals", projected=0.5)
def build_proposals(signals, pm_prices, k_prices, bankroll):
    proposals = []
    for s in signals:
        p = s["pair"]
        pm, k = pm_prices[p["pm_id"]], k_prices[p["kalshi_ticker"]]
        edge = (pm - k) if s["side"] == "yes" else (k - pm)
        s.update({"pm": pm, "kalshi": k, "edge": round(edge, 2),
                  "ticker": p["kalshi_ticker"], "event": p["event"]})
        if abs(s["z"]) < MIN_SIGNAL_Z:
            s["status"] = "no_signal"
            continue
        if edge < MIN_EDGE:
            s["status"] = "already_priced"  # late, skip, don't chase
            continue
        entry = k if s["side"] == "yes" else 1 - k
        b = (1 - entry) / entry                      # net odds on a $1 contract
        p_est = pm if s["side"] == "yes" else 1 - pm  # PM price as prob estimate
        kelly = max(0.0, (p_est * (b + 1) - 1) / b) * KELLY_FRACTION
        stake = round(min(kelly * bankroll, SHADOW_CAP_PCT * bankroll), 2)
        if stake < 5:
            s["status"] = "stake_too_small"
            continue
        s["status"] = "proposed"
        proposals.append({"event": p["event"], "ticker": p["kalshi_ticker"],
                          "side": s["side"], "z": round(s["z"], 1),
                          "sharp_usd": round(s["sharp_usd"]),
                          "pm": pm, "kalshi": k, "edge": round(edge, 2),
                          "stake": stake})
    return sorted(proposals, key=lambda x: -x["edge"])


# ---------------------------------------------------------------- report
def fmt_secs(s):
    return f"{s:.1f}s" if s < 90 else f"{s/60:.1f}m"


def main(bankroll=BANKROLL_DEFAULT):
    pairs = load_market_map()
    tape = fetch_polymarket_trades(pairs)
    k_prices = fetch_kalshi_prices(pairs)
    pm_prices = fetch_polymarket_prices(pairs, tape)
    scores = update_wallet_scores()
    signals = compute_signals(pairs, tape, scores)
    proposals = build_proposals(signals, pm_prices, k_prices, bankroll)

    print(f"\n=== TRADE PROPOSALS (bankroll ${bankroll:,.0f}, shadow cap "
          f"{SHADOW_CAP_PCT:.0%}/trade) ===")
    if not proposals:
        print("  none - no flow anomaly cleared both signal and edge filters")
    for pr in proposals:
        print(f"  {pr['ticker']:<18} {pr['side'].upper():<4} ${pr['stake']:>6.2f}"
              f"  | z={pr['z']:>4}  sharp ${pr['sharp_usd']:,} "
              f" | PM {pr['pm']:.2f} vs K {pr['kalshi']:.2f} (edge {pr['edge']:.2f})"
              f"  | {pr['event']}")

    print("\n=== RUNTIME: this mock vs projected live APIs ===")
    total_m = total_p = 0.0
    for stage, m, p in TIMINGS:
        total_m += m; total_p += p
        print(f"  {stage:<38} mock {m*1000:>6.1f}ms   live ~{fmt_secs(p):>6}")
    print(f"  {'TOTAL per cycle':<38} mock {total_m*1000:>6.1f}ms   live ~{fmt_secs(total_p):>6}")

    cold = COLD_START_RESOLVED_MARKETS * COLD_START_CALLS_PER_MARKET * (API_LATENCY + PM_RATE_SLEEP)
    print(f"\n  one-time cold start (90d wallet-history backfill, REST): ~{cold/3600:.1f}h")
    print(f"  same backfill via subgraph/bulk export:                  ~20-40m")

    out = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "params": {"bankroll": bankroll, "shadow_cap_pct": SHADOW_CAP_PCT,
                   "kelly_fraction": KELLY_FRACTION, "min_edge": MIN_EDGE,
                   "min_signal_z": MIN_SIGNAL_Z},
        "proposals": proposals,
        "signals": [{"ticker": s["ticker"], "event": s["event"],
                     "side": s["side"], "z": round(s["z"], 1),
                     "sharp_usd": round(s["sharp_usd"]),
                     "pm": s["pm"], "kalshi": s["kalshi"], "edge": s["edge"],
                     "status": s["status"]} for s in signals],
        "timings": [{"stage": st, "mock_ms": round(m * 1000, 1),
                     "live_s": round(p, 1)} for st, m, p in TIMINGS],
        "cold_start": {"rest_hours": round(cold / 3600, 1),
                       "bulk_minutes": "20-40"},
    }
    out_path = Path(__file__).parent / "data" / "proposals.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, indent=1))
    print(f"\n  wrote {out_path}")


if __name__ == "__main__":
    import sys
    main(float(sys.argv[1]) if len(sys.argv) > 1 else BANKROLL_DEFAULT)
