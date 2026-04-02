# CLAUDE.md — Agent Instructions for HYDRA

This file provides context for Claude Code and other AI agents working on this repository.

## Project Overview

HYDRA is a regime-adaptive crypto trading agent for Kraken. It detects market conditions (trending, ranging, volatile) and switches between four strategies (Momentum, Mean Reversion, Grid, Defensive) to execute limit post-only orders on SOL/USDC, SOL/XBT, and XBT/USDC.

## Repository Structure

```
hydra_engine.py     — Pure Python trading engine (indicators, regime detection, signals, position sizing)
hydra_agent.py      — Live agent (Kraken CLI via WSL, WebSocket broadcast, trade execution)
hydra_brain.py      — AI reasoning: Claude Analyst + Risk Manager + Grok Strategist
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

# Engine test (no API keys needed)
python hydra_engine.py

# Run test suite (67 tests)
python tests/test_engine.py
```

## Working with the Code

### Indicators (hydra_engine.py)
- RSI uses Wilder's exponential smoothing — do not simplify to SMA
- MACD builds a full historical series then applies 9-EMA — do not simplify to single-point calculation
- Bollinger Bands use population variance (divide by N, not N-1)
- All indicators are stateless static methods — they recompute from the full price array each tick

### Regime Detection
- Priority: VOLATILE > TREND_UP > TREND_DOWN > RANGING
- Volatile check must come first — it overrides trend signals
- Warmup requires 50 candles before regime detection activates

### Trading
- Confidence threshold: 0.55 minimum to execute
- Position sizing: Quarter-Kelly (`(confidence*2 - 1) * 0.25 * balance`)
- Circuit breaker: 15% max drawdown halts the engine permanently for the session
- Rate limiting: 2-second minimum between every Kraken API call — do not remove or reduce

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
- Only fires on BUY/SELL signals (HOLD is free, no API call)
- Falls back to engine-only on API failure, budget exceeded, or missing key
- Enable by setting `ANTHROPIC_API_KEY` and/or `XAI_API_KEY` in `.env`
- Cost: ~$3-5/day with Grok escalation on ~20-30% of signals
- Do not change the JSON response format in system prompts — the parser depends on it
- Escalation threshold is parameterized (0.65 conservative, 0.50 competition) — it controls when Grok fires
- Strategist always uses `self.strategist_client` (xAI) — do not route it through primary client

## Testing

Run the full test suite (67 tests):
```bash
python tests/test_engine.py
```

Run the engine synthetic demo (no API keys needed):
```bash
python hydra_engine.py
```

See AUDIT.md for the full verification checklist.

## Common Pitfalls

- Don't add `import numpy` or `import pandas` to the engine — it's intentionally pure Python
- Don't change orders to market type — limit post-only is a deliberate design choice
- Don't reduce rate limiting below 2s — Kraken will throttle or ban
- Don't merge engine instances across pairs — they must remain independent
- The `.env` file contains Kraken API keys — never commit it
- `hydra_trades_*.json` files are runtime trade logs — they're gitignored
