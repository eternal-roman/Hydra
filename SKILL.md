---
name: hydra-regime-trader
description: >
  HYDRA (Hyper-adaptive Dynamic Regime-switching Universal Agent) is an autonomous
  crypto trading agent for Kraken CLI that detects market regimes and switches between
  four strategies: Momentum, Mean Reversion, Grid, and Defensive. Use when: (1) running
  an adaptive paper or live trading session on BTC, ETH, or SOL via Kraken CLI,
  (2) analyzing current market regime from OHLC data, (3) generating trade signals with
  position sizing, (4) monitoring and reporting on ongoing trading performance. Requires
  kraken-cli installed. NOT for: non-Kraken exchanges, DeFi/on-chain trading, or
  strategies outside the HYDRA framework.
---

# HYDRA — Regime-Adaptive Trading Agent for Kraken CLI

## Overview

HYDRA detects what the market is doing *right now* and selects the optimal strategy for
that condition. Most bots fail because they apply one strategy to all market states.
HYDRA solves this with a four-regime, four-strategy matrix:

| Detected Regime | Selected Strategy | Logic |
|-----------------|-------------------|-------|
| TREND_UP        | MOMENTUM          | Ride the wave — MACD positive, price > EMA20, RSI 30–70 |
| TREND_DOWN      | DEFENSIVE         | Reduce exposure — sell rallies, only buy extreme oversold |
| RANGING         | MEAN_REVERSION    | Buy at lower Bollinger Band, sell at upper |
| VOLATILE        | GRID              | Split orders across Bollinger Band zones |

## Prerequisites

```bash
# Install Kraken CLI
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh

# Verify installation
kraken --version

# For live trading only (paper trading needs no keys):
kraken setup
```

## Core Workflow

### Phase 1: Collect Market Data

```bash
# Get current ticker
kraken ticker BTC/USD -o json

# Get OHLC candles (1-minute interval, for regime detection)
kraken ohlc BTC/USD --interval 1 -o json

# Get OHLC candles (5-minute interval, for confirmation)
kraken ohlc BTC/USD --interval 5 -o json

# Stream live ticks via WebSocket
kraken ws ticker BTC/USD -o json
```

### Phase 2: Detect Regime

Using the OHLC data, compute:
1. **EMA(20)** and **EMA(50)** — trend direction
2. **ATR(14)** — volatility measurement
3. **Bollinger Bands(20, 2)** — band width for regime classification

**Regime Rules:**
- `ATR% > 4%` OR `BB_width > 8%` → **VOLATILE**
- `EMA20 > EMA50 * 1.005` AND `price > EMA20` → **TREND_UP**
- `EMA20 < EMA50 * 0.995` AND `price < EMA20` → **TREND_DOWN**
- Otherwise → **RANGING**

### Phase 3: Generate Signal

Each strategy produces a signal: **BUY**, **SELL**, or **HOLD** with a confidence score (0–1).

**MOMENTUM Strategy:**
- BUY when: RSI 30–70, MACD histogram > 0, price > BB middle. Confidence scales with MACD strength.
- SELL when: RSI > 75 OR MACD histogram crosses negative.

**MEAN_REVERSION Strategy:**
- BUY when: price ≤ BB lower AND RSI < 35. Confidence scales with distance from middle band.
- SELL when: price ≥ BB upper AND RSI > 65.

**GRID Strategy:**
- Divide BB range into 5 zones. BUY in bottom zone, SELL in top zone.

**DEFENSIVE Strategy:**
- BUY only when RSI < 20 (extreme oversold), small position.
- SELL when RSI > 50 (reduce exposure).

### Phase 4: Size Position (Quarter-Kelly)

```
kelly_fraction = max(0, (confidence * 2 - 1)) * 0.25
risk_amount = balance * 0.02  # 2% risk per trade
position_size = (risk_amount * kelly_fraction) / current_price
```

**Hard limits:**
- Never allocate more than 30% of capital to a single trade
- Minimum trade size: $50
- Confidence threshold to execute: 0.55

### Phase 5: Execute Trade

```bash
# Paper trading (no API keys needed)
kraken paper buy BTC/USD --type market --volume 0.001
kraken paper sell BTC/USD --type market --volume 0.001

# Check paper positions
kraken paper positions -o json

# Check paper balance
kraken paper balance -o json

# --- LIVE TRADING (requires API keys + extreme caution) ---
# ALWAYS set dead man's switch first:
kraken order cancel-after 60

# Then execute:
kraken order buy BTC/USD --type market --volume 0.001
kraken order sell BTC/USD --type market --volume 0.001

# Validate without executing:
kraken order buy BTC/USD --type market --volume 0.001 --validate
```

### Phase 6: Monitor & Report

```bash
# Check open orders
kraken open-orders -o json

# Check trade history
kraken trades-history -o json

# Check balance
kraken balance -o json

# Check closed orders
kraken closed-orders -o json
```

## Agent Loop (Pseudocode)

```
INITIALIZE paper session
SET assets = ["BTC/USD", "ETH/USD", "SOL/USD"]
SET interval = 60 seconds
SET max_position_pct = 0.30
SET min_confidence = 0.55

LOOP every {interval}:
  FOR each asset in assets:
    1. FETCH ohlc data: kraken ohlc {asset} --interval 1 -o json
    2. PARSE candles into arrays: opens, highs, lows, closes
    3. COMPUTE indicators: EMA20, EMA50, RSI14, ATR14, BB(20,2), MACD(12,26,9)
    4. DETECT regime using indicator values
    5. SELECT strategy from regime
    6. GENERATE signal (action, confidence, reason)
    7. IF signal.action != HOLD AND signal.confidence >= min_confidence:
         a. COMPUTE position size via quarter-Kelly
         b. CHECK balance: kraken paper balance -o json
         c. VALIDATE trade size against limits
         d. EXECUTE: kraken paper {buy|sell} {asset} --type market --volume {size}
         e. LOG trade with timestamp, price, reason, confidence, strategy
    8. LOG current state: regime, strategy, signal, position, equity

  COMPUTE portfolio metrics:
    - Total equity = cash + sum(position_value)
    - P&L % = (equity - initial) / initial * 100
    - Max drawdown = max historical peak-to-trough
    - Win rate = wins / (wins + losses)
    - Sharpe estimate from rolling returns

  PRINT status summary

  IF max_drawdown > 10%:
    WARN "Drawdown limit approaching — consider reducing exposure"
  IF max_drawdown > 15%:
    HALT "Circuit breaker triggered — stopping agent"
END LOOP
```

## Risk Management Rules

1. **Circuit Breaker**: Stop all trading if max drawdown exceeds 15%
2. **Dead Man's Switch**: Always run `kraken order cancel-after 60` before live orders
3. **Position Limits**: No single position > 30% of portfolio
4. **Trade Threshold**: Only execute when confidence ≥ 0.55
5. **Minimum Size**: Skip trades below $50
6. **Regime Warmup**: Require 50+ candles before generating signals
7. **Rate Limiting**: Respect Kraken API limits — minimum 2s between requests

## Indicator Reference

| Indicator | Formula | Purpose |
|-----------|---------|---------|
| EMA(n)    | close[i] * k + EMA[i-1] * (1-k), k = 2/(n+1) | Trend direction |
| RSI(14)   | 100 - 100/(1 + avg_gain/avg_loss) | Overbought/oversold |
| ATR(14)   | SMA of True Range over 14 periods | Volatility measure |
| BB(20,2)  | middle ± 2*stddev(close, 20) | Price bands & regime |
| MACD      | EMA(12) - EMA(26), signal = EMA(9) of MACD | Momentum |

## Performance Metrics to Track

- **Net P&L** (realized + unrealized)
- **Sharpe Ratio** (annualized from tick returns)
- **Max Drawdown** (peak-to-trough %)
- **Win Rate** (winning trades / total trades)
- **Profit Factor** (gross profit / gross loss)
- **Trades per Hour** (activity level)
- **Regime Detection Accuracy** (compare detected vs. retrospective)

## Example Claude Code Session

```
> Install kraken-cli, then run HYDRA in paper mode on BTC/USD for 10 minutes.
> Use 1-minute OHLC candles. Start with $10,000 paper balance.
> Print a status update every 60 seconds showing:
>   - Current regime and strategy
>   - Signal (action, confidence, reason)
>   - Position and unrealized P&L
>   - Total equity and drawdown
> At the end, print a full performance report with all metrics.
```

## File Structure

```
hydra/
├── SKILL.md              # This file — agent instructions
├── hydra_engine.py       # Strategy engine (indicators, regime detection, signals)
├── hydra_agent.py        # Main agent loop (Kraken CLI integration)
├── hydra_dashboard.jsx   # React dashboard for visualization
└── README.md             # Project overview for hackathon submission
```

## License

MIT
