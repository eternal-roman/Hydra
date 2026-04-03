"""
HYDRA Parameter Tuner Test Suite
Validates ParameterTracker: defaults, load/save, trade recording,
Bayesian updating, clamping, shift direction, and engine integration.
All tests use deterministic synthetic trade data.
"""

import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_tuner import ParameterTracker, DEFAULT_PARAMS, PARAM_BOUNDS, SHIFT_RATE, MIN_OBSERVATIONS
from hydra_engine import HydraEngine


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def make_tracker(pair="SOL/USDC", tmpdir=None):
    """Create a tracker with a temp save directory."""
    d = tmpdir or tempfile.mkdtemp()
    return ParameterTracker(pair=pair, save_dir=d)


def record_trades(tracker, n_wins=20, n_losses=10, win_params=None, loss_params=None):
    """Record a batch of mock trades with known param distributions."""
    wp = win_params or dict(DEFAULT_PARAMS)
    lp = loss_params or dict(DEFAULT_PARAMS)
    for _ in range(n_wins):
        tracker.record_trade(wp, "SELL", "win", profit=10.0)
    for _ in range(n_losses):
        tracker.record_trade(lp, "SELL", "loss", profit=-5.0)


# ═══════════════════════════════════════════════════════════════
# 1. INITIALIZATION & DEFAULTS
# ═══════════════════════════════════════════════════════════════

class TestInit:
    def test_default_params(self):
        t = make_tracker()
        params = t.get_tunable_params()
        for key in DEFAULT_PARAMS:
            assert key in params
            assert params[key] == DEFAULT_PARAMS[key]

    def test_pair_stored(self):
        t = make_tracker("XBT/USDC")
        assert t.pair == "XBT/USDC"

    def test_empty_observations(self):
        t = make_tracker()
        assert len(t.observations) == 0
        assert t.update_count == 0

    def test_custom_defaults(self):
        custom = {"volatile_atr_pct": 5.0, "volatile_bb_width": 0.10}
        d = tempfile.mkdtemp()
        t = ParameterTracker(pair="SOL/USDC", save_dir=d, defaults={**DEFAULT_PARAMS, **custom})
        assert t.current_params["volatile_atr_pct"] == 5.0
        assert t.current_params["volatile_bb_width"] == 0.10


# ═══════════════════════════════════════════════════════════════
# 2. TRADE RECORDING
# ═══════════════════════════════════════════════════════════════

class TestRecording:
    def test_record_appends(self):
        t = make_tracker()
        t.record_trade(DEFAULT_PARAMS, "SELL", "win", 10.0)
        assert len(t.observations) == 1
        assert t.observations[0]["outcome"] == "win"
        assert t.observations[0]["profit"] == 10.0

    def test_multiple_records(self):
        t = make_tracker()
        for i in range(30):
            t.record_trade(DEFAULT_PARAMS, "SELL", "win" if i % 3 != 0 else "loss", i)
        assert len(t.observations) == 30

    def test_params_snapshot_stored(self):
        t = make_tracker()
        params = {"volatile_atr_pct": 3.5, **{k: v for k, v in DEFAULT_PARAMS.items() if k != "volatile_atr_pct"}}
        t.record_trade(params, "SELL", "win", 5.0)
        assert t.observations[0]["params"]["volatile_atr_pct"] == 3.5


# ═══════════════════════════════════════════════════════════════
# 3. UPDATE — MINIMUM OBSERVATIONS GUARD
# ═══════════════════════════════════════════════════════════════

class TestMinObservations:
    def test_no_update_below_minimum(self):
        t = make_tracker()
        for i in range(MIN_OBSERVATIONS - 1):
            t.record_trade(DEFAULT_PARAMS, "SELL", "win", 10.0)
        result = t.update()
        assert result == DEFAULT_PARAMS
        assert t.update_count == 0

    def test_update_at_minimum(self):
        t = make_tracker()
        record_trades(t, n_wins=15, n_losses=5)
        assert len(t.observations) == 20
        result = t.update()
        assert t.update_count == 1
        # Observations should be cleared after update
        assert len(t.observations) == 0

    def test_no_wins_no_update(self):
        """If all trades are losses, no shift should occur."""
        t = make_tracker()
        for _ in range(25):
            t.record_trade(DEFAULT_PARAMS, "SELL", "loss", -5.0)
        old_params = t.get_tunable_params()
        result = t.update()
        assert result == old_params


# ═══════════════════════════════════════════════════════════════
# 4. BAYESIAN UPDATE — SHIFT DIRECTION
# ═══════════════════════════════════════════════════════════════

class TestShiftDirection:
    def test_shifts_toward_winning_values(self):
        """Wins with high ATR threshold should shift current value upward."""
        t = make_tracker()
        win_params = dict(DEFAULT_PARAMS)
        win_params["volatile_atr_pct"] = 6.0  # Wins happened at higher ATR
        loss_params = dict(DEFAULT_PARAMS)
        loss_params["volatile_atr_pct"] = 2.0  # Losses at lower ATR
        record_trades(t, n_wins=20, n_losses=10, win_params=win_params, loss_params=loss_params)

        old_atr = t.current_params["volatile_atr_pct"]
        t.update()
        new_atr = t.current_params["volatile_atr_pct"]

        # Should shift toward winning mean (6.0), so new > old
        assert new_atr > old_atr

    def test_shifts_toward_lower_winning_values(self):
        """Wins with lower RSI buy threshold should shift current value downward."""
        t = make_tracker()
        win_params = dict(DEFAULT_PARAMS)
        win_params["mean_reversion_rsi_buy"] = 25.0  # Wins at lower threshold
        record_trades(t, n_wins=20, n_losses=10, win_params=win_params)

        old_rsi = t.current_params["mean_reversion_rsi_buy"]
        t.update()
        new_rsi = t.current_params["mean_reversion_rsi_buy"]

        # Should shift toward 25.0, so new < old (35.0)
        assert new_rsi < old_rsi

    def test_shift_rate_is_conservative(self):
        """Shift should be exactly 10% of the distance to winning mean."""
        t = make_tracker()
        win_params = dict(DEFAULT_PARAMS)
        win_params["volatile_atr_pct"] = 6.0
        record_trades(t, n_wins=20, n_losses=0, win_params=win_params)

        old_val = t.current_params["volatile_atr_pct"]  # 4.0
        t.update()
        new_val = t.current_params["volatile_atr_pct"]

        expected = old_val + SHIFT_RATE * (6.0 - old_val)  # 4.0 + 0.1 * 2.0 = 4.2
        assert abs(new_val - expected) < 1e-6

    def test_no_shift_when_wins_match_current(self):
        """If winning trades used the same params as current, no shift."""
        t = make_tracker()
        record_trades(t, n_wins=20, n_losses=10)
        old_params = t.get_tunable_params()
        t.update()
        new_params = t.get_tunable_params()
        for key in DEFAULT_PARAMS:
            assert abs(new_params[key] - old_params[key]) < 1e-8


# ═══════════════════════════════════════════════════════════════
# 5. CLAMPING
# ═══════════════════════════════════════════════════════════════

class TestClamping:
    def test_params_clamped_to_bounds(self):
        """Even with extreme winning values, params stay within bounds."""
        t = make_tracker()
        extreme_params = dict(DEFAULT_PARAMS)
        extreme_params["volatile_atr_pct"] = 100.0  # Way above max bound of 8.0
        extreme_params["momentum_rsi_lower"] = 1.0  # Way below min bound of 10.0
        record_trades(t, n_wins=20, n_losses=0, win_params=extreme_params)
        t.update()

        assert t.current_params["volatile_atr_pct"] <= PARAM_BOUNDS["volatile_atr_pct"][1]
        assert t.current_params["momentum_rsi_lower"] >= PARAM_BOUNDS["momentum_rsi_lower"][0]

    def test_all_params_within_bounds_after_update(self):
        t = make_tracker()
        extreme_params = {}
        for key in DEFAULT_PARAMS:
            # Use extreme values far outside bounds
            extreme_params[key] = PARAM_BOUNDS[key][1] * 10
        record_trades(t, n_wins=20, n_losses=5, win_params=extreme_params)
        t.update()
        for key in DEFAULT_PARAMS:
            lo, hi = PARAM_BOUNDS[key]
            assert lo <= t.current_params[key] <= hi, f"{key}: {t.current_params[key]} not in [{lo}, {hi}]"


# ═══════════════════════════════════════════════════════════════
# 6. PERSISTENCE (SAVE / LOAD)
# ═══════════════════════════════════════════════════════════════

class TestPersistence:
    def test_save_and_load(self):
        d = tempfile.mkdtemp()
        t1 = ParameterTracker(pair="SOL/USDC", save_dir=d)
        win_params = dict(DEFAULT_PARAMS)
        win_params["volatile_atr_pct"] = 6.0
        record_trades(t1, n_wins=20, n_losses=10, win_params=win_params)
        t1.update()

        # Load from same path
        t2 = ParameterTracker(pair="SOL/USDC", save_dir=d)
        assert abs(t2.current_params["volatile_atr_pct"] - t1.current_params["volatile_atr_pct"]) < 1e-8
        assert t2.update_count == 1

    def test_load_clamps_invalid_saved_values(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "hydra_params_SOL_USDC.json")
        with open(path, "w") as f:
            json.dump({"pair": "SOL/USDC", "params": {"volatile_atr_pct": 999.0}}, f)

        t = ParameterTracker(pair="SOL/USDC", save_dir=d)
        assert t.current_params["volatile_atr_pct"] <= PARAM_BOUNDS["volatile_atr_pct"][1]

    def test_load_handles_corrupt_json(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "hydra_params_SOL_USDC.json")
        with open(path, "w") as f:
            f.write("not json")

        t = ParameterTracker(pair="SOL/USDC", save_dir=d)
        assert t.current_params == DEFAULT_PARAMS

    def test_reset_deletes_file(self):
        d = tempfile.mkdtemp()
        t = ParameterTracker(pair="SOL/USDC", save_dir=d)
        win_params = dict(DEFAULT_PARAMS)
        win_params["volatile_atr_pct"] = 6.0
        record_trades(t, n_wins=20, n_losses=10, win_params=win_params)
        t.update()
        assert os.path.exists(t.save_path)

        t.reset()
        assert not os.path.exists(t.save_path)
        assert t.current_params == DEFAULT_PARAMS
        assert t.update_count == 0
        assert len(t.observations) == 0


# ═══════════════════════════════════════════════════════════════
# 7. ENGINE INTEGRATION
# ═══════════════════════════════════════════════════════════════

class TestEngineIntegration:
    def test_snapshot_params_returns_all_tunable(self):
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        params = engine.snapshot_params()
        for key in DEFAULT_PARAMS:
            assert key in params

    def test_apply_tuned_params(self):
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        new_params = {
            "volatile_atr_pct": 5.5,
            "trend_ema_ratio": 1.008,
            "momentum_rsi_lower": 25.0,
            "momentum_rsi_upper": 75.0,
            "mean_reversion_rsi_buy": 30.0,
            "mean_reversion_rsi_sell": 70.0,
            "min_confidence_threshold": 0.50,
        }
        engine.apply_tuned_params(new_params)
        assert engine.volatile_atr_pct == 5.5
        assert engine.trend_ema_ratio == 1.008
        assert engine.momentum_rsi_lower == 25.0
        assert engine.momentum_rsi_upper == 75.0
        assert engine.mean_reversion_rsi_buy == 30.0
        assert engine.mean_reversion_rsi_sell == 70.0
        assert engine.sizer.min_confidence == 0.50

    def test_params_at_entry_stored_on_buy(self):
        """When a BUY creates a new position, params_at_entry should be set."""
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        # Feed enough data to generate signals
        for i in range(60):
            price = 95000 + i * 50
            engine.ingest_candle({
                "open": price - 10, "high": price + 100,
                "low": price - 100, "close": price, "volume": 100,
            })
        # Position should start empty
        assert engine.position.params_at_entry is None

        # Force a buy by manipulating balance and position
        from hydra_engine import Signal, SignalAction, Strategy
        buy_signal = Signal(
            action=SignalAction.BUY, confidence=0.8,
            reason="test", strategy=Strategy.MOMENTUM,
        )
        trade = engine._maybe_execute(buy_signal)
        if trade:
            assert engine.position.params_at_entry is not None
            assert "volatile_atr_pct" in engine.position.params_at_entry

    def test_params_at_entry_cleared_on_full_sell(self):
        """When position is fully closed, params_at_entry should be cleared."""
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        for i in range(60):
            price = 95000 + i * 50
            engine.ingest_candle({
                "open": price - 10, "high": price + 100,
                "low": price - 100, "close": price, "volume": 100,
            })

        from hydra_engine import Signal, SignalAction, Strategy
        buy = Signal(action=SignalAction.BUY, confidence=0.8, reason="test", strategy=Strategy.MOMENTUM)
        engine._maybe_execute(buy)

        if engine.position.size > 0:
            sell = Signal(action=SignalAction.SELL, confidence=0.9, reason="test", strategy=Strategy.MOMENTUM)
            engine._maybe_execute(sell)
            assert engine.position.size == 0.0
            assert engine.position.params_at_entry is None

    def test_tuned_params_affect_regime_detection(self):
        """Changed trend_ema_ratio should produce a different regime than default."""
        from hydra_engine import RegimeDetector, Candle

        # Build a gentle uptrend: enough for EMA20 > EMA50 * 1.005 but not * 1.02
        prices = [100.0 + i * 0.10 for i in range(80)]
        candles = [Candle(open=p - 0.1, high=p + 0.5, low=p - 0.5,
                          close=p, volume=100.0, timestamp=float(i))
                   for i, p in enumerate(prices)]

        # Default ratio (1.005) — gentle trend should register as TREND_UP
        regime_default = RegimeDetector.detect(candles, prices,
                                               volatile_atr_pct=4.0,
                                               volatile_bb_width=0.08,
                                               trend_ema_ratio=1.005)

        # Strict ratio (1.02) — same data should NOT register as TREND_UP
        regime_strict = RegimeDetector.detect(candles, prices,
                                              volatile_atr_pct=4.0,
                                              volatile_bb_width=0.08,
                                              trend_ema_ratio=1.02)

        assert regime_default == "TREND_UP", f"Expected TREND_UP with default ratio, got {regime_default}"
        assert regime_strict != "TREND_UP", f"Expected non-TREND_UP with strict ratio, got {regime_strict}"

    def test_changes_log(self):
        t = make_tracker()
        win_params = dict(DEFAULT_PARAMS)
        win_params["volatile_atr_pct"] = 6.0
        record_trades(t, n_wins=20, n_losses=10, win_params=win_params)
        old_params = t.get_tunable_params()
        t.update()
        changes = t.get_changes_log(old_params)
        assert len(changes) > 0
        assert any("volatile_atr_pct" in c for c in changes)


# ═══════════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    test_classes = [
        TestInit,
        TestRecording,
        TestMinObservations,
        TestShiftDirection,
        TestClamping,
        TestPersistence,
        TestEngineIntegration,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            total += 1
            method = getattr(instance, method_name)
            try:
                method()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  FAIL  {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  ERROR {cls.__name__}.{method_name}: {e}")

    print(f"\n  {'='*60}")
    print(f"  Tuner Tests: {passed}/{total} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  FAILURES:")
        for cls_name, method_name, err in errors:
            print(f"    {cls_name}.{method_name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
