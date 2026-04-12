# HYDRA — Hyper-adaptive Dynamic Regime-switching Universal Agent

[![CI](https://github.com/eternal-roman/Hydra/actions/workflows/ci.yml/badge.svg)](https://github.com/eternal-roman/Hydra/actions/workflows/ci.yml)

A multi-strategy crypto trading agent that detects market regimes in real-time and switches between four optimized strategies. Built for [Kraken](https://www.kraken.com) via [kraken-cli](https://github.com/krakenfx/kraken-cli), with a live React dashboard.

## The Problem

Most trading bots apply **one strategy** to all market conditions. Momentum bots bleed during ranges. Mean-reversion bots get steamrolled by trends. Grid bots implode during breakouts.

Markets aren't static. Your strategy shouldn't be either.

## The Solution

HYDRA detects **what the market is doing right now** and selects the optimal strategy:

| Market Regime | Detection Method | Active Strategy | Logic |
|--------------|-----------------|----------------|-------|
| **Trending Up** | EMA20 > EMA50 x 1.005, price > EMA20 | **Momentum** | Ride the wave — MACD histogram > 0, RSI 30-70, price > BB middle |
| **Trending Down** | EMA20 < EMA50 x 0.995, price < EMA20 | **Defensive** | Preserve capital — only buy extreme oversold (RSI < 20), sell rallies |
| **Ranging** | No clear trend direction | **Mean Reversion** | Buy at lower Bollinger Band (RSI < 35), sell at upper (RSI > 65) |
| **Volatile** | ATR > 4% or BB width > 8% | **Grid** | Split orders across 5 Bollinger Band zones |

Volatility is checked first — it overrides trend detection. This prevents false trend signals during chaotic markets.

## Architecture

```
HYDRA Agent Loop (5-min candles, ~305s tick)
============================================

  Kraken CLI OHLC ──> Regime Detector ──> Strategy Selector
                                          TREND_UP  → MOMENTUM
                                          TREND_DN  → DEFENSIVE
  Signal Generator <── Indicator Engine   RANGING   → MEAN_REV
       │                                  VOLATILE  → GRID
       │
  Position Sizer ──> Trade Executor ──> kraken order buy
  (Quarter/Half-Kelly)                   --type limit
                                         --oflags post
       │
  WebSocket ──> React Dashboard (localhost:3000)
```

## Trading Pairs

| Pair | Description |
|------|-------------|
| **SOL/USDC** | Primary — SOL priced in stablecoin |
| **SOL/XBT** | Cross — SOL priced in BTC, enables regime-driven rotation |
| **XBT/USDC** | BTC priced in stablecoin, completes the triangle |

## Technical Indicators

All indicators are implemented in pure Python with no external dependencies.

| Indicator | Implementation | Purpose |
|-----------|---------------|---------|
| **EMA(20, 50)** | SMA-seeded exponential moving average | Trend direction |
| **RSI(14)** | Wilder's exponential smoothing (not simple average) | Overbought/oversold |
| **ATR(14)** | True Range with Wilder's exponential smoothing | Volatility measurement |
| **Bollinger Bands(20, 2)** | Population std dev, width normalized by mean | Price bands and regime classification |
| **MACD(12, 26, 9)** | Full historical MACD series with 9-EMA signal line | Momentum confirmation |

## Position Sizing: Kelly Criterion

Every trade is sized using a Kelly fraction, with two modes:

```
edge = max(0, confidence × 2 - 1)       # 0 at 50% confidence, 1 at 100%
kelly = edge × multiplier                # 0.25 (conservative) or 0.50 (competition)
position_value = kelly × balance
```

| Mode | Multiplier | Min confidence | Max position |
|---|---|---|---|
| **Conservative** *(default)* | 0.25 quarter-Kelly | 55% | 30% of balance |
| **Competition** | 0.50 half-Kelly | 50% | 40% of balance |

**Exchange minimums enforced on both buy and sell paths:**
- Pair-aware Kraken `ordermin` (SOL: 0.02, XBT: 0.00005, ETH: 0.001)
- Pair-aware Kraken `costmin` (USDC/USD: 0.5, XBT: 0.00002)
- Partial sells below ordermin are auto-upgraded to full close to prevent dust

## Order Execution

All orders are **limit post-only** (maker orders):
- BUY orders placed at the current **bid** price
- SELL orders placed at the current **ask** price
- `--oflags post` ensures the order sits on the book and never crosses the spread
- Orders are validated before execution via `--validate`

This means lower fees (maker rate) and no slippage from market orders.

## Risk Management

| Safety Feature | Implementation |
|----------------|----------------|
| **Circuit Breaker** | Halts all trading if max drawdown exceeds **15%** |
| **Dead Man's Switch** | `kraken order cancel-after 60` refreshed every tick — if agent dies, all open orders cancel in 60 seconds |
| **Rate Limiting** | Minimum **2 seconds** between every Kraken API call |
| **Validation** | Every order is validated via `--validate` before real execution |
| **Graceful Shutdown** | Ctrl+C triggers SIGINT handler → final performance report → trade log export |

## AI Brain — 3-Agent Reasoning Pipeline

Every BUY/SELL signal passes through a multi-agent AI pipeline before execution:

```
Engine signal (BUY/SELL)
  → Agent 1: Market Analyst (Claude Sonnet) — thesis, conviction, agreement
  → Agent 2: Risk Manager (Claude Sonnet) — CONFIRM / ADJUST / OVERRIDE
  → Agent 3: Strategic Advisor (Grok 4 Reasoning) — only on contested decisions
  → Execute or skip
```

| Agent | Model | When | Cost |
|-------|-------|------|------|
| Market Analyst | Claude Sonnet | Every BUY/SELL signal | ~$0.004 |
| Risk Manager | Claude Sonnet | Every BUY/SELL signal | ~$0.004 |
| Strategic Advisor | Grok 4 Reasoning | Only when ADJUST/OVERRIDE or conviction < 65% | ~$0.003 |

**Grok escalation:** When the Analyst and Risk Manager disagree, or the Analyst's conviction is low, Grok 4 is called as the final decision-maker with full context from both prior agents. Clear CONFIRM signals skip Grok entirely.

**Fallback:** If AI is unavailable (API failure, budget exceeded, no key), the system falls back to engine-only mode. Trading continues without interruption.

Enable by setting API keys in `.env`:
```
ANTHROPIC_API_KEY=sk-ant-api03-...
OPENAI_API_KEY=sk-...
XAI_API_KEY=xai-...
```

## Live Dashboard

React + Vite dashboard at `http://localhost:3000` connected to the agent via WebSocket on port 8765.

**Components:**
- **Header** — Hydra logo, LIVE TRADING badge, connection status with tick counter, session elapsed time
- **Stats Row** — Total Balance, P&L %, Max Drawdown, Trade Count, Win Rate
- **Per-Pair Panels** — Live price, regime indicator (color-coded dot), active strategy with icon, candlestick chart (80 candles), signal confidence bar, signal reason, position size with unrealized P&L, per-pair balance
- **Indicator Row** — RSI (colored at 30/70 thresholds), MACD histogram (green/red), Bollinger Band range, BB width (highlighted above 6%)
- **Balance History** — Running equity curve across all pairs
- **Trade Log** — Scrollable reverse-chronological log with status icons, BUY/SELL coloring, smart price formatting
- **Sidebar** — Kraken account balances, strategy matrix showing which pairs are in which regime, per-pair stats (trades, win rate, Sharpe, drawdown), session configuration

## Quick Start

### Prerequisites

- **Python 3.10+** with `websockets` package
- **Node.js 18+** with npm
- **WSL (Ubuntu)** with [kraken-cli](https://github.com/krakenfx/kraken-cli) installed
- **Kraken API keys** configured via `kraken setup` in WSL

### Installation

```bash
# Clone the repository
git clone https://github.com/eternal-roman/Hydra.git
cd Hydra

# Install Python dependencies
pip install websockets

# Install dashboard dependencies
cd dashboard
npm install
cd ..
```

### Running

```bash
# Terminal 1: Start the dashboard
cd dashboard && npm run dev

# Terminal 2: Start the trading agent (5-min candles, runs forever)
python hydra_agent.py --pairs SOL/USDC,SOL/XBT,XBT/USDC --balance 100

# Open http://localhost:3000 in your browser
```

Or use the launcher scripts:

```bash
# Start everything (Windows)
start_all.bat
```

### CLI Options

```
--pairs            Comma-separated trading pairs (default: SOL/USDC,SOL/XBT,XBT/USDC)
--balance          Reference balance for position sizing in USD (default: 100)
--candle-interval  OHLC candle period in minutes: 1, 5, 15, 30, 60 (default: 5)
--interval         Seconds between ticks (default: auto from candle interval)
--duration         Total duration in seconds, 0 = forever (default: 0)
--ws-port          WebSocket port for dashboard (default: 8765)
--mode             Sizing mode: conservative (quarter-Kelly) or competition (half-Kelly)
--paper            Use paper trading — no API keys needed, no real money
--resume           Restore engine/coordinator state from hydra_session_snapshot.json
--reset-params     Reset all learned tuning parameters to defaults
```

### Competition Mode

For the [AI Trading Agents hackathon](https://lablab.ai/ai-hackathons/ai-trading-agents) ($55k prize pool, March 30 — April 12, 2026):

```bash
# Paper trade first to validate strategy
python hydra_agent.py --mode competition --paper

# Go live with competition sizing
python hydra_agent.py --mode competition
```

Competition mode uses half-Kelly (2x position sizes), 50% confidence threshold (trades more often), and 40% max position. On shutdown, exports `competition_results_{timestamp}.json` with full PnL proof.

| Setting | Conservative | Competition |
|---------|-------------|-------------|
| Kelly multiplier | 0.25 (quarter) | 0.50 (half) |
| Min confidence | 55% | 50% |
| Max position | 30% | 40% |

### Engine Demo (No Kraken Required)

```bash
python hydra_engine.py
```

Runs 300 ticks of synthetic price data through the full engine — regime detection, signal generation, trade execution, and performance report. No API keys needed.

## Auto-Restart & Startup

HYDRA includes Windows launcher scripts with automatic restart on crash:

| File | Purpose |
|------|---------|
| `start_all.bat` | Launches both agent and dashboard in separate windows |
| `start_hydra.bat` | Agent with auto-restart loop (10s delay between restarts) |
| `start_dashboard.bat` | Dashboard with auto-restart loop (5s delay) |

A Windows Startup shortcut is placed at:
```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\HYDRA.lnk
```
This launches `start_all.bat` automatically on login.

## File Structure

```
hydra/
├── .gitignore
├── LICENSE
├── README.md               # This file
├── CLAUDE.md               # Agent instructions for Claude Code
├── CHANGELOG.md            # Version history
├── AUDIT.md                # Technical audit and test results
├── SKILL.md                # Agent skill definition (Claude Code / MCP compatible)
├── .env                    # API keys: Kraken, Anthropic, xAI (not committed)
├── hydra_engine.py         # Core: indicators, regime detection, signals, position sizing
├── hydra_brain.py          # AI reasoning: Claude Analyst + Risk Manager + Grok Strategist
├── hydra_agent.py          # Kraken CLI integration, agent loop, trade execution, WebSocket, execution stream, WS market data streams, --resume
├── hydra_journal_migrator.py # Legacy trade log → order journal migration
├── hydra_tuner.py          # Self-tuning parameters via Bayesian updating
├── start_all.bat           # Launch agent + dashboard
├── start_hydra.bat         # Agent with auto-restart
├── start_dashboard.bat     # Dashboard with auto-restart
├── create_shortcut.ps1     # Windows Startup shortcut creator
├── tests/
│   ├── test_engine.py       # Core engine tests
│   ├── test_cross_pair.py   # Cross-pair coordinator tests
│   ├── test_order_book.py   # Order book analyzer tests
│   ├── test_tuner.py        # Self-tuning parameter tests
│   ├── test_balance.py      # Balance & asset conversion tests
│   ├── test_kraken_cli.py   # KrakenCLI wrapper tests (args, precision, fees)
│   ├── test_execution_stream.py  # ExecutionStream health + auto-restart
│   ├── test_status_gate.py       # System status gate (maintenance, degradation)
│   ├── test_pair_constants.py    # Dynamic pair constants (load, apply, fallback)
│   ├── test_reconciliation.py    # Restart-gap reconciliation
│   ├── test_resume_reconcile.py  # Resume reconciliation (stale PLACED entries)
│   ├── test_candle_stream.py     # CandleStream (ws ohlc) dispatch + storage
│   ├── test_ticker_stream.py     # TickerStream (ws ticker) dispatch + storage
│   ├── test_balance_stream.py    # BalanceStream (ws balances) normalization
│   ├── test_book_stream.py       # BookStream (ws book) dispatch + conversion
│   ├── test_pnl_reconcile.py     # P&L reconciliation (trades-history matching)
│   └── live_harness/        # Live-execution test harness (41+ scenarios)
│       ├── harness.py       # Harness class, CLI entry, harness_execute wrapper
│       ├── scenarios.py     # All scenarios + ALL_SCENARIOS registry
│       ├── schemas.py       # Per-status trade log entry schemas
│       ├── state_comparator.py  # 13-field rollback comparator
│       ├── stubs.py         # StubRun + Kraken response builders
│       └── README.md        # Catalog, findings tracker, authoring guide
└── dashboard/
    ├── index.html           # Entry point
    ├── package.json         # React 19 + Vite
    ├── vite.config.js       # Dev server config (port 3000, strictPort)
    ├── public/
    │   └── favicon.svg      # Three-headed Hydra icon
    └── src/
        ├── main.jsx         # React root
        ├── App.jsx          # Full dashboard (single-file, inline styles)
        ├── App.css          # Empty (all styles inline)
        └── index.css        # Base styles, fonts, scrollbar, pulse animation
```

## Performance Metrics

HYDRA tracks and reports per pair:
- **Net P&L** (realized + unrealized)
- **Sharpe Ratio** (annualized from tick returns)
- **Maximum Drawdown** (peak-to-trough %)
- **Win Rate** (winning sells / total sells)
- **Profit Factor** (gross profit / gross loss)
- **Trade Count** per session

## Key Design Decisions

1. **Pure Python, zero dependencies** — `hydra_engine.py` uses only the standard library. No numpy, no pandas. Portable, auditable, fast to deploy.

2. **Limit post-only orders** — Never cross the spread. All orders sit on the book at bid (buy) or ask (sell). Lower fees, no slippage.

3. **Kelly fraction sizing** — Full Kelly is mathematically optimal but practically dangerous. Conservative mode uses quarter-Kelly (0.25) for low variance and ruin probability; competition mode uses half-Kelly (0.50) for higher returns with acceptable risk in short-horizon tournament play.

4. **Circuit breaker at 15%** — No exceptions. An autonomous agent that can't stop itself is a liability.

5. **Regime detection over prediction** — HYDRA doesn't try to predict where the market is going. It detects *what the market is currently doing* and responds appropriately.

6. **One engine per pair** — Each pair runs its own independent regime detector, signal generator, and position tracker. No cross-contamination.

7. **Dead man's switch** — If the agent crashes, all open orders cancel within 60 seconds. Refreshed every tick.

## Testing

```bash
python tests/test_engine.py            # Indicators, regime, signals, sizing, circuit breaker
python tests/test_cross_pair.py        # Cross-pair coordinator rules
python tests/test_order_book.py        # Depth analyzer, imbalance, walls
python tests/test_tuner.py             # Self-tuning Bayesian updates
python tests/test_balance.py           # Staked asset, USD conversion, balance init
python tests/test_kraken_cli.py        # KrakenCLI wrappers, price precision, fee parsing
python tests/test_execution_stream.py  # ExecutionStream health + auto-restart cooldown
python tests/test_status_gate.py       # System status gate (maintenance, degradation, transitions)
python tests/test_pair_constants.py    # Dynamic pair constants (load, apply, fallback)
python tests/test_reconciliation.py    # Restart-gap reconciliation (query-orders recovery)
python tests/test_resume_reconcile.py  # Resume reconciliation (stale PLACED from previous sessions)
python tests/test_candle_stream.py     # CandleStream (ws ohlc) dispatch, storage, symbol mapping
python tests/test_ticker_stream.py     # TickerStream (ws ticker) dispatch, storage, symbol mapping
python tests/test_balance_stream.py    # BalanceStream (ws balances) dispatch, normalization, filtering
python tests/test_book_stream.py       # BookStream (ws book) dispatch, REST-format conversion
python tests/test_pnl_reconcile.py     # P&L reconciliation (trades-history matching)
python hydra_engine.py                 # Synthetic 300-tick demo (no API keys needed)
```

### Live-execution test harness

`tests/live_harness/` drives `HydraAgent._place_order` across 41+ scenarios
(happy, failure, edge, schema, rollback, historical regression, WS execution
stream lifecycle transitions, real Kraken).
It is the canonical validation tool for any change to the execution path.

```bash
python tests/live_harness/harness.py --mode smoke    # ~1.5s, import + agent
python tests/live_harness/harness.py --mode mock     # ~1.5s, 33+ scenarios (default)
python tests/live_harness/harness.py --mode validate # ~10s, real Kraken read-only
python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders
```

`smoke` and `mock` run in CI on every PR. See
[tests/live_harness/README.md](tests/live_harness/README.md) for the scenario
catalog, findings tracker (HF-### IDs), and authoring guide.

See **[AUDIT.md](AUDIT.md)** for the v1.0-era technical audit report
(indicators, regime detection, signal generation, position sizing,
order execution, dashboard components, infrastructure) and
**[CHANGELOG.md](CHANGELOG.md)** for version-by-version history.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `kraken: command not found` | Install kraken-cli in WSL: `curl --proto '=https' --tlsv1.2 -LsSf https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh \| sh` |
| `wsl: not found` or WSL errors | Ensure WSL is installed with Ubuntu: `wsl --install -d Ubuntu` |
| Port 3000 in use | `strictPort: true` in `vite.config.js` — Vite will fail instead of auto-picking. Kill the blocking process: `npx kill-port 3000` or change the port in vite.config.js |
| Port 8765 in use | Stop any running agent, or change port: `--ws-port 8766` |
| `websockets` not installed | `pip install websockets` |
| Agent shows `Empty response` | Verify kraken-cli works: `wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken ticker SOL/USDC -o json"` |
| Dashboard shows "DISCONNECTED" | Ensure agent is running — it hosts the WebSocket server on port 8765 |
| Dashboard hosted on a different machine | Set `VITE_HYDRA_WS_URL=ws://agent-host:8765` before `npm run build` or `npm run dev`. Default is `ws://localhost:8765`. |
| No trades executing | Normal if market is ranging with low confidence. Check signal confidence in dashboard — needs to exceed 55% |

## SKILL.md

`SKILL.md` is an agent skill definition file compatible with Claude Code and other MCP-compatible agents. It contains the full specification for HYDRA's trading logic, enabling AI coding assistants to understand, operate, and modify the agent. You can point any MCP agent at this file to give it context on how HYDRA works.

## Risk Disclaimer

**This is experimental software. Not financial advice.**

- Trading crypto involves significant risk of loss
- Past performance does not guarantee future results
- Never trade with money you can't afford to lose
- The dead man's switch and circuit breaker are safety nets, not guarantees
- Always use least-privilege API keys

## License

MIT
