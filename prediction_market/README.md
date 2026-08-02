# Cross-venue shadow pipeline — runnable mock

Reads Polymarket's public wallet-attributed trade flow, scores wallets by
track record, and proposes trades executed on Kalshi. This mock runs the
full cycle on synthetic data (no API keys, no dependencies, stdlib only)
and reports both measured mock time and projected live-API time per stage.

```
python3 mock_pipeline.py [bankroll_usd]
```

## Stages

1. **Market map** — hand-curated Polymarket-id ↔ Kalshi-ticker pairs with a
   `resolution_equivalent` flag; non-equivalent pairs are excluded up front.
2. **Polymarket trades** — last-24h fills per mapped market, wallet-attributed
   (live: public CLOB `/trades`, paginated; no account needed to read).
3. **Kalshi prices** — current yes-price per mapped ticker (live: batched
   `/markets` call).
4. **Wallet scores** — Wilson lower bound of each wallet's resolved-market win
   rate, centered so only above-coin-flip records add weight; thin records
   (< 8 resolved) score zero.
5. **Flow signals** — score-weighted net flow per market, normalized to a
   z-proxy against volume noise.
6. **Proposals** — require signal z ≥ 1.5 **and** ≥ 4c of edge remaining vs
   Kalshi (skip if Kalshi already caught up — never chase), then quarter-Kelly
   sizing capped at 2% of bankroll per shadow-bucket trade. Output is a ranked
   proposal list for human approval; nothing auto-executes.

## Runtime (from the projection model in the script)

- **Steady-state cycle:** ~2 minutes against live APIs (dominated by
  incremental wallet refreshes and rate-limit spacing). The mock itself
  runs in ~3ms.
- **One-time cold start:** backfilling ~90 days of resolved-market wallet
  history is ~1h via polite REST paging, or ~20–40m via subgraph/bulk export.

Scaling note: runtime grows roughly linearly with mapped markets and tracked
wallets; at ~50 mapped pairs and ~2,000 wallets a cycle is still ~10m, fine
for an hourly schedule.
