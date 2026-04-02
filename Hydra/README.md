# HYDRA — Hyper-adaptive Dynamic Regime-switching Universal Agent

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
┌─────────────────────────────────────────────────────────────┐
│                    HYDRA Agent Loop (30s)                     │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────┐        │
│  │ Kraken   │──>│ Regime   │──>│ Strategy Selector │        │
│  │ CLI OHLC │   │ Detector │   │                   │        │
│  └──────────┘   └──────────┘   │ TREND_UP→MOMENTUM │        │
│                                │ TREND_DN→DEFENSIVE │        │
│  ┌──────────┐   ┌──────────┐  │ RANGING→MEAN_REV  │        │
│  │ Signal   │<──│ Indicator│  │ VOLATILE→GRID      │        │
│  │ Generator│   │ Engine   │  └──────────────────┘        │
│  └────┬─────┘   └──────────┘                               │
│       │                                                     │
│  ┌────▼─────┐   ┌──────────┐   ┌──────────────────┐       │
│  │ Position │──>│ Trade    │──>│ kraken order buy  │       │
│  │ Sizer    │   │ Executor │   │ --type limit      │       │
│  │ (¼Kelly) │   │          │   │ --oflags post     │       │
│  └──────────┘   └──────────┘   └──────────────────┘       │
│       │                                                     │
│  ┌────▼─────┐                                               │
│  │WebSocket │──> React Dashboard (localhost:3001)            │
│  │Broadcast │                                               │
│  └──────────┘                                               │
└─────────────────────────────────────────────────────────────┘
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
| **ATR(14)** | True Range with SMA averaging | Volatility measurement |
| **Bollinger Bands(20, 2)** | Population std dev, width normalized by mean | Price bands and regime classification |
| **MACD(12, 26, 9)** | Full historical MACD series with 9-EMA signal line | Momentum confirmation |

## Position Sizing: Quarter-Kelly Criterion

Every trade is sized using a conservative quarter-Kelly formula:

```
edge = max(0, confidence × 2 - 1)       # 0 at 50% confidence, 1 at 100%
kelly_quarter = edge × 0.25
position_value = kelly_quarter × balance
```

**Hard limits:**
- Maximum single position: **30% of balance**
- Minimum confidence to trade: **55%** (below this, no trade)
- Minimum trade value: **$0.50** (Kraken costmin)
- Kraken minimum order sizes enforced per asset (SOL: 0.02, XBT: 0.00005)

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

## Live Dashboard

React + Vite dashboard at `http://localhost:3001` connected to the agent via WebSocket on port 8765.

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
git clone https://github.com/YOUR_USERNAME/hydra.git
cd hydra/Hydra

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

# Terminal 2: Start the trading agent (runs forever)
python hydra_agent.py --pairs SOL/USDC,SOL/XBT,XBT/USDC --balance 100 --interval 30

# Open http://localhost:3001 in your browser
```

Or use the launcher scripts:

```bash
# Start everything (Windows)
start_all.bat
```

### CLI Options

```
--pairs       Comma-separated trading pairs (default: SOL/USDC,SOL/XBT,XBT/USDC)
--balance     Reference balance for position sizing in USD (default: 100)
--interval    Seconds between ticks (default: 60)
--duration    Total duration in seconds, 0 = forever (default: 0)
--ws-port     WebSocket port for dashboard (default: 8765)
```

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
Hydra/
├── hydra_engine.py        # Core: indicators, regime detection, signals, position sizing
├── hydra_agent.py         # Kraken CLI integration, agent loop, trade execution, WebSocket
├── SKILL.md               # Agent skill definition (Claude Code / MCP compatible)
├── README.md              # This file
├── .env                   # Kraken API keys (not committed)
├── start_all.bat          # Launch agent + dashboard
├── start_hydra.bat        # Agent with auto-restart
├── start_dashboard.bat    # Dashboard with auto-restart
├── create_shortcut.ps1    # Windows Startup shortcut creator
└── dashboard/
    ├── index.html          # Entry point
    ├── package.json        # React 19 + Vite 8
    ├── vite.config.js      # Dev server config
    ├── public/
    │   └── favicon.svg     # Three-headed Hydra icon
    └── src/
        ├── main.jsx        # React root
        ├── App.jsx         # Full dashboard (single-file, inline styles)
        ├── App.css         # Empty (all styles inline)
        └── index.css       # Base styles, fonts, scrollbar, pulse animation
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

3. **Quarter-Kelly sizing** — Full Kelly is mathematically optimal but practically dangerous. Quarter-Kelly sacrifices some expected return for dramatically lower variance and ruin probability.

4. **Circuit breaker at 15%** — No exceptions. An autonomous agent that can't stop itself is a liability.

5. **Regime detection over prediction** — HYDRA doesn't try to predict where the market is going. It detects *what the market is currently doing* and responds appropriately.

6. **One engine per pair** — Each pair runs its own independent regime detector, signal generator, and position tracker. No cross-contamination.

7. **Dead man's switch** — If the agent crashes, all open orders cancel within 60 seconds. Refreshed every tick.

## Risk Disclaimer

**This is experimental software. Not financial advice.**

- Trading crypto involves significant risk of loss
- Past performance does not guarantee future results
- Never trade with money you can't afford to lose
- The dead man's switch and circuit breaker are safety nets, not guarantees
- Always use least-privilege API keys

## License

MIT
