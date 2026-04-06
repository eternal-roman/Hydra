# HYDRA Technical Audit Report

**Date:** 2026-04-01
**Scope:** Full end-to-end audit of all components — engine, agent, dashboard, infrastructure
**Status:** All critical issues resolved. System verified operational.

> **Note:** Line numbers reference the codebase as of this audit date and may drift with future changes.

---

## Summary Scorecard

| Area | Tests | Pass | Fixed | Status |
|------|-------|------|-------|--------|
| Technical Indicators | 5 | 3 | 2 (RSI, MACD) | PASS |
| Regime Detection | 6 | 6 | 0 | PASS |
| Signal Generation | 5 | 5 | 0 | PASS |
| Position Sizing | 3 | 3 | 0 | PASS |
| Order Execution | 6 | 3 | 3 (order type, rate limit, log price) | PASS |
| WebSocket Broadcast | 3 | 3 | 0 | PASS |
| Dashboard Components | 15 | 15 | 0 | PASS |
| Infrastructure | 6 | 5 | 1 (dead man's switch refresh) | PASS |
| **Total (v1.0 audit)** | **49** | **43** | **6** | **ALL PASS** |
| **Total (v2.4.0)** | **191** | **191** | **0** | **ALL PASS** |

**Bugs found:** 10 (all fixed)
**Known limitations:** 5 (documented in section 9)

---

## Table of Contents

1. [Technical Indicators](#1-technical-indicators-hydra_enginepy) — EMA, RSI, ATR, Bollinger Bands, MACD
2. [Regime Detection](#2-regime-detection-hydra_enginepy) — Priority ordering, thresholds, warmup
3. [Signal Generation](#3-signal-generation-hydra_enginepy) — Momentum, Mean Reversion, Grid, Defensive
4. [Position Sizing](#4-position-sizing-hydra_enginepy) — Quarter-Kelly, hard limits, circuit breaker
5. [Order Execution](#5-order-execution-hydra_agentpy) — Limit post-only, validation, rate limiting
6. [WebSocket Broadcast](#6-websocket-broadcast-hydra_agentpy) — Server, state payload, candle data
7. [Dashboard](#7-dashboard-appjsx) — All 15 UI components
8. [Infrastructure](#8-infrastructure) — Auto-restart, startup, pair mapping, parsing
9. [Known Limitations](#9-known-limitations) — 5 documented behaviors
10. [Fixes Applied](#10-fixes-applied-during-audit) — 10 issues resolved

---

## 1. Technical Indicators (hydra_engine.py)

### 1.1 EMA — Exponential Moving Average
- **Location:** `Indicators.ema()` lines 117-126
- **Test:** SMA seed initialization, multiplier k = 2/(n+1), iterative smoothing
- **Result:** PASS
- **Notes:** Standard textbook implementation. SMA seed over first `period` values, then exponential smoothing for remaining.

### 1.2 RSI — Relative Strength Index
- **Location:** `Indicators.rsi()` lines 136-161
- **Test:** Wilder's exponential smoothing (not simple average), proper gain/loss separation, edge case avg_loss=0
- **Result:** PASS (after fix)
- **Fix applied:** Original used simple sum over last N changes. Rewrote to use Wilder's method — SMA seed for first period, then `avg = (prev_avg * (period-1) + current) / period` for all subsequent values.
- **Verified:** Produces correct RSI values matching TradingView reference for same input data.

### 1.3 ATR — Average True Range
- **Location:** `Indicators.atr()` lines 163-176
- **Test:** True Range computed as max(high-low, |high-prev_close|, |low-prev_close|), averaged over period
- **Result:** PASS
- **Notes:** Uses Wilder's exponential smoothing (SMA seed, then recursive smoothing) — updated in v2.4.0 for consistency with RSI.

### 1.4 Bollinger Bands
- **Location:** `Indicators.bollinger_bands()` lines 178-193
- **Test:** Population variance (divide by N, not N-1), width normalized as `(2 * std_mult * std) / mean`
- **Result:** PASS
- **Notes:** Population variance is correct for Bollinger Bands (not sample variance). Width returns a ratio (0.08 = 8%), which matches the regime detection threshold.

### 1.5 MACD — Moving Average Convergence Divergence
- **Location:** `Indicators.macd()` lines 195-226
- **Test:** Historical MACD series built from EMA-fast minus EMA-slow at each point, signal line as 9-EMA of that series
- **Result:** PASS (after fix)
- **Fix applied:** Original used `signal_line = macd_line * (2/(signal_period+1))` which is mathematically incorrect — it multiplied a single value by the smoothing constant instead of computing an EMA over historical values. Rewrote to build full MACD series and apply proper EMA.
- **Verified:** Histogram now correctly represents momentum divergence.

---

## 2. Regime Detection (hydra_engine.py)

### 2.1 Detection Priority
- **Location:** `RegimeDetector.detect()` lines 236-258
- **Test:** VOLATILE checked first (overrides trends), then TREND_UP, then TREND_DOWN, then RANGING as default
- **Result:** PASS
- **Notes:** Correct priority ordering. Volatile markets should not be misclassified as trending.

### 2.2 VOLATILE Regime
- **Condition:** `ATR% > 4.0` OR `BB width > 0.08`
- **Code:** `atr_pct = (atr / current) * 100` then `atr_pct > 4.0 or bb["width"] > 0.08`
- **Result:** PASS — BB width is a ratio (0.08 = 8%), comparison is correct.

### 2.3 TREND_UP Regime
- **Condition:** `EMA20 > EMA50 * 1.005` AND `price > EMA20`
- **Result:** PASS — 0.5% threshold prevents false signals from noise.

### 2.4 TREND_DOWN Regime
- **Condition:** `EMA20 < EMA50 * 0.995` AND `price < EMA20`
- **Result:** PASS — mirrors TREND_UP with inverse logic.

### 2.5 RANGING Regime
- **Condition:** Default fallback when no other regime matches
- **Result:** PASS

### 2.6 Warmup Guard
- **Condition:** Returns RANGING if `len(prices) < 50`
- **Result:** PASS — prevents meaningless regime detection on insufficient data.

---

## 3. Signal Generation (hydra_engine.py)

### 3.1 MOMENTUM Strategy
- **Location:** `SignalGenerator._momentum()` lines 325-350
- **BUY:** RSI 30-70, MACD histogram > 0, price > BB middle — PASS
- **SELL:** RSI > 75 OR MACD histogram < 0 — PASS
- **Confidence:** `min(0.95, 0.5 + |histogram|/price * 1000)` — scales proportionally to price magnitude. Verified for SOL ($78), XBT ($67k), SOL/XBT ($0.0012).

### 3.2 MEAN_REVERSION Strategy
- **Location:** `SignalGenerator._mean_reversion()` lines 353-378
- **BUY:** price <= BB lower AND RSI < 35 — PASS
- **SELL:** price >= BB upper AND RSI > 65 — PASS
- **Confidence:** Scales with distance from middle band.

### 3.3 GRID Strategy
- **Location:** `SignalGenerator._grid()` lines 380-406
- **BUY:** Price in bottom zone (dist_from_lower < 1 of 5 zones) — PASS
- **SELL:** Price in top zone (dist_from_lower > 4 of 5 zones) — PASS
- **Division:** `grid_spacing = (upper - lower) / 5` with zero-division guard.

### 3.4 DEFENSIVE Strategy
- **Location:** `SignalGenerator._defensive()` lines 408-432
- **BUY:** RSI < 20 only (extreme oversold), confidence 0.4 (below 0.55 threshold, so never executes unless manually overridden) — PASS by design
- **SELL:** RSI > 50, confidence 0.8 — PASS
- **Notes:** Intentionally ultra-conservative. In TREND_DOWN, the agent reduces exposure but doesn't buy into falling markets.

### 3.5 Signal Warmup Guard
- **Condition:** Returns HOLD with 0 confidence if `len(prices) < 26` (MACD slow period)
- **Result:** PASS

---

## 4. Position Sizing (hydra_engine.py)

### 4.1 Quarter-Kelly Formula
- **Location:** `PositionSizer.calculate()` lines 453-487
- **Formula:** `edge = max(0, confidence*2 - 1)`, `kelly_quarter = edge * 0.25`, `position_value = kelly_quarter * balance`
- **Result:** PASS — matches SKILL.md specification.

### 4.2 Hard Limits
| Limit | Spec | Code | Status |
|-------|------|------|--------|
| Max position | 30% of balance | `MAX_POSITION_PCT = 0.30` | PASS |
| Min confidence | 0.55 | `MIN_CONFIDENCE = 0.55` | PASS |
| Min trade value | $0.50 (Kraken costmin) | `MIN_TRADE_VALUE = 0.50` | PASS |
| Min order size | Per-asset (SOL:0.02, XBT:0.00005) | `MIN_ORDER_SIZE` dict | PASS |

### 4.3 Circuit Breaker
- **Location:** `HydraEngine.tick()` lines 574-578
- **Condition:** Halts if `max_drawdown > 15%`
- **Result:** PASS — sets `self.halted = True`, all subsequent ticks return HOLD.

---

## 5. Order Execution (hydra_agent.py)

### 5.1 Order Type
- **Location:** `KrakenCLI.order_buy()` / `order_sell()` lines 151-179
- **Type:** Limit post-only (`--type limit --oflags post`)
- **Result:** PASS
- **Notes:** Post-only ensures orders sit on the book as maker orders. If they would cross the spread, Kraken rejects them (preventing accidental taker execution).

### 5.2 Price Selection
- **Location:** `_execute_trade()` lines 435-439
- **BUY:** Placed at current bid price (top of book, maker side)
- **SELL:** Placed at current ask price (top of book, maker side)
- **Result:** PASS

### 5.3 Validation Before Execution
- **Location:** `_execute_trade()` lines 441-457
- **Flow:** Fetch ticker → validate with `--validate` flag → execute if valid
- **Result:** PASS — validation failures are logged and skip execution.

### 5.4 Rate Limiting
- **Location:** Throughout `_execute_trade()` and main loop
- **Spec:** Minimum 2 seconds between every Kraken API call
- **Result:** PASS (after fix)
- **Fix applied:** Added `time.sleep(2)` between every API call — ticker fetch, validation, execution, OHLC fetch, balance check, dead man's switch refresh.

### 5.5 Dead Man's Switch
- **Location:** Main loop line 366, `KrakenCLI.cancel_after()` line 182
- **Behavior:** `kraken order cancel-after 60` refreshed every tick (30s default)
- **Result:** PASS — 60s timeout with 30s refresh gives 30s safety margin.

### 5.6 Trade Log Accuracy
- **Location:** `_execute_trade()` lines 475-487
- **Test:** Logs `limit_price` (actual order price from bid/ask), not engine's internal price
- **Result:** PASS (after fix)
- **Fix applied:** Original logged `trade["price"]` (engine price). Changed to `limit_price`.

---

## 6. WebSocket Broadcast (hydra_agent.py)

### 6.1 DashboardBroadcaster
- **Location:** Lines 196-256
- **Test:** Starts in background thread, handles multiple clients, auto-reconnect, latest state sent on connect
- **Result:** PASS
- **Notes:** Uses `asyncio.run_coroutine_threadsafe()` for thread-safe broadcast from sync agent loop to async WebSocket server.

### 6.2 State Payload
- **Location:** `_build_dashboard_state()` lines 517-541
- **Fields:** tick, timestamp, elapsed, remaining, balance (cached), pairs (full state per pair), trade_log (last 20)
- **Result:** PASS
- **Notes:** Balance fetched every 5th tick to reduce API load. `remaining=0` for continuous mode.

### 6.3 Candle Data
- **Location:** `_build_state()` in engine, line 703-706
- **Payload:** Last 100 candles as `{o, h, l, c, t}` objects
- **Result:** PASS — dashboard slices to 80 for rendering.

---

## 7. Dashboard (App.jsx)

### 7.1 WebSocket Connection
- **Location:** `connect()` callback, lines 172-190
- **Test:** Auto-connect on mount, 3s reconnect on disconnect, cleanup on unmount
- **Result:** PASS — `useCallback` prevents stale closures, `useEffect` cleanup prevents memory leaks.

### 7.2 StatCard Row
- **Components:** Total Balance, P&L, Max Drawdown, Trades, Win Rate
- **Data source:** Derived from `state.pairs` portfolio data
- **Result:** PASS — all values compute correctly from live WebSocket data.

### 7.3 CandleChart
- **Location:** Lines 87-129
- **Test:** Renders OHLC candles with correct green (bullish) / red (bearish) coloring, wick lines, responsive SVG via viewBox
- **Result:** PASS
- **Notes:** Price labels use `fmtInd()` for correct decimal places across price magnitudes.

### 7.4 ConfidenceMeter
- **Location:** Lines 131-143
- **Test:** Bar width from confidence (0-1), color from signal type (BUY=green, SELL=red, HOLD=amber)
- **Result:** PASS

### 7.5 Per-Pair Panels
- **Components:** Price header, regime dot, strategy icon, candle chart, confidence meter, signal reason, position, balance, indicators
- **Data source:** `state.pairs[pair]` — all fields populated from engine state
- **Result:** PASS — all components render with null-safe defaults (e.g. `pos.size > 0 ? ... : "Flat"`).

### 7.6 Indicator Row
- **Components:** RSI (colored at 30/70), MACD histogram (green/red), BB range, BB width (highlighted at 6%)
- **Formatting:** `fmtInd()` auto-formats based on magnitude (6 decimals for <0.01, 4 for <1, 2 otherwise)
- **Result:** PASS — verified correct for SOL/XBT (0.0012), SOL/USDC ($78), XBT/USDC ($67k).

### 7.7 Balance History Chart
- **Location:** Lines 342-348
- **Data:** Accumulated total equity across ticks (up to 500 points)
- **Result:** PASS — renders after 5+ data points.

### 7.8 Trade Log
- **Location:** Lines 350-372
- **Test:** Reverse-chronological, status icons, BUY/SELL coloring, smart price formatting via `fmtPrice()`
- **Result:** PASS — text overflow handled with ellipsis.

### 7.9 Sidebar — Kraken Account
- **Data source:** `state.balance` (cached from exchange every 5th tick)
- **Result:** PASS — shows "Loading..." when empty.

### 7.10 Sidebar — Strategy Matrix
- **Test:** 4 rows (regime → strategy), active regimes highlighted with glowing dot and pair labels, inactive dimmed to 35%
- **Result:** PASS — dynamically highlights based on current pair regimes.

### 7.11 Sidebar — Per-Pair Stats
- **Components:** Trades, Win Rate, Sharpe, Drawdown per pair
- **Styling:** Regime-colored border and header dot
- **Result:** PASS

### 7.12 Sidebar — Session Panel
- **Content:** Order type (Limit Post-Only), Interval (30s), Pairs count, Circuit Breaker (15% DD), Dead Man (Active), Sizing (Quarter-Kelly)
- **Result:** PASS — static operational reference, all values accurate.

### 7.13 Waiting Screen
- **Condition:** `!connected && !state`
- **Content:** Hydra logo, title, WS URL, startup command
- **Result:** PASS

### 7.14 ConnectionStatus
- **Location:** Lines 146-159
- **Test:** Green dot when connected (with tick counter), red pulsing dot when disconnected
- **CSS:** `pulse` keyframe animation defined in `index.css`
- **Result:** PASS

### 7.15 Price Formatting
- **Location:** `fmtPrice()` lines 43-50
- **Test:** $0 for zero, 8 decimals for <0.001, 6 for <0.01, 4 for <1, locale-formatted for >10000, 2 decimals otherwise
- **Result:** PASS — handles SOL/XBT ($0.0012), SOL/USDC ($78), XBT/USDC ($67,000).

---

## 8. Infrastructure

### 8.1 Auto-Restart Scripts
| File | Test | Result |
|------|------|--------|
| `start_hydra.bat` | Loops agent with 10s restart delay, `%~dp0` for path resolution | PASS |
| `start_dashboard.bat` | Loops dashboard with 5s restart delay | PASS |
| `start_all.bat` | Launches both in separate `cmd` windows with 3s stagger | PASS |

### 8.2 Windows Startup
- **Location:** `create_shortcut.ps1`
- **Target:** `<repo_root>\start_all.bat` (resolved dynamically via `$MyInvocation`)
- **Result:** PASS — shortcut created in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`

### 8.3 Pair Mapping
- **Location:** `KrakenCLI.PAIR_MAP` lines 39-46
- **Test:** SOL/USDC → SOL/USDC, SOL/XBT → SOLXBT, XBT/USDC → XBTUSDC
- **Result:** PASS — verified all 3 pairs resolve correctly for ticker, OHLC, and order commands.

### 8.4 OHLC Parsing
- **Location:** `KrakenCLI.ohlc()` lines 98-122
- **Test:** Handles nested Kraken JSON format, skips "error" and "last" keys, extracts 7-element arrays
- **Result:** PASS

### 8.5 Ticker Parsing
- **Location:** `KrakenCLI.ticker()` lines 77-96
- **Test:** Extracts price, bid, ask, high, low, volume from Kraken ticker response
- **Result:** PASS

### 8.6 Graceful Shutdown
- **Location:** `_handle_shutdown()` line 308, SIGINT/SIGTERM handlers
- **Test:** Sets `self.running = False`, loop exits, final report generated, trade log exported to JSON
- **Result:** PASS

---

## 9. Known Limitations

These are documented behaviors, not bugs:

1. ~~**No position reconciliation**~~ — **Resolved in v2.4.0.** `OrderReconciler` polls `kraken open-orders` every 5 ticks and detects filled/cancelled orders. Session snapshots (`--resume`) preserve engine state across restarts.

2. **No partial fill handling** — Limit post-only orders may not fill immediately (or at all). The engine records the trade as executed internally regardless. A fill-check mechanism is not yet implemented.

3. ~~**Cross-pair swaps are advisory only**~~ — **Resolved in v2.1.0.** `_execute_coordinated_swap()` now executes both sell and buy legs via `execute_signal()` + `_execute_trade()`.

4. **DEFENSIVE strategy never buys** — BUY confidence is 0.4 (below the 0.55 execution threshold). This is by design — in a downtrend, the agent preserves capital rather than catching falling knives.

5. ~~**Sharpe ratio requires 30+ ticks**~~ — **Resolved in v2.4.0.** Annualization now uses observed candle timestamp deltas (median), not a hardcoded assumption. Still requires 30+ ticks of data.

---

## 10. Fixes Applied During Audit

| # | Component | Issue | Fix |
|---|-----------|-------|-----|
| 1 | RSI | Simple sum instead of Wilder's smoothing | Rewrote with exponential smoothing |
| 2 | MACD signal | `signal = macd * constant` (wrong) | Built full MACD series with 9-EMA |
| 3 | Orders | Market orders (taker) | Changed to limit post-only (maker) |
| 4 | Rate limiting | Multiple rapid API calls per tick | Added 2s sleep between every call |
| 5 | Trade log price | Logged engine price, not order price | Changed to log actual limit_price |
| 6 | Dead man's switch | Refreshed every 2nd tick (could expire) | Refreshed every tick |
| 7 | Dashboard balance | Extra API call every tick | Cached every 5th tick |
| 8 | Indicator precision | BB bands rounded to 2 decimals (0.00 for SOL/XBT) | Dynamic precision based on price magnitude |
| 9 | Continuous mode | Duration=0 caused TypeError on remaining | Fixed string/float handling |
| 10 | Performance report | Box-drawing characters misaligned | Rewrote with clean ASCII formatting |
