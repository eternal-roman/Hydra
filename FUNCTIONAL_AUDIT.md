# HYDRA Functional & Product Audit
**Date:** 2026-04-05  
**Branch:** claude/trading-system-audit-R5u83  
**Scope:** End-to-end functional, financial, and agentic capability audit

---

## Audit Verdict

### Critical Issues (user will lose money or data)

**1. Engine state never reconciles with exchange fills**  
`hydra_agent.py:640-664` — `execute_signal()` updates the engine's balance and position *before* submitting the Kraken order. If the order fails (network error, rejection, insufficient funds), there is no rollback. The engine permanently believes it holds a position it doesn't hold. All subsequent signals, sizes, and P&L are computed against a phantom position. No code exists to query open orders or fills on startup or after a failed order.

**2. No fee deduction — all P&L is fictional**  
`hydra_engine.py` (entire file) — Kraken charges 0.16% maker fee per post-only limit order. No fee is ever subtracted from balance, added to cost basis, or deducted from P&L. A system executing 50 round-trips incurs ~16% fee drag that is invisible to the user. Every P&L figure displayed — in the dashboard, the performance report, and the competition results export — is pre-fee gross P&L. Net P&L is systematically overstated.

**3. P&L uses signal price, not fill price**  
`hydra_engine.py:903, 937` — Cost basis is set to `self.prices[-1]` (last OHLC close at tick time). The actual limit order is placed at `ticker["bid"]` fetched 2+ seconds later via `_execute_trade`. These prices can differ. More importantly, limit orders may never fill if price moves away — yet the engine records the trade immediately as executed. There is no fill confirmation loop anywhere in the codebase.

**4. Auto-restart resets the circuit breaker**  
`start_hydra.bat:10-17` — The Windows launcher restarts the agent 10 seconds after any exit, including a circuit breaker halt. Each restart creates a fresh `HydraEngine` with `max_drawdown = 0.0`. A user who loses 15% and triggers the breaker will have trading resume automatically in 10 seconds with no memory of the previous session's losses.

---

### Major Issues (product claims are false or misleading)

**5. "Bayesian updating" is not Bayesian**  
`hydra_tuner.py:144-145` — The update formula is `new_val = old_val + 0.10 * (win_mean - old_val)`. This is gradient descent toward the mean of winning trades. There is no prior, no likelihood, no posterior. The CLAUDE.md, class docstring, and README all call this "Bayesian updating." It is not.

**6. Defensive BUY signal can never execute**  
`hydra_engine.py:437-441` — `_defensive()` returns a BUY with `confidence=0.4`. The minimum threshold is `0.55` (conservative) and `0.50` (competition). `_maybe_execute()` filters at line 901: `confidence >= self.sizer.min_confidence`. The BUY never passes. The TREND_DOWN regime is effectively SELL-only. The comment "cautious nibble" describes a trade that is permanently blocked.

**7. Sharpe ratio is arithmetically inflated**  
`hydra_engine.py:1143-1160` — Annualization factor is `sqrt(525600 / candle_interval)`. For 5-minute candles: `sqrt(105120) ≈ 324`. A per-tick mean return of 0.03% with std of 0.3% produces Sharpe = 0.1 × 324 = **32.4**. No risk-free rate is subtracted. The displayed Sharpe is not a meaningful financial metric. Any positive-edge strategy will show values that would make it the best-performing fund in history.

**8. Risk Manager drawdown rule is an LLM instruction, not a code constraint**  
`hydra_brain.py:86` — System prompt says "NEVER allow a trade when drawdown exceeds 10%." This depends on the LLM reading the drawdown value correctly and complying. Between 10–15% drawdown, the only protection is LLM compliance. The code-enforced circuit breaker fires only at 15%.

**9. `total_trades` double-counts round-trips**  
`hydra_engine.py:919, 951` — BUY increments `total_trades`, and full position close increments it again. One round-trip = 2 total trades. The performance report shows "Total Trades: 10, Wins: 3, Losses: 2" which implies 5 unresolved trades when all 5 round-trips are resolved. Win rate denominator is `win_count + loss_count`, not `total_trades`, creating a misleading inconsistency.

---

### Moderate Issues (workflows break under real conditions)

**10. No 429 detection or exponential backoff**  
`hydra_agent.py:96-115` — Rate limiting is fixed `time.sleep(2)` calls. A 429 response from Kraken returns as `{"error": "..."}` and the system skips the action silently. There is no backoff, no 429 detection, and no escalation. Sustained 429s could lead to an IP ban with no user notification.

**11. Circuit breaker halt is invisible on dashboard**  
`dashboard/src/App.jsx` — No code in the dashboard handles `state.pairs[pair].halted` or `halt_reason`. When the circuit breaker fires, the pair shows a HOLD signal with the halt reason buried in the signal reason text. There is no visual alert, no color change, no badge. A user monitoring the dashboard will not know trading has permanently stopped for that pair.

**12. Paper trading uses market orders; live uses limit orders**  
`hydra_agent.py:246-254` — `_execute_paper_trade` calls `KrakenCLI.paper_buy/sell` which uses `--type market`. Live trading uses limit post-only. Paper results will show market-fill execution with no spread cost, while live trading uses limit orders that may not fill. Paper mode does not test the live execution path.

**13. Brain timeout (10s) risks stale execution on 1-minute candles**  
`hydra_brain.py:362` — Anthropic timeout is 10 seconds per LLM call. Three sequential calls (Analyst → Risk Manager → Strategist) can take 30+ seconds. With `--candle-interval 1`, the market has moved half a candle by execution time. The system does not check candle age before executing the brain-approved signal.

---

### Minor Issues (UX friction, missing polish)

**14. Stale data displayed without indication when WebSocket disconnects**  
`App.jsx:189` — Dashboard reconnects after 3 seconds on disconnect, and shows "DISCONNECTED" banner. However, all pair panels continue showing the last-received prices and signals without any visual stale indicator. A user could see a 5-minute-old price as if current.

**15. `retryable: True` flag is set but never consumed**  
`hydra_agent.py:111` — `KrakenCLI._run()` sets `{"error": "Command timed out", "retryable": True}` on timeout. No caller checks the `retryable` key. Retryable errors are treated identically to permanent errors.

**16. Trade log capped at 20 entries in dashboard**  
`hydra_agent.py:1164` — `self.trade_log[-20:]` is sent to the dashboard. Full history is only visible in the exported JSON. Users cannot audit older trades from the UI.

---

## Per-Feature Report

### Regime Detection
- **Claimed:** Detects VOLATILE/TREND_UP/TREND_DOWN/RANGING with priority ordering
- **Verified Status:** VERIFIED
- **Evidence:** `hydra_engine.py:239-263` — priority ordering correct, warmup enforced at 50 candles

### 4-Strategy Engine
- **Claimed:** MOMENTUM/MEAN_REVERSION/GRID/DEFENSIVE strategies
- **Verified Status:** PARTIAL
- **Evidence:** All 4 implemented. Defensive BUY is unreachable (confidence 0.4 < min 0.55). See Issue #6.

### Quarter-Kelly Position Sizing
- **Claimed:** `edge = (confidence × 2 - 1) × kelly_multiplier × balance`
- **Verified Status:** VERIFIED
- **Evidence:** `hydra_engine.py:506-513` — formula matches documentation

### AI Brain (3-agent pipeline)
- **Claimed:** Claude Analyst + Claude Risk Manager + Grok Strategist
- **Verified Status:** VERIFIED (with caveats)
- **Evidence:** Real API calls in `hydra_brain.py:335-385`. Brain output modifies signal at `hydra_agent.py:792-798`. Pipeline is sequential, not parallel. Risk Manager drawdown constraint is LLM-only (Issue #8).

### Self-Tuning Parameters
- **Claimed:** "Bayesian updating of regime/signal thresholds"
- **Verified Status:** MISLEADING
- **Evidence:** `hydra_tuner.py:144` — 10% shift toward winning-trade mean. Not Bayesian.

### Circuit Breaker
- **Claimed:** Halts trading at 15% drawdown
- **Verified Status:** PARTIAL
- **Evidence:** Code enforcement verified at `hydra_engine.py:888-890`. Auto-restart bypasses it (Issue #4). Dashboard does not surface halt state (Issue #11).

### Dead Man's Switch
- **Claimed:** `cancel-after 60` refreshed every tick
- **Verified Status:** VERIFIED
- **Evidence:** `hydra_agent.py:519` — refreshed at top of each tick in live mode

### Fee Handling
- **Claimed:** "Lower fees (maker rate)" — implies fee-awareness
- **Verified Status:** MISSING
- **Evidence:** No `fee` variable or fee deduction anywhere in `hydra_engine.py`. P&L is pre-fee throughout.

### Backtesting
- **Claimed:** Not explicitly claimed. README references paper mode as validation.
- **Verified Status:** MISSING
- **Evidence:** No backtesting engine exists. The `python hydra_engine.py` demo runs synthetic forward data, not historical replay with proper out-of-sample methodology.

---

## Agentic Tier Assessment

- **Claimed Tier:** Tier 4 implied ("autonomous", "regime-adaptive", "self-tuning", "AI reasoning")
- **Verified Tier:** Tier 3 (Deliberative Agent)
- **Evidence for Tier 3:** Has goal representation (drawdown limits, circuit breaker), multi-step planning (regime → strategy → signal → brain → execute), feedback loop (tuner, equity tracking), reasoning trace (AI decisions in state), kill switch enforced in code
- **Missing for Tier 4:**
  - No position reconciliation with exchange on restart
  - No detection of order-fill discrepancy
  - No error recovery when engine/exchange state diverges
  - Auto-restart defeats the circuit breaker (a Tier 4 system would not resume after breaker fire without human confirmation)

---

## Product Maturity Scorecard

| Dimension | Rating | Evidence |
|-----------|--------|----------|
| Error Handling | MVP | Try/catch, logs failures, no rollback on order fail |
| Configuration | MVP | CLI args + .env, restart to apply changes |
| Observability | MVP | Console logs + WebSocket dashboard, no metrics, no alerts |
| Testing | MVP | 191 unit tests for engine logic, no integration tests against exchange sandbox, no E2E |
| Documentation | MVP | CLAUDE.md + README cover architecture; no runbook for failure scenarios |
| Deployment | Prototype | Manual launch + .bat scripts, no CI/CD for the agent |
| Data Integrity | Prototype | In-memory state + rolling JSON trade log, no crash recovery |
| Regulatory Awareness | Prototype | Single disclaimer in performance report |

---

## What This Product Actually Is vs. What It Claims To Be

HYDRA is a rules-based regime-switching trading bot with genuine LLM integration for signal review and a gradient-descent parameter tuner. Its regime detection, signal generation, order submission, and AI brain wiring are functionally implemented and correctly connected. The system will detect regimes, generate signals, consult Claude/Grok, and submit limit orders to Kraken — these claims are true.

What it is not: the P&L it reports is pre-fee and based on theoretical signal prices, not actual fills. When a live order fails, the engine's internal state corrupts silently with no recovery path. The "Bayesian" tuner is a misnomer for a simple heuristic. The Sharpe ratio formula produces numbers in the range of 20–100 for any positive-edge strategy, rendering it meaningless as a risk metric. The circuit breaker — the primary capital protection mechanism — is defeated by the auto-restart launcher that ships with the product. A user who trusts the displayed P&L, Sharpe, or trade count is operating on false information.
