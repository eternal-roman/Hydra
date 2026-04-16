"""Drift regression for the backtester (I7).

Zero-drift invariant: running the backtester on a fixed candle sequence must
produce the same per-tick (regime, signal.action, signal.confidence,
position.size, balance) as invoking HydraEngine directly with the same inputs.

Phase 1 pins the engine+coordinator path. Phase 6 extends drift coverage to
the modifier chain (order book / FOREX session / brain) once the live
modifier logic is factored out of hydra_agent.py.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_engine import HydraEngine, SIZING_CONSERVATIVE  # noqa: E402
from hydra_backtest import (  # noqa: E402
    BacktestRunner,
    SyntheticSource,
    make_quick_config,
)


class TestZeroDrift(unittest.TestCase):
    """The backtester's tick-by-tick engine outputs must match a direct
    HydraEngine loop on the same candles and params (I7).

    We reproduce the backtester's per-pair path: ingest_candle → tick(generate_only)
    → execute_signal (when applicable). Post-only fill semantics are the new
    behavior the backtester adds on top; drift test excludes fill-bound equity
    (which is affected by post-only rejection / fee deduction) and pins only
    signal-layer + pre-fill engine decisions.
    """

    def _collect_direct(self, candles, candle_interval=15):
        """Run a single HydraEngine through the candles and collect per-tick state."""
        engine = HydraEngine(
            initial_balance=100.0,
            asset="SOL/USDC",
            sizing=SIZING_CONSERVATIVE,
            candle_interval=candle_interval,
        )
        states = []
        for c in candles:
            engine.ingest_candle({
                "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                "volume": c.volume, "timestamp": c.timestamp,
            })
            s = engine.tick(generate_only=True)
            states.append({
                "regime": s.get("regime"),
                "strategy": s.get("strategy"),
                "action": s.get("signal", {}).get("action"),
                "confidence": round(s.get("signal", {}).get("confidence", 0.0), 9),
            })
        return states

    def test_signal_layer_matches_direct_engine_single_pair(self):
        source = SyntheticSource(kind="gbm", n_candles=250, seed=17)
        candles = list(source.iter_candles("SOL/USDC"))

        direct_states = self._collect_direct(candles)

        # Same candles through the backtester — single-pair, coordinator disabled to
        # isolate pure engine behavior (coordinator requires ≥2 pairs to issue overrides)
        cfg = make_quick_config(name="drift", n_candles=250, seed=17)
        # Disable coordinator explicitly
        from dataclasses import replace
        cfg = replace(cfg, coordinator_enabled=False)
        runner = BacktestRunner(cfg)
        bt_states_collected = []

        # Hook into the runner by monkey-patching on_tick to capture per-tick signals.
        # Cleaner than re-reading trade_log because it includes HOLD ticks.
        def capture(state):
            for _pair, pair_state in state.get("pairs", {}).items():
                sig = pair_state.get("signal", {})
                bt_states_collected.append({
                    "regime": pair_state.get("regime"),
                    "strategy": pair_state.get("strategy"),
                    "action": sig.get("action"),
                    "confidence": round(sig.get("confidence", 0.0), 9),
                })

        runner.run(on_tick=capture)

        self.assertEqual(len(direct_states), len(bt_states_collected),
                         "tick count mismatch — drift source of truth is broken")

        # Compare tick-by-tick
        divergence = []
        for i, (d, b) in enumerate(zip(direct_states, bt_states_collected)):
            if d != b:
                divergence.append((i, d, b))
        self.assertEqual(divergence, [], f"drift detected at ticks: {divergence[:5]}")

    def test_candle_stream_parity_multi_seed(self):
        """A lighter drift check across 3 seeds ensures the result is seed-invariant
        with respect to drift (not just a lucky alignment for seed=17)."""
        from dataclasses import replace
        for seed in (1, 7, 123):
            source = SyntheticSource(kind="gbm", n_candles=150, seed=seed)
            candles = list(source.iter_candles("SOL/USDC"))
            direct = self._collect_direct(candles)

            cfg = make_quick_config(name=f"drift_{seed}", n_candles=150, seed=seed)
            cfg = replace(cfg, coordinator_enabled=False)
            runner = BacktestRunner(cfg)
            captured = []
            runner.run(on_tick=lambda st: captured.extend([
                {
                    "regime": ps.get("regime"),
                    "strategy": ps.get("strategy"),
                    "action": ps.get("signal", {}).get("action"),
                    "confidence": round(ps.get("signal", {}).get("confidence", 0.0), 9),
                }
                for _p, ps in st.get("pairs", {}).items()
            ]))
            self.assertEqual(direct, captured, f"drift on seed={seed}")


if __name__ == "__main__":
    unittest.main()
