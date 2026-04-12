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
hydra_tuner.py      — Self-tuning parameters via exponential smoothing of regime/signal thresholds
dashboard/src/App.jsx — React dashboard (single-file, all inline styles)
SKILL.md            — Full trading specification (agent-readable)
AUDIT.md            — Technical audit with test results
CHANGELOG.md        — Version history
```

## Agent Memory (project-local, gitignored)

This repo has a structured local memory layer for agents. It is **not tracked in git** and is per-machine.

- `HYDRA_MEMORY.md` — readable index: schema, group table, edge vocabulary, usage protocol
- `.hydra-memory/graph.json` — canonical node/edge graph (groups → categories → nodes, plus typed edges)

**Protocol:** On arrival to a non-trivial task, read `HYDRA_MEMORY.md` for the map, then open `graph.json` for the detail relevant to the task. When you learn something durable — an invariant, a decision, a confluence point, an incident, an open question — update the graph. Full schema, tag vocabulary, edge types, hygiene rules, and query patterns all live in `HYDRA_MEMORY.md` — that file is the single source of truth for how to use this system.

If `HYDRA_MEMORY.md` does not exist on this machine (e.g., fresh clone), it has not been bootstrapped yet — this is expected and the rest of the project still works without it.

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
- Confidence threshold: 0.55 conservative mode, 0.50 competition mode. Applied to both BUY and SELL signals — SELL is gated by the same min_confidence check as BUY.
- Position sizing: quarter-Kelly conservative, half-Kelly competition (`(confidence*2 - 1) * multiplier * balance`)
- Order minimums: pair-aware — Kraken `ordermin` per base asset (0.02 SOL, 0.00005 XBT), `costmin` per quote (0.5 USDC, 0.00002 XBT). Enforced on both buy and sell paths. Partial sells below ordermin force full position close to prevent dust.
- Price precision: `KrakenCLI._format_price(pair, price)` rounds to the pair's native decimals before the `.8f` format. Any code that computes a derived price MUST use this — raw `f"{price:.8f}"` will be rejected by Kraken on low-precision pairs (SOL/USDC=2, XBT/USDC=2, SOL/XBT=7). Hardcoded `PRICE_DECIMALS` remain as fallbacks; at startup `KrakenCLI.load_pair_constants()` dynamically loads the true values from `kraken pairs` and patches them via `apply_pair_constants()`.
- Dynamic pair constants: at startup (live mode), the agent calls `kraken pairs` to load `pair_decimals`, `ordermin`, and `costmin` for each traded pair. These override the hardcoded `PRICE_DECIMALS`, `MIN_ORDER_SIZE`, and `MIN_COST` class-level dicts. If the API call fails, hardcoded fallbacks are used — no degradation in behavior.
- System status gate: each tick (live mode) checks `kraken status` before doing any work. If Kraken reports `"maintenance"` or `"cancel_only"`, the tick is skipped with a log message. `"post_only"` is treated as normal (we only place post-only orders). API errors degrade gracefully to `"online"`. Status transitions are logged once per change, not every tick.
- Circuit breaker: 15% max drawdown halts the engine permanently for the session. Both `tick()` and `_maybe_execute` check the halt flag.
- Rate limiting: 2-second minimum between every Kraken API call — do not remove or reduce
- Order journal persistence: `order_journal` is snapshotted immediately after any tick that appends (not just on the periodic N-tick cadence), so a subsequent crash cannot lose entries since the last successful tick. The rolling file `hydra_order_journal.json` is merged on startup so restarts preserve full history.
- Execution stream: lifecycle finalization flows from `kraken ws executions` via the `ExecutionStream` class — push-based, not polling. Placement stays REST (`KrakenCLI.order_buy/sell` with `--userref` for correlation); WS events drive entries from `PLACED` to `FILLED` / `PARTIALLY_FILLED` / `CANCELLED_UNFILLED` / `REJECTED` and handle engine rollback on non-fills. All fill-detection uses the shared `_is_fully_filled()` helper with 1% tolerance.
- Execution stream restart-gap reconciliation: when the stream auto-restarts, `reconcile_restart_gap()` queries `kraken query-orders` for all in-flight orders to detect fills/cancels that occurred while the stream was down. Terminal events are injected into `drain_events()` so the agent processes them in the same tick the stream recovers. Orders still open on the exchange remain in `_known_orders` for the new stream to finalize normally.
- Resume reconciliation: on `--resume`, `_reconcile_stale_placed()` scans the journal for PLACED entries from the previous session and queries the exchange. Terminal orders (closed/canceled/expired) have their journal lifecycle updated directly. Still-open orders are re-registered with the ExecutionStream so WS events finalize them. Engine rollback is not possible for previous-session entries (no `pre_trade_snapshot` persisted) — a warning is logged if an unfilled order is found.
- BaseStream superclass: `ExecutionStream`, `CandleStream`, `TickerStream`, `BalanceStream`, and `BookStream` all inherit from `BaseStream` which provides subprocess spawn/stop, reader/stderr threads, heartbeat-based health checks, and auto-restart with cooldown. Subclasses override `_build_cmd()`, `_on_message(msg)`, and `_stream_label()`.
- Push-based market data: `CandleStream` (ws ohlc) and `TickerStream` (ws ticker) each subscribe to ALL traded pairs in one WS connection. `_fetch_and_tick()` checks the candle stream first (zero REST calls, zero rate-limit sleep); falls back to REST `ohlc()` when the stream is unhealthy. Per-tick rate-limit sleep is skipped when the candle stream is healthy. Both streams are auto-restarted on failure via `ensure_healthy()` each tick.
- Push-based balances: `BalanceStream` (ws balances) receives real-time balance updates. `_build_dashboard_state()` uses WS data when healthy; falls back to REST polling every 5th tick. Asset names are normalized (BTC→XBT) and equities/ETFs are filtered out.
- Push-based order book: `BookStream` (ws book) subscribes to all pairs with depth 10. Phase 1.75 (order book intelligence) uses WS data when healthy; falls back to REST `depth()`. WS format `{price, qty}` dicts are converted to REST format `[price, qty, ts]` arrays so `OrderBookAnalyzer` works unchanged.
- Order batch: `KrakenCLI.order_batch(json_file, pair, validate)` wraps `kraken order batch` for submitting 2–15 orders atomically. Single-pair only (Kraken limitation) — not usable for cross-pair swaps, but available for future same-pair batch scenarios.
- P&L reconciliation: `_reconcile_pnl()` compares journal fill data (vol_exec, fee_quote) against `kraken trades-history`. Aggregates multiple Kraken trades per order_id, reports matched/mismatched/missing counts. Available on-demand; not wired into the tick loop (call manually or add periodic trigger).
- Execution stream health: `ExecutionStream.health_status()` returns `(healthy, reason)` so the tick warning identifies *which* check failed (subprocess exited / reader thread crashed / heartbeat stale). `ensure_healthy()` auto-restarts the subprocess on failure with a `RESTART_COOLDOWN_S=30s` cooldown so we don't thrash. Heartbeat threshold is 30s — kraken cold-start over WSL can take 5–10s before the first heartbeat. A separate stderr-drain thread prevents the OS pipe buffer from filling and silently freezing the subprocess. The tick warning is rate-limited to *transitions* (one print per distinct reason; one "stream healthy again" print on recovery).
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
python tests/test_tuner.py         # Self-tuning exponential smoothing updates
python tests/test_balance.py       # Staked asset handling, USD conversion, engine balance init
python tests/test_kraken_cli.py    # KrakenCLI wrappers: args, parsing, price precision, fee extraction
python tests/test_execution_stream.py # ExecutionStream health diagnostics + auto-restart cooldown
python tests/test_status_gate.py   # System status gate (maintenance skip, degradation, transitions)
python tests/test_pair_constants.py # Dynamic pair constants (load, apply, fallback)
python tests/test_reconciliation.py # Restart-gap reconciliation (query-orders recovery, drain integration)
python tests/test_resume_reconcile.py # Resume reconciliation (stale PLACED entries from previous sessions)
python tests/test_candle_stream.py # CandleStream (ws ohlc) dispatch, storage, symbol mapping
python tests/test_ticker_stream.py # TickerStream (ws ticker) dispatch, storage, symbol mapping
python tests/test_balance_stream.py # BalanceStream (ws balances) dispatch, normalization, filtering
python tests/test_book_stream.py   # BookStream (ws book) dispatch, REST-format conversion, analyzer compat
python tests/test_pnl_reconcile.py # P&L reconciliation (trades-history matching, discrepancy detection)
python hydra_engine.py             # Synthetic engine demo (no API keys needed)
```

### Live-execution test harness

`tests/live_harness/` drives `HydraAgent._place_order` across 33+ scenarios
(happy paths, failure modes, rollback completeness, schema validation,
historical regressions, WS execution stream lifecycle transitions, and real
Kraken). It is the canonical validation tool for any change to `_place_order`,
`ExecutionStream`, `snapshot_position`/`restore_position`, `PositionSizer`, or
any order-journal write site. Snapshot fields include `gross_profit` and
`gross_loss` for per-engine P&L tracking across restarts. A `FakeExecutionStream` test double lets mock
scenarios drive lifecycle transitions via `inject_event(...)` without spawning
the real `kraken ws executions` subprocess.

```bash
python tests/live_harness/harness.py --mode smoke    # import + agent construction
python tests/live_harness/harness.py --mode mock     # full mock-mode scenario run
python tests/live_harness/harness.py --mode validate # real Kraken read-only + --validate
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
- `hydra_order_journal.json` is the rolling order journal — it's gitignored. Legacy `hydra_trades_live.json` is auto-migrated on first startup and preserved as `hydra_trades_live.json.migrated`.
- `hydra_params_*.json` files are learned tuning parameters — they're gitignored
- `hydra_session_snapshot.json` is the session snapshot for `--resume` — it's gitignored
- On shutdown, the agent cancels all resting limit orders and flushes a snapshot — do not bypass this
- `start_hydra.bat` uses `--mode competition --resume` for production — do not remove these flags
- **Feature gap:** CrossPairCoordinator Rule 2 (BTC recovery BUY boost) and Rule 3 (coordinated swap SELL) can theoretically conflict if BTC is TREND_UP + SOL TREND_DOWN + SOL/XBT TREND_UP simultaneously — Rule 3 overwrites Rule 2. Current behavior favors the safer SELL. Future work: add explicit priority or merge logic.
