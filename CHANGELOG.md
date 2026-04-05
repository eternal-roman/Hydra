# Changelog

All notable changes to HYDRA are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed
- **Comprehensive codebase audits** — three review passes landed 12 bug fixes total
  across win/loss tallying (partial sells now accumulate realized PnL before
  tally), defensive BUY gating, PLAN_STEP brain decisions correctly applied,
  Hamilton posterior restoration on `--resume`, dashboard interval variable
  reference, stale brain decision cleanup, hardcoded dashboard interval, and
  several smaller crash risks.
- **Refactor pass** — filter serialisation encapsulated, session snapshot
  writes throttled, trade log bounded, joint-signal solver emission deduped.

### Tests
- `tests/test_balance.py` — 38 balance & asset conversion tests added alongside
  the live-exchange balance feature (see 2.3.2).
- Current suite totals **213 tests across 6 files**: 67 engine, 23 cross-pair,
  38 order book, 26 tuner, 38 balance, 21 brain agent.

---

## [2.3.2] — 2026-04-03

### Added
- **Live exchange balance** — dashboard and `HydraEngine` initialisation now
  pull real balances from `kraken balance` on startup instead of relying solely
  on the `--balance` reference value. The CLI flag still controls per-pair
  reference sizing, but displayed equity and portfolio valuation reflect the
  actual account. 38 new balance/asset-conversion tests (`tests/test_balance.py`).

---

## [2.4.0] — 2026-04-05

### Added
- **Hamilton (1989) regime-switching filter** (`hydra_engine.RegimeSwitchingFilter`) —
  Bayesian filter over the 4 regimes (TREND_UP/DOWN, RANGING, VOLATILE). Maintains a
  posterior probability vector per pair, seeded from observed regime history via a
  Laplace-smoothed transition matrix. Pure Python, ~O(16) ops per update. Exposes the
  full posterior via `state["regime_probs"]` for downstream consumers.
- **QAOA-inspired joint-signal solver** (`hydra_engine.JointSignalSolver`) — replaces
  the three hardcoded if/else rules (with literal 0.8/0.85 confidences) in the old
  `CrossPairCoordinator.get_overrides`. Builds an Ising-style cost Hamiltonian
  `E(s) = -h·s + γ·sᵀΣs` over the N-pair spin configuration space and exact-
  diagonalises the 2^N states (8 for the SOL/USDC + SOL/XBT + XBT/USDC triangle) in
  pure Python. `h` combines per-pair signal with regime-probability drift from the
  Hamilton filter; `Σ` is the rolling 50-candle log-return covariance. Per-pair
  confidences are derived from the energy gap to the runner-up state — no literal
  constants anywhere.
- **Agentic brain loop** (`hydra_brain.HydraBrain.step`) — the AI brain now maintains
  genuine cross-tick state: episodic `BrainMemory` (last 200 decisions per pair with
  state digest, outcome back-filling, regret tracking), named `beliefs` updated via
  EMA on closed trades, `GoalState` with explicit drawdown budget / target Sharpe /
  session PnL target, and a `risk_posture` (conservative/neutral/aggressive) promoted
  automatically from realised drawdown vs budget. `step()` consults multi-step
  `BrainPlan`s registered after high-conviction signals — if a plan's `if_condition`
  matches the live state, the plan step fires without any API call. Otherwise the
  existing Claude-Analyst → Claude-Risk-Manager → Grok-Strategist pipeline is
  delegated to, and a small follow-up plan is optionally registered. `reflect()`
  back-fills realised PnL on closed trades and updates beliefs.
- **Session snapshot + `--resume`** — atomic JSON snapshot at
  `hydra_session_snapshot.json` written via `tmp` + `os.replace()` every tick. On
  `--resume` each engine, the coordinator's regime history + Hamilton filter seeds,
  and the full brain memory (episodes, beliefs, plans, goals) are restored.
- **Order reconciler** (`hydra_agent.OrderReconciler`) — polls `kraken open-orders`
  every 5 ticks, diffs against locally-registered txids, and emits events when a
  known order disappears from the exchange (fill / cancel / reject).
- `HydraEngine.snapshot_runtime()` / `restore_runtime()` — serialise/deserialise
  mutable engine state (balance, position, equity history, candle buffer, halted
  flag) for `--resume`.

### Changed
- **`CrossPairCoordinator.get_overrides`** — two-layer pipeline: per-pair Hamilton
  filter update → attach `regime_probs` → joint-signal solver. No hardcoded
  confidences remain. Backward-compatible output shape: `{pair: {action, signal,
  confidence_adj, reason, swap?}}` unchanged, plus new `joint_energy` /
  `energy_gap` fields.
- **`HydraEngine._calc_sharpe`** — annualisation now uses the *observed* median
  candle timestamp delta (with sub-second fallback to nominal `candle_interval`),
  not a hardcoded `525600 / candle_interval`. Corrects skew when the configured
  interval disagrees with the exchange's actual cadence.
- **`HydraBrain.deliberate`** — becomes a thin wrapper over `step()` so existing
  callsites in `hydra_agent.py` inherit the agentic loop transparently.
- **Total modifier cap** — order book still capped at +0.15 above engine confidence;
  joint-signal coordinator overrides are flagged via `cross_pair_override` and
  bypass the cap so principled covariance-based decisions aren't clipped.
- **Kill-switch on SIGINT/SIGTERM** — agent now cancels all open Kraken orders via
  `cancel-all` and flushes a final session snapshot before exit. Positions are
  left intact (no forced market close).
- **Brain `size_multiplier` clamp `[0.0, 1.25]`** — enforced inside `step()`,
  further capped to 0.5 when `goals.risk_posture == "conservative"`.

### Tests
- `tests/test_cross_pair.py` rewritten for the new pipeline: 23 tests across
  Hamilton filter convergence, transition-matrix seeding, joint-signal solver
  ground-state properties (zero-covariance reduction, energy gap → confidence
  mapping, no-hardcoded-constants regression guard), coordinator pipeline, and
  timestamp-derived Sharpe.
- `tests/test_brain_agent.py` — 21 tests across `BrainMemory` CRUD + round-trip
  serialisation, `GoalState` posture promotion, `BrainPlan` follow-through,
  `size_multiplier` clamping, and `reflect()` outcome back-fill.

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
- 31 new order book tests (`tests/test_order_book.py`): parsing (direct + nested format), imbalance ratios, spread calculation, wall detection, BUY/SELL/HOLD modifier logic, edge cases (zero volume, malformed entries, small prices). Total: 120 tests. *(Suite has since grown to 38 tests with later edge-case additions.)*

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
- 5-layer fallback system: single failure, repeated failures (disable 30 min), budget exceeded, missing API key, timeout.
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
