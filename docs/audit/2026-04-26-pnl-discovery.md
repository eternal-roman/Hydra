# Live P&L Discovery — 2026-04-26

> **Status:** Discovery only. Findings below explain the *likely* sources of the dashboard's `+$221.98` reading. Before any fix lands, these need to be reconciled against actual Kraken trade history (next step).

**Branch:** `fix/live-pnl-accounting-audit` (worktree at `C:/Users/elamj/Dev/Hydra-pnl-audit/`)
**Trigger:** User reported dashboard top-panel "P&L: +$221.98" looks wrong.

## Where the number comes from

Dashboard `App.jsx:4147`:

```jsx
const journalPnlUsd = state?.journal_stats?.total_pnl_usd ?? 0;
// …
<StatCard label="P&L" value={`${journalPnlUsd >= 0 ? "+$" : "-$"}${Math.abs(journalPnlUsd).toFixed(2)}`} … />
```

Path back to the agent: `journal_stats.total_pnl_usd` is computed in `hydra_agent.py:3581`:

```python
total_pnl_usd = total_realized_pnl_usd + total_unrealized_pnl_usd
```

Where:
- **realized** comes from `_compute_pair_realized_pnl(pair)` — walks `self.order_journal` for FILLED / PARTIALLY_FILLED entries, average-cost-basis accounting (`hydra_agent.py:3725-3770`).
- **unrealized** = `engine.position.size * (last_price - engine.position.avg_entry)` for any open position.
- both summed across all pairs, converted to USD via `_get_asset_prices()`.

## Findings (likely contributors to the discrepancy)

### F1 — Label says "P&L"; value includes both realized AND unrealized

`StatCard label="P&L"` shows `realized + unrealized` (mark-to-market). User's read of "P&L" was realized-only (closed positions). On its own this isn't a bug, just a labeling mismatch — the value isn't *wrong*, the user just expected a different definition. **Fix:** label should be "Net P&L" or split into two cards: "Realized" + "Open"; both numbers are already computed (`total_realized_pnl_usd`, `total_unrealized_pnl_usd` are present in the journal_stats payload).

### F2 — The order_journal is Hydra-only; user-placed Kraken trades are invisible

`_compute_pair_realized_pnl` only reads from `self.order_journal`, which is appended exclusively by Hydra's own placement loop. **Any manual trade the user executes via the Kraken UI is NOT in the journal**, so:

- **Buys** done by hand → no cost-basis entry → next Hydra sell computes "realized P&L" against an artificially low average cost (or zero) → realized P&L *overstates* gain.
- **Sells** done by hand → Hydra's view of inventory remains higher than reality → engine `position.size` is too high → `unrealized = size * (price - entry)` is computed against phantom inventory.

User explicitly asked: pull Kraken trade history and impute user-action trades into the journal "using the standard log note per the conventions used in the trade journal." This is a real gap — Hydra's accounting has been one-sided since shipping. Severity: **HIGH** if the user trades manually; LOW if they don't.

### F3 — Fees are not subtracted from realized P&L

`_compute_pair_realized_pnl` uses `avg_fill_price` (the gross fill from `lifecycle.avg_fill_price`). Kraken's maker fee at the user's tier is ~16 bps per side per CLAUDE.md (`maker_fee_bps=16` in `BacktestConfig`). A round-trip eats **~32 bps of notional** in fees that are NOT subtracted from realized.

Quick math: at 0.32% round-trip fee, **$69,000 of cumulative round-trip notional is enough to fictitiously inflate realized P&L by $221**. Hydra trades small amounts but trades often — across hundreds of round-trips this absolutely accumulates.

This is a real bug regardless of F2.

### F4 — Quote-currency conversion has a fragile fallback

`_get_asset_prices()` (`hydra_agent.py:3297-3320`) returns `{asset: usd_price}`. For the SOL/BTC pair, P&L is denominated in BTC, then multiplied by `prices["BTC"]` to convert to USD.

The fallback path: if "BTC" isn't already in `prices`, derive it from `prices["SOL"] / sol_per_btc`. But:

- If `engine.prices` is empty for BTC/USD (e.g. during warmup, or after a stream restart before the first candle arrives), `prices["BTC"]` is never set in the first loop.
- If the bridge SOL/BTC engine *also* has no prices yet, `prices["BTC"]` stays absent → `quote_usd = asset_prices.get("BTC", 1.0)` defaults to **1.0**.
- This silently treats "1 BTC" as "1 USD" → SOL/BTC realized P&L (in BTC) is reported as USD with a ~100,000× understatement, not overstatement. So this contributes only to a *negative* skew during warmup, not the +$221 overstatement. Still worth fixing.

### F5 — `_compute_pair_realized_pnl` average-cost basis vs FIFO

The function uses **average-cost basis**: the running weighted average of all open buys. This is one valid accounting convention; it differs from FIFO, which Kraken's own trade ledger uses for tax purposes. Mismatch isn't a bug — it's a definitional choice — but it will produce a different number than Kraken's own reports when partial sells happen against multi-lot buys.

For verification we should compute *both* (avg-cost and FIFO) against Kraken's trades CSV and compare against the dashboard's number to see which methodology actually matches the on-disk journal accounting.

### F6 — `vol_exec` and `avg_fill_price` precision

The journal stores `vol_exec` from the execution stream as a string-coerced float (Kraken's WS execution events). For `BTC` at ~$60k, a single 1e-8 BTC rounding mistake is $0.0006 — irrelevant individually. But across hundreds of fills with consistent rounding direction, drift could accumulate. Likely small contributor.

## Reconciliation plan (next session)

1. Pull Kraken full trade history:
   ```
   wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken trades-history --type all" > kraken_trades_dump.json
   ```
   *(Verify the exact subcommand — Kraken's CLI may use `trades-history`, `trade-history`, or `tradesHistory`.)*

2. Filter to just SOL/USD, SOL/BTC, BTC/USD (the active triangle).

3. Compare each Kraken trade row against `hydra_order_journal.json`:
   - Match by `(pair, ts ± 5s, vol, price)`.
   - Trades present in Kraken but missing from journal = user-action trades. Format them as journal entries with `lifecycle.state="FILLED"`, `lifecycle.notes="user_action_imputed_from_kraken"` (matches existing convention) and merge into the in-memory journal at agent startup.

4. Independently compute, from Kraken trades CSV alone:
   - Total realized P&L per pair using both avg-cost basis and FIFO
   - Net of fees (Kraken includes the fee on each fill row)
   - Convert to USD using historical mark prices at trade time (NOT current — fixes F4 type error if any historical SOL/BTC trades happened during warmup)
   - Sum

5. Compare the three numbers:
   - Kraken-reconstructed realized (truth)
   - Hydra journal realized (current `total_realized_pnl_usd`)
   - Hydra journal net (current `total_pnl_usd`, the +$221.98 the dashboard shows)

The deltas tell us which finding(s) are dominating.

## Proposed fixes (after reconciliation, not yet)

In rough priority order:

1. **F3 — net fees from realized P&L.** Subtract `vol_exec * avg_fill_price * (maker_fee_bps / 10000)` per fill. Likely the biggest single contributor.
2. **F2 — impute user-action trades.** On agent startup, pull last N trades from Kraken, diff against journal, append the missing ones with `lifecycle.notes="user_action"`. Same loop on resume.
3. **F1 — split the dashboard cell into "Realized" + "Open".** Two cards, two numbers, no ambiguity. Both already in the journal_stats payload.
4. **F4 — make `_get_asset_prices` raise (or log loudly) when BTC fallback hits 1.0.** A silent default-to-1 on a quote currency that's actually $60k is a class of bug that should be impossible to ship with.
5. **F5/F6 — accounting methodology + rounding** are validation/QA items, not necessarily bugs. Document the chosen methodology in CLAUDE.md as an invariant.

Each should be its own PR; this audit branch is just discovery.

## Out of scope for this audit

- Backtest P&L accounting (different code path; lower stakes).
- Companion live execution P&L (gated by `HYDRA_COMPANION_LIVE_EXECUTION`, currently default OFF).
- Tax-lot reporting (out of scope for live dashboard accuracy; relevant for end-of-year only).
