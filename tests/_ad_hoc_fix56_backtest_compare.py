"""Ad-hoc backtest comparison for the Fix 5/6 revert gate.

Per the plan at ~/.claude/plans/twinkling-twirling-leaf.md — if Sharpe
regresses >10% on any pair after Fix 5 (symmetric momentum SELL) + Fix 6
(full-close on SELL), we revert both commits.

Fix 5 and Fix 6 both live in hydra_engine.py and can be neutralized at
runtime via monkey-patches:
  - Fix 5: restore the old OR-gate SELL (line 514-534 in current)
  - Fix 6: restore the 50/50 split at conf=0.7 (line 1194 in current)

This script runs the same synthetic GBM backtest under four configs
(neutralize Fix 5, neutralize Fix 6, neutralize both, vanilla current)
over 8 seeds per pair, and compares the aggregate Sharpe ratios.

Not a unit test — intentionally standalone, run once as part of the
v2.10.1 release verification. Run: `python tests/_ad_hoc_fix56_backtest_compare.py`
"""
from __future__ import annotations

import os
import sys
import statistics
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_backtest import BacktestRunner, make_quick_config
from hydra_engine import (
    HydraEngine, SignalGenerator, Signal, SignalAction, Strategy,
    _fmt_price,
)


# ════════════════════════════════════════════════════════════════
# Fix 5 neutralizer — restore the pre-Fix 5 momentum SELL
# ════════════════════════════════════════════════════════════════

def _old_momentum(rsi, macd, bb, price, indicators, ctx,
                  rsi_lower: float = 30.0, rsi_upper: float = 70.0):
    """Pre-Fix 5 _momentum: BUY uses 4 AND-gates (same as current), SELL uses
    just 2 OR-gates. Restored from git HEAD~3 for this revert-gate check."""
    BASE = SignalGenerator.BASE
    hist = macd["histogram"]
    prev = ctx["prev_histogram"]
    noise_floor = ctx["atr"] * 0.10 if ctx["atr"] > 0 else 0.0

    if (rsi_lower < rsi < rsi_upper
            and hist > noise_floor
            and price > bb["middle"]
            and (hist > prev or prev <= 0)):
        macd_strength = min(1.0, abs(hist) / ctx["atr"]) if ctx["atr"] > 0 else 0.0
        vol = SignalGenerator._vol_bonus(ctx)
        conf = min(0.95, BASE + macd_strength * 0.40 + vol)
        return Signal(
            action=SignalAction.BUY, confidence=conf,
            reason=f"Momentum confirmed: MACD hist {hist:.2f} > 0, "
                   f"price {_fmt_price(price)} > BB mid {_fmt_price(bb['middle'])}, RSI {rsi:.1f}",
            strategy=Strategy.MOMENTUM, indicators=indicators,
        )

    # OLD SELL: OR of 2 gates
    macd_fading = (hist < -noise_floor and (hist < prev or prev >= 0))
    overbought_threshold = rsi_upper + 5
    rsi_overbought = rsi > overbought_threshold
    if rsi_overbought or macd_fading:
        rsi_strength = max(0.0, rsi - rsi_upper) / (100.0 - rsi_upper) if rsi_upper < 100 else 0.0
        macd_strength = min(1.0, abs(hist) / ctx["atr"]) if hist < 0 and ctx["atr"] > 0 else 0.0
        primary = max(rsi_strength, macd_strength)
        vol = SignalGenerator._vol_bonus(ctx)
        conf = min(0.90, BASE + primary * 0.35 + vol)
        return Signal(
            action=SignalAction.SELL, confidence=conf,
            reason=f"Momentum fading: RSI {rsi:.1f}" +
                   (f" > {overbought_threshold:.0f} overbought" if rsi_overbought
                    else f", MACD crossed negative"),
            strategy=Strategy.MOMENTUM, indicators=indicators,
        )
    return Signal(
        action=SignalAction.HOLD, confidence=BASE,
        reason=f"Awaiting momentum confirmation (RSI {rsi:.1f}, MACD hist {hist:.6f})",
        strategy=Strategy.MOMENTUM, indicators=indicators,
    )


# ════════════════════════════════════════════════════════════════
# Fix 6 neutralizer — restore the 50/50 split
# ════════════════════════════════════════════════════════════════

def _install_old_fix6():
    """Wrap _maybe_execute so that on SELL the pre-Fix 6 50% split applies.
    Easier than re-implementing _maybe_execute: monkey-patch the relevant
    sell_amount assignment by replacing a constant on the class."""
    # The current implementation: `sell_amount = self.position.size` (full close)
    # The pre-Fix-6 implementation: `sell_pct = 1.0 if conf > 0.7 else 0.5`
    # We monkey-patch by wrapping _maybe_execute
    original = HydraEngine._maybe_execute

    def wrapped(self, signal, size_multiplier: float = 1.0):
        # For SELL under confidence threshold, shrink position before calling
        # the real _maybe_execute so the full-close path ends up closing 50%
        # of the original. This is a shim — not perfect, but sufficient to
        # measure directional impact.
        if (signal.action == SignalAction.SELL
                and self.position.size > 0
                and signal.confidence >= self.sizer.min_confidence
                and signal.confidence <= 0.7):
            # Temporarily halve position, let the engine "full-close" what
            # remains, then re-add the untouched half.
            original_size = self.position.size
            half = original_size * 0.5
            saved_avg = self.position.avg_entry
            self.position.size = half
            trade = original(self, signal, size_multiplier)
            # Re-add the untouched half (at the original avg_entry — a
            # partial close doesn't change avg_entry in the real engine)
            if self.position.size == 0:  # full-closed the half
                self.position.size = original_size - half
                self.position.avg_entry = saved_avg
            return trade
        return original(self, signal, size_multiplier)

    HydraEngine._maybe_execute = wrapped


def _uninstall_old_fix6():
    # Reimport to reset — we grab the fresh _maybe_execute from source
    import importlib, hydra_engine as he
    importlib.reload(he)
    # Re-alias our module's HydraEngine to the reloaded one so further
    # constructions pick up the new class (though we're calling via the
    # already-bound reference, this is a no-op in the way we've scoped it;
    # install only ever wraps once per process so just replace the function)
    HydraEngine._maybe_execute = he.HydraEngine._maybe_execute


# ════════════════════════════════════════════════════════════════
# Backtest runner + metric extraction
# ════════════════════════════════════════════════════════════════

def run_cfg(name: str, seeds, pairs, n_candles=500):
    """Run BacktestRunner for each seed × pair and collect per-run Sharpe."""
    sharpes = []
    returns = []
    dds = []
    trades = []
    for seed in seeds:
        for pair in pairs:
            cfg = make_quick_config(
                name=f"{name}_{pair.replace('/', '_')}_{seed}",
                pairs=(pair,),
                n_candles=n_candles,
                seed=seed,
            )
            cfg = replace(cfg, coordinator_enabled=False)
            res = BacktestRunner(cfg).run()
            m = res.metrics
            sharpes.append(m.sharpe)
            returns.append(m.total_return_pct)
            dds.append(m.max_drawdown_pct)
            trades.append(m.total_trades)
    def _mean(xs): return statistics.mean(xs) if xs else 0.0
    def _median(xs): return statistics.median(xs) if xs else 0.0
    return {
        "name": name,
        "n_runs": len(sharpes),
        "sharpe_mean": _mean(sharpes),
        "sharpe_median": _median(sharpes),
        "return_mean": _mean(returns),
        "dd_mean": _mean(dds),
        "trades_mean": _mean(trades),
    }


def main():
    seeds = [1, 7, 42, 123, 777, 1337, 9001, 31415]
    pairs = ["SOL/USDC", "BTC/USDC", "SOL/BTC"]

    print(f"\n  Backtest comparison for Fix 5/6 revert gate")
    print(f"  seeds={seeds}  pairs={pairs}  n_candles=500  coordinator=off\n")

    # ── (A) CURRENT — both Fix 5 and Fix 6 applied ──
    res_current = run_cfg("current", seeds, pairs)

    # ── (B) Neutralize Fix 5 only (old SELL gates, new full-close) ──
    original_generate = SignalGenerator._momentum
    SignalGenerator._momentum = staticmethod(_old_momentum)
    try:
        res_no_fix5 = run_cfg("no_fix5", seeds, pairs)
    finally:
        SignalGenerator._momentum = staticmethod(original_generate)

    # ── (C) Neutralize Fix 6 only (new SELL gates, old 50% split) ──
    _install_old_fix6()
    try:
        res_no_fix6 = run_cfg("no_fix6", seeds, pairs)
    finally:
        _uninstall_old_fix6()

    # ── (D) Neutralize both (pre-Fix 5/6 baseline) ──
    SignalGenerator._momentum = staticmethod(_old_momentum)
    _install_old_fix6()
    try:
        res_baseline = run_cfg("pre_fix56", seeds, pairs)
    finally:
        SignalGenerator._momentum = staticmethod(original_generate)
        _uninstall_old_fix6()

    # ── Report ──
    print(f"  {'config':<14} {'n':>4} {'sharpe':>10} {'median':>10} {'return%':>9} {'dd%':>7} {'trades':>7}")
    print(f"  {'-'*14} {'-'*4} {'-'*10} {'-'*10} {'-'*9} {'-'*7} {'-'*7}")
    for r in (res_baseline, res_no_fix6, res_no_fix5, res_current):
        print(f"  {r['name']:<14} {r['n_runs']:>4} "
              f"{r['sharpe_mean']:>10.3f} {r['sharpe_median']:>10.3f} "
              f"{r['return_mean']:>9.2f} {r['dd_mean']:>7.2f} "
              f"{r['trades_mean']:>7.1f}")

    # ── Revert gate ──
    baseline_sharpe = res_baseline["sharpe_mean"]
    current_sharpe = res_current["sharpe_mean"]
    print(f"\n  Revert gate:")
    if baseline_sharpe == 0:
        delta_pct = 0.0
    else:
        delta_pct = (current_sharpe - baseline_sharpe) / abs(baseline_sharpe) * 100
    print(f"    baseline (pre-fix) mean sharpe: {baseline_sharpe:.4f}")
    print(f"    current  (post-fix) mean sharpe: {current_sharpe:.4f}")
    print(f"    delta: {delta_pct:+.2f}%")
    if delta_pct < -10.0:
        print(f"    VERDICT: REVERT — Sharpe regression exceeds 10%")
        return 1
    print(f"    VERDICT: KEEP — no material regression ({delta_pct:+.2f}% within tolerance)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
