# CLAUDE.md — Agent Instructions for HYDRA

This file provides context for Claude Code and other AI agents working on this repository.

## Project Overview

HYDRA is a regime-adaptive crypto trading agent for Kraken. It detects market conditions (trending, ranging, volatile) and switches between four strategies (Momentum, Mean Reversion, Grid, Defensive) to execute limit post-only orders on SOL/USDC, SOL/XBT, and XBT/USDC.

## Repository Structure

```
hydra_engine.py     — Pure Python trading engine (indicators, regime detection, signals, position sizing)
hydra_agent.py      — Live agent (Kraken CLI via WSL, WebSocket broadcast, trade execution,
                      order reconciler, session snapshot + --resume)
hydra_brain.py      — AI reasoning: Claude Analyst + Risk Manager + Grok Strategist
hydra_tuner.py      — Self-tuning parameters via Bayesian updating of regime/signal thresholds
dashboard/src/App.jsx — React dashboard (single-file, all inline styles)
SKILL.md            — Full trading specification (agent-readable)
AUDIT.md            — Technical audit with test results
CHANGELOG.md        — Version history
```

## Key Technical Decisions

- **Pure Python, zero dependencies** — `hydra_engine.py` uses only stdlib. No numpy/pandas. Do not add external dependencies to the engine.
- **Limit post-only orders** — All trades use `--type limit --oflags post`. Never use market orders.
- **Kraken CLI via WSL** — Commands run through `wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken ..."`. The CLI is installed in WSL Ubuntu, not Windows.
- **Single-file dashboard** — All React components are in `App.jsx` with inline styles. No component library, no CSS modules. Keep it this way.
- **One engine per pair** — Each trading pair has its own independent `HydraEngine` instance. They do not share state.

## Build & Run

```bash
# Dashboard
cd dashboard && npm install && npm run dev

# Agent — conservative (default, 5-min candles, runs forever)
python hydra_agent.py --pairs SOL/USDC,SOL/XBT,XBT/USDC --balance 100

# Agent — competition mode (half-Kelly, lower threshold)
python hydra_agent.py --mode competition

# Agent — 1-min candles (faster ticks, noisier signals)
python hydra_agent.py --candle-interval 1

# Agent — paper trading (no API keys needed)
python hydra_agent.py --mode competition --paper

# Agent — resume previous session (restores engines + coordinator state)
python hydra_agent.py --mode competition --resume

# Engine synthetic demo (no API keys needed)
python hydra_engine.py
```

## Working with the Code

### Indicators (hydra_engine.py)
- RSI uses Wilder's exponential smoothing — do not simplify to SMA
- ATR uses Wilder's exponential smoothing (same as RSI) — do not simplify to simple average
- MACD builds a full historical series then applies 9-EMA — do not simplify to single-point calculation
- Bollinger Bands use population variance (divide by N, not N-1)
- All indicators are stateless static methods — they recompute from the full price array each tick

### Regime Detection
- Priority: VOLATILE > TREND_UP > TREND_DOWN > RANGING
- Volatile check must come first — it overrides trend signals
- Warmup requires 50 candles before regime detection activates

### Trading
- Confidence threshold: 0.55 conservative mode, 0.50 competition mode
- Position sizing: quarter-Kelly conservative, half-Kelly competition (`(confidence*2 - 1) * multiplier * balance`)
- Order minimums: pair-aware — Kraken `ordermin` per base asset (0.02 SOL, 0.00005 XBT), `costmin` per quote (0.5 USDC, 0.00002 XBT). Enforced on both buy and sell paths. Partial sells below ordermin force full position close to prevent dust.
- Price precision: `KrakenCLI._format_price(pair, price)` rounds to the pair's native decimals before the `.8f` format. Any code that computes a derived price MUST use this — raw `f"{price:.8f}"` will be rejected by Kraken on low-precision pairs (SOL/USDC=2, XBT/USDC=1, SOL/XBT=7).
- Circuit breaker: 15% max drawdown halts the engine permanently for the session. Both `tick()` and `_maybe_execute` check the halt flag.
- Rate limiting: 2-second minimum between every Kraken API call — do not remove or reduce
- Trade log persistence: `trade_log` is snapshotted immediately after any tick that appends (not just on the periodic N-tick cadence), so a subsequent crash cannot lose entries since the last successful tick
- Tick body is wrapped in try/except — any exception is logged to `hydra_errors.log` with full traceback and the tick loop continues to the next iteration instead of dying (which would trigger `start_hydra.bat` restart)

### Dashboard
- Connects to agent via WebSocket on port 8765
- All data comes from `state.pairs[pair]` — no direct API calls from the frontend
- Price formatting: use `fmtPrice()` for prices, `fmtInd()` for indicator values
- Charts use responsive SVG with `width="100%" viewBox`

## AI Brain (hydra_brain.py)

3-agent reasoning pipeline using Claude + Grok:
- **Market Analyst** (Claude Sonnet) — evaluates engine signals, produces thesis + conviction
- **Risk Manager** (Claude Sonnet) — approves/adjusts/overrides trades, manages risk exposure
- **Strategic Advisor** (Grok 4 Reasoning) — called only on contested decisions (ADJUST/OVERRIDE or conviction < 0.65)
- Only fires on BUY/SELL signals (HOLD is free, no API call — skip logic lives in the agent's `_apply_brain`, not in the brain itself)
- Falls back to engine-only on API failure, budget exceeded, or missing key
- Enable by setting `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and/or `XAI_API_KEY` in `.env`
- Cost: ~$3-5/day with Grok escalation on ~20-30% of signals
- Do not change the JSON response format in system prompts — the parser depends on it
- Escalation threshold is parameterized (0.65 conservative, 0.50 competition) — it controls when Grok fires
- Strategist always uses `self.strategist_client` (xAI) — do not route it through primary client

## Testing

```bash
python tests/test_engine.py        # Core engine: indicators, regime, signals, sizing, circuit breaker
python tests/test_cross_pair.py    # Cross-pair coordinator (BTC-leads-SOL rules)
python tests/test_order_book.py    # Depth analyzer, imbalance, walls, confidence modifiers
python tests/test_tuner.py         # Self-tuning Bayesian updates
python tests/test_balance.py       # Staked asset handling, USD conversion, engine balance init
python tests/test_kraken_cli.py    # KrakenCLI wrappers: args, parsing, price precision, fee extraction
python hydra_engine.py             # Synthetic engine demo (no API keys needed)
```

### Live-execution test harness

`tests/live_harness/` drives `HydraAgent._execute_trade` across 34 scenarios
(happy paths, failure modes, rollback completeness, schema validation,
historical regressions, and real Kraken). It is the canonical validation tool
for any change to `_execute_trade`, `OrderReconciler`, `snapshot_position`/
`restore_position`, `PositionSizer`, or any trade-log write site.

```bash
python tests/live_harness/harness.py --mode smoke    # ~1.5s, import + agent construction
python tests/live_harness/harness.py --mode mock     # ~1.5s, 26 scenarios (default)
python tests/live_harness/harness.py --mode validate # ~10s, real Kraken read-only + --validate
python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders
```

`smoke` and `mock` run in CI on every PR. `mock` is the required gate for any
PR touching the execution path. `validate` and `live` are manual for high-risk
changes. See `tests/live_harness/README.md` for the scenario catalog, findings
tracker (HF-### IDs), authoring guide, and the field-sync checklist that must
be consulted before modifying `HydraEngine` snapshot fields.

See AUDIT.md for the full verification checklist.

## Common Pitfalls

- Don't add `import numpy` or `import pandas` to the engine — it's intentionally pure Python
- Don't change orders to market type — limit post-only is a deliberate design choice
- Don't reduce rate limiting below 2s — Kraken will throttle or ban
- Don't merge engine instances across pairs — they must remain independent
- The `.env` file contains Kraken API keys — never commit it
- `hydra_trades_*.json` files are runtime trade logs — they're gitignored
- `hydra_params_*.json` files are learned tuning parameters — they're gitignored
- `hydra_session_snapshot.json` is the session snapshot for `--resume` — it's gitignored
- On shutdown, the agent cancels all resting limit orders and flushes a snapshot — do not bypass this
- `start_hydra.bat` uses `--mode competition --resume` for production — do not remove these flags
- **Feature gap:** CrossPairCoordinator Rule 2 (BTC recovery BUY boost) and Rule 3 (coordinated swap SELL) can theoretically conflict if BTC is TREND_UP + SOL TREND_DOWN + SOL/XBT TREND_UP simultaneously — Rule 3 overwrites Rule 2. Current behavior favors the safer SELL. Future work: add explicit priority or merge logic.
