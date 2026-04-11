# Changelog

All notable changes to HYDRA are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.5.0] — 2026-04-11

### Added
- **KrakenCLI wrappers** — `volume()`, `spreads()`, and `order_amend()`
  thin passthroughs over the kraken CLI commands of the same name. `volume`
  is called once per hour from `_build_dashboard_state` to cache the 30-day
  fee tier; `spreads` is polled every 5 ticks in a new Phase 1.8 to maintain
  a 120-entry rolling history per pair; `order_amend` is groundwork for a
  future drift-detect repricing loop (no caller yet).
- **Fee tier + spread diagnostics on the dashboard** — compact `Fee M/T`
  pill in each pair's Indicators row showing current maker/taker fee, and
  a `Spread X.X bps (N samples)` readout below it. Inline styles, no new
  components.
- **`KrakenCLI._format_price(pair, price)`** — pair-aware price rounding
  that looks up native precision in a new `PRICE_DECIMALS` dict (SOL/USDC=2,
  XBT/USDC=1, SOL/XBT=7, etc.) and rounds before the `.8f` format. Applied
  to `order_buy`, `order_sell`, and `order_amend`. Required for any future
  code path that computes a derived price (drift→amend, maker-fee shading).
- **Live-execution test harness** (`tests/live_harness/`) — drives
  `HydraAgent._execute_trade` across 34 scenarios (happy, failure, edge,
  schema, rollback, historical regression, real Kraken) in four modes:
  `smoke`, `mock` (default, ~1.5s), `validate`, `live`. Fast mock mode
  achieved by monkey-patching `time.sleep` to no-op. Runs in CI on every
  PR as a regression gate. Surfaced HF-001 through HF-004 on its first run.
- **Findings tracker** — stable `HF-###` IDs with severity (S1-S4), status,
  fix commit, and regression test. Documented in the harness README.
- **`hydra_errors.log`** — any exception caught by the new tick-body
  try/except writes a full traceback here with timestamp. Previously
  unhandled exceptions would silently kill `run()` and force a
  `start_hydra.bat` restart with lost in-memory state.
- **61 new tests in `test_kraken_cli.py`** — TestVolumeArgsAndParsing (8),
  TestSpreadsArgsAndParsing (7), TestPriceFormat (14), TestOrderAmendArgs (9),
  TestFeeTierExtraction (9), TestRecordSpreads (11), plus the `_StubRun`
  helper and Kraken response builders reused by the harness.
- **11 new tests in `test_engine.py`** — TestHaltedEngineExecuteSignal (3)
  for HF-002, TestSnapshotTradesRoundTrip (8) for HF-004.

### Fixed
- **HF-004 (S1, active production bug)** — `trade_log` silently frozen
  across tick crashes. Two-part root cause: (a) `HydraEngine.snapshot_runtime()`
  did not include `self.trades`, so every `--resume` started with
  `engine.trades == []` while counters were restored correctly — per-pair
  P&L from trade history was silently broken; (b) the tick loop body had
  no top-level try/except, so any unhandled exception killed `run()` and
  `start_hydra.bat` restarted from the stale snapshot (saved only every
  12 ticks ≈ 1h), losing all new entries since the last successful save.
  Fix: serialize `trades[-500:]` in `snapshot_runtime`; wrap tick body in
  try/except that logs tracebacks to `hydra_errors.log` and continues to
  the next iteration; save snapshot immediately after any tick that
  appends to `trade_log`, not just on the N-tick cadence.
- **HF-003** — `except Exception: pass` in the rolling log writer
  silently swallowed every write failure. Replaced with a logged warning
  so failures become visible.
- **HF-001** — `KrakenCLI` hardcoded `.8f` price precision regardless of
  pair. Production was safe today because `_execute_trade` only passed
  `ticker["bid"]`/`ticker["ask"]` unmodified, but any derived price would
  have hit Kraken's per-pair precision rejection. Fixed via
  `_format_price` helper (see Added).
- **HF-002** — `HydraEngine.execute_signal` did not check the `halted`
  flag. Only `tick()` did, so halt was enforced via a non-local invariant
  ("`tick()` always runs first") rather than at the boundary. Any future
  caller of `execute_signal` on a halted engine would silently trade.
  Fix: `if self.halted: return None` at the top of `_maybe_execute`.
- **Dashboard fee pill null-collapse** — when `_extract_fee_tier` couldn't
  parse a fee, it stored `null`; dashboard's `(null ?? 0).toFixed(2)`
  silently rendered `"0.00%"` (misleading "zero fees" display). Fixed
  via IIFE gate that hides the pill when both sides are null and shows
  `—` for individually-null sides.
- **`order_amend` txid validation** — previously accepted `None`/`""`
  silently and burned an API slot producing an obscure Kraken error. Now
  returns a clean local error dict matching the fail-fast pattern used
  for missing `limit_price`/`order_qty`.

### Changed
- Snapshot cadence: was strictly every `SNAPSHOT_EVERY_N_TICKS` ticks
  (default 12). Now also triggers immediately after any tick whose
  `trade_log` grew, so a subsequent crash can lose at most one unsaved
  append instead of up to an hour's worth.
- CI adds a `Run live-execution harness (smoke + mock)` step to the
  `engine-tests` job (~3 seconds added to total CI time).

---

## [2.4.0] — 2026-04-05

### Added
- **Order reconciler** (`OrderReconciler`) — polls `kraken open-orders` every
  5 ticks and detects orders that disappeared (filled, DMS-cancelled, rejected).
  Prevents silent divergence between agent and exchange state.
- **Session snapshots + `--resume`** — atomic JSON snapshots of engine state,
  coordinator regime history, and recent trade log. Written every 12 ticks
  (~1h at 5-min candles) and on SIGINT/SIGTERM. `start_hydra.bat` auto-restart
  now uses `--resume` for seamless recovery.
- **Shutdown cancel-all** — `_handle_shutdown` cancels all resting limit orders
  on Kraken before exit.
- **Trade log bounding** — capped at 2000 entries to prevent unbounded growth.

### Fixed
- **Brain JSON parsing** — strip markdown code fences from LLM responses;
  increased API timeout 10s→30s and max_tokens to prevent truncation.
- **ATR smoothing** — now uses Wilder's exponential smoothing (was simple average).
- **TREND_DOWN symmetry** — `down_ratio` uses multiplicative inverse `1/ratio`.
- **Coordinated swap state sync** — sell/buy legs call `execute_signal()` on
  engines before placing Kraken orders; swap sell pairs excluded from Phase 2.5
  to prevent premature position close.
- **Swap currency conversion** — buy-leg sizing converts proceeds to buy-pair
  quote currency via XBT/USDC price when currencies differ.
- **Tuner accuracy** — records on full position close only, using accumulated
  `realized_pnl`, with `params_at_entry` preserved on Trade object.
- **Ticker freshness** — re-fetches bid/ask immediately before order placement.
- **Price precision** — 8 decimals for all prices/amounts; pair-aware rounding
  for dollar values (2 for USDC/USD, 8 for crypto pairs).
- **Candle dedup** — ticker-fallback candles get interval-aligned timestamps.
- **Sharpe annualization** — uses observed candle timestamp deltas (median)
  instead of nominal `candle_interval`.
- **Txid handling** — unwraps list-format txids from Kraken API.
- **Trade confidence** — `last_trade` dicts now include `confidence` key.
- **Competition mode** — `start_hydra.bat` uses `--mode competition --resume`.

---

## [2.3.1] — 2026-04-02

### Changed
- Order book confidence modifier range reduced from ±0.20 to ±0.07 based on Monte Carlo
  analysis (50k paths) showing Sharpe peak at ±0.07 with rapid degradation above ±0.15.
- Added total external modifier cap of +0.15 — cross-pair coordinator + order book
  combined cannot boost confidence more than +0.15 above the engine's original signal.
  Downward modifiers remain uncapped (weak signals should be killable by external data).
- When cross-pair coordinator changes signal direction (e.g., BUY→SELL override),
  the cap baseline resets to the coordinator's confidence, not the engine's original.

### Fixed
- Stacking vulnerability where cross-pair (+0.15) and order book (+0.20) could inflate
  a 0.55 engine signal to 0.90, causing Kelly criterion to oversize speculative positions.

---

## [2.3.0] — 2026-04-02

### Added
- **Self-Tuning Parameters** (`hydra_tuner.py`) — Bayesian updating of regime detection and signal generation thresholds based on trade outcomes.
  - `ParameterTracker` class tracks 8 tunable parameters: `volatile_atr_pct`, `volatile_bb_width`, `trend_ema_ratio`, `momentum_rsi_lower/upper`, `mean_reversion_rsi_buy/sell`, `min_confidence_threshold`.
  - Conservative 10% shift per update cycle toward winning trade parameter means — prevents overfitting to recent market conditions.
  - Hard bounds on all parameters (e.g., RSI thresholds clamped 10–90, ATR 1%–8%) to prevent degenerate configurations.
  - Persists learned params to `hydra_params_{pair}.json` across restarts.
  - Updates trigger every 50 completed trades or on agent shutdown.
- **Tunable engine parameters** — `RegimeDetector.detect()` now accepts `trend_ema_ratio`, `SignalGenerator.generate()` accepts RSI thresholds for momentum and mean reversion strategies.
- `HydraEngine.snapshot_params()` / `apply_tuned_params()` — snapshot and apply tunable parameter sets.
- `Position.params_at_entry` — captures parameter state at BUY time so outcomes are attributed to the correct parameter values.
- `--reset-params` CLI flag — wipes all learned parameter files back to defaults.
- 26 new tuner tests (`tests/test_tuner.py`): defaults, recording, min observations guard, Bayesian shift direction, clamping, persistence (save/load/reset/corrupt), engine integration. Total: 146 tests.

---

## [2.2.0] — 2026-04-02

### Added
- **Order Book Intelligence** (`OrderBookAnalyzer` in `hydra_engine.py`) — analyzes Kraken order book depth to generate signal-aware confidence modifiers.
  - Computes bid/ask volume totals, imbalance ratio, spread in basis points.
  - **Wall detection** — flags bid or ask walls when a single level exceeds 3x the average level volume.
  - **Confidence modifier** (−0.07 to +0.07) based on imbalance vs signal direction: bullish book boosts BUY / penalizes SELL, bearish book boosts SELL / penalizes BUY, HOLD unchanged.
- `KrakenCLI.depth()` — fetches order book depth (top 10 levels per side) via `kraken depth` command.
- Order book data injected into engine state as `order_book` key, visible to AI brain for reasoning.
- Agent Phase 1.75: fetches depth for each pair between cross-pair coordination and brain deliberation, applies confidence modifier, logs imbalance/spread/wall status.
- 31 new order book tests (`tests/test_order_book.py`): parsing (direct + nested format), imbalance ratios, spread calculation, wall detection, BUY/SELL/HOLD modifier logic, edge cases (zero volume, malformed entries, small prices). Total: 120 tests.

---

## [2.1.0] — 2026-04-02

### Added
- **Cross-Pair Regime Coordinator** (`CrossPairCoordinator` in `hydra_engine.py`) — detects regime divergences across the SOL/USDC + SOL/XBT + XBT/USDC triangle and generates coordinated signal overrides.
  - **Rule 1: BTC leads SOL down** — when XBT/USDC shifts to TREND_DOWN while SOL/USDC is still TREND_UP or RANGING, overrides SOL/USDC to SELL with 0.80 confidence.
  - **Rule 2: BTC recovery boost** — when XBT/USDC shifts to TREND_UP while SOL/USDC is TREND_DOWN, boosts SOL/USDC confidence by +0.15 (capped at 0.95) for recovery buy.
  - **Rule 3: Coordinated swap** — when SOL/USDC is TREND_DOWN but SOL/XBT is TREND_UP with an open position, generates atomic sell-SOL/USDC + buy-SOL/XBT swap with shared `swap_id`.
- **Coordinated swap execution** in `hydra_agent.py` — executes two-leg swaps (sell first, then buy) as an atomic unit with shared swap ID, logged together in the trade log.
- 22 new cross-pair tests (`tests/test_cross_pair.py`): regime history tracking, all three override rules, no-override baselines, rule priority, and Sharpe annualization fix. Total: 89 tests.

### Fixed
- **Sharpe annualization bug** — `_calc_sharpe()` used `sqrt(525600)` assuming 1-minute candles. Now uses `sqrt(525600 / candle_interval)` to correctly annualize for 5-minute or other intervals.

---

## [2.0.0] — 2026-04-02

### Added
- **3-agent AI reasoning pipeline** (`hydra_brain.py`) — Claude + Grok evaluate every BUY/SELL signal before execution.
  - **Market Analyst** (Claude Sonnet) — analyzes indicators, regime, price action; produces thesis, conviction, agreement/disagreement with engine signal.
  - **Risk Manager** (Claude Sonnet) — evaluates portfolio risk, drawdown, exposure; produces CONFIRM / ADJUST / OVERRIDE decision with size multiplier.
  - **Strategic Advisor** (Grok 4 Reasoning) — called only on contested decisions (ADJUST/OVERRIDE or conviction < 0.65). Re-evaluates with full context from both prior agents and makes the final call.
- Multi-provider support: Anthropic Claude (primary) + xAI Grok (strategist). Both keys configurable via `.env`.
- Intelligent escalation: clear CONFIRM signals skip Grok (~$0.008/decision), contested signals escalate (~$0.011/decision).
- AI reasoning displayed in dashboard: decision badges (CONFIRM/ADJUST/OVERRIDE), analyst thesis, risk assessment, Grok strategist reasoning (when escalated), risk flags.
- AI Brain sidebar panel: decisions, overrides, escalations, strategist status, API cost, latency, active/offline status.
- Header badge switches to "AI LIVE" when brain is active.
- 5-layer fallback system: single failure, repeated failures (disable 60 ticks), budget exceeded, missing API key, timeout.
- Daily cost guard (`max_daily_cost`) prevents runaway API spend.
- 8 new brain tests (fallback, budget guard, JSON parser, prompt builders, caching). Total: 62 tests.

### Changed
- Agent now routes BUY/SELL signals through 3-agent AI pipeline before execution (HOLD signals skip AI to save cost).
- Trade log includes AI reasoning when brain is active.
- Dashboard shows AI reasoning inline in each pair panel, with Grok strategist panel on escalated decisions.

---

## [1.1.0] — 2026-04-01

### Added
- **Competition mode** (`--mode competition`) — half-Kelly sizing, 50% confidence threshold, 40% max position. Optimized for the lablab.ai AI Trading Agents hackathon (March 30 — April 12, 2026, $55k prize pool).
- **Paper trading** (`--paper`) — uses `kraken paper buy/sell` commands. No API keys needed, no real money at risk. Safe strategy validation before going live.
- **Competition results export** — `competition_results_{timestamp}.json` with per-pair PnL, drawdown, Sharpe, trade log, and session metadata for submission proof.
- **Configurable position sizing** — `PositionSizer` is now an instance with configurable `kelly_multiplier`, `min_confidence`, and `max_position_pct`. Two presets: `SIZING_CONSERVATIVE` and `SIZING_COMPETITION`.
- 7 new tests: competition sizing threshold, larger positions, higher max, half-Kelly ratio, preset validation, engine mode acceptance, defaults check. Total: 54 tests.

### Changed
- `PositionSizer` refactored from static class to configurable instance — breaks no external API, all existing behavior preserved via `SIZING_CONSERVATIVE` default.
- Dead man's switch and order validation skip in paper mode (not needed).
- Agent banner shows trading mode (LIVE/PAPER) and sizing mode (CONSERVATIVE/COMPETITION).
- Default `--interval` changed to 30s (was 60s).

---

## [1.0.0] — 2026-04-01

### Added
- Core trading engine (`hydra_engine.py`) with pure Python indicators: EMA, RSI (Wilder's), ATR, Bollinger Bands, MACD (proper 9-EMA signal line)
- Four-regime detection: TREND_UP, TREND_DOWN, RANGING, VOLATILE — with priority ordering (volatile overrides trends)
- Four trading strategies: Momentum, Mean Reversion, Grid, Defensive — each with BUY/SELL/HOLD signal generation
- Quarter-Kelly position sizing with hard limits (30% max position, 55% confidence threshold, $0.50 minimum)
- Circuit breaker at 15% max drawdown — halts all trading automatically
- Live trading agent (`hydra_agent.py`) connecting to Kraken via kraken-cli (WSL)
- Limit post-only orders (`--type limit --oflags post`) — maker fees, no spread crossing
- Order validation via `--validate` before every execution
- Dead man's switch (`kraken order cancel-after 60`) refreshed every tick
- Rate limiting — minimum 2 seconds between every Kraken API call
- Three trading pairs: SOL/USDC, SOL/XBT, XBT/USDC (full coin triangle)
- WebSocket broadcast server (port 8765) for real-time dashboard communication
- React + Vite live dashboard (`dashboard/`) with:
  - Candlestick charts (80 candles per pair, responsive SVG)
  - Signal confidence meter with color-coded BUY/SELL/HOLD
  - Per-pair regime detection with strategy matrix
  - Balance history line chart
  - Scrollable trade log with status indicators
  - Kraken account balance (cached every 5th tick)
  - Session configuration panel
  - Auto-reconnecting WebSocket with connection status indicator
- Three-headed Hydra SVG favicon with purple/cyan color scheme
- Smart price formatting (`fmtPrice`) handling $0.0012 to $67,000
- Smart indicator formatting (`fmtInd`) with dynamic decimal precision
- Auto-restart launcher scripts (`start_all.bat`, `start_hydra.bat`, `start_dashboard.bat`)
- Windows Startup shortcut via `create_shortcut.ps1`
- Continuous mode (`--duration 0`) for indefinite operation
- Graceful shutdown (Ctrl+C) with final performance report and trade log export
- SKILL.md agent skill definition for Claude Code / MCP compatibility
- AUDIT.md technical audit report (49 tests, all passing)
- Cross-pair regime swap detection (advisory logging)

### Fixed
- RSI: Replaced simple sum with Wilder's exponential smoothing
- MACD: Replaced incorrect `signal = macd * constant` with proper 9-EMA of historical MACD series
- Orders: Changed from market orders to limit post-only (maker)
- Rate limiting: Added 2s sleep between every API call (was batching multiple calls instantly)
- Trade log: Now logs actual limit price instead of engine's internal price
- Dead man's switch: Now refreshed every tick (was every 2nd tick, risking expiry)
- Dashboard balance: Cached every 5th tick (was fetching every tick, wasting API calls)
- Indicator precision: Dynamic decimals based on price magnitude (fixed SOL/XBT showing 0.00)
- Continuous mode: Fixed TypeError when `remaining` was string in dashboard state
- Performance report: Replaced misaligned box-drawing characters with clean ASCII formatting
