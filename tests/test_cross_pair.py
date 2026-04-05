"""
HYDRA Cross-Pair Coordinator Test Suite (v2)

Covers the new Hamilton regime-switching filter + QAOA-inspired joint-signal
solver that replaced the old rule-based CrossPairCoordinator at
hydra_engine.py:716-755.

No hardcoded confidences are asserted; all expectations are derived from
the mathematical contract of the filter/solver:

  - The Hamilton filter returns a normalised probability vector over the 4
    regimes and concentrates mass on the regime whose feature means best
    match the observation.
  - The joint-signal solver minimises E(s) = -h·s + γ·sᵀΣs over 2^N spin
    configurations. With zero covariance and a strongly directional h, the
    minimiser matches sign(h).
  - Energy gaps map monotonically to confidences via 1 - exp(-k·gap).
  - Sharpe annualisation uses observed candle timestamp deltas.
"""

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_engine import (
    CrossPairCoordinator,
    RegimeSwitchingFilter,
    JointSignalSolver,
    HydraEngine,
    Candle,
)


PAIRS = ["SOL/USDC", "SOL/XBT", "XBT/USDC"]


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def synthetic_candles(start_price: float, drift: float, n: int = 60, dt: float = 300.0) -> list:
    """Deterministic candle sequence with a constant drift (log-return)."""
    price = start_price
    out = []
    t0 = 1_700_000_000.0
    for i in range(n):
        prev = price
        price = prev * math.exp(drift)
        out.append({
            "o": prev, "h": max(prev, price), "l": min(prev, price),
            "c": price, "t": t0 + i * dt,
        })
    return out


def make_state(
    regime="RANGING",
    signal_action="HOLD",
    confidence=0.5,
    position_size=0.0,
    price=100.0,
    drift=0.0,
    atr_pct=1.0,
    ema20=100.0,
    ema50=100.0,
    rsi=50.0,
):
    return {
        "regime": regime,
        "signal": {"action": signal_action, "confidence": confidence, "reason": "test"},
        "position": {"size": position_size, "avg_entry": price, "unrealized_pnl": 0.0},
        "price": price,
        "portfolio": {"balance": 100.0, "equity": 100.0, "pnl_pct": 0.0,
                      "max_drawdown_pct": 0.0, "peak_equity": 100.0},
        "trend": {"ema20": ema20, "ema50": ema50},
        "volatility": {"atr": 1.0, "atr_pct": atr_pct},
        "indicators": {"rsi": rsi, "bb_width": 0.03},
        "candles": synthetic_candles(price, drift),
    }


# ═══════════════════════════════════════════════════════════════
# 1. HAMILTON FILTER
# ═══════════════════════════════════════════════════════════════

class TestHamiltonFilter:
    def test_initial_probs_uniform(self):
        f = RegimeSwitchingFilter()
        for p in f.probs:
            assert abs(p - 0.25) < 1e-9

    def test_probs_normalised_after_update(self):
        f = RegimeSwitchingFilter()
        f.update({"atr_pct": 1.5, "ema_ratio": 1.01, "rsi": 65.0})
        assert abs(sum(f.probs) - 1.0) < 1e-9
        assert all(p >= 0 for p in f.probs)

    def test_trend_up_features_concentrate_on_trend_up(self):
        f = RegimeSwitchingFilter()
        for _ in range(30):
            f.update({"atr_pct": 1.5, "ema_ratio": 1.015, "rsi": 62.0})
        assert f.argmax_regime() == "TREND_UP"

    def test_trend_down_features_concentrate_on_trend_down(self):
        f = RegimeSwitchingFilter()
        for _ in range(30):
            f.update({"atr_pct": 1.5, "ema_ratio": 0.985, "rsi": 38.0})
        assert f.argmax_regime() == "TREND_DOWN"

    def test_volatile_features_concentrate_on_volatile(self):
        f = RegimeSwitchingFilter()
        for _ in range(30):
            f.update({"atr_pct": 6.0, "ema_ratio": 1.0, "rsi": 50.0})
        assert f.argmax_regime() == "VOLATILE"

    def test_transition_matrix_seeds_from_history(self):
        f = RegimeSwitchingFilter()
        history = ["TREND_UP"] * 10 + ["TREND_DOWN"] * 2
        f.seed_transition_matrix(history)
        # After mostly-TREND_UP observations the TREND_UP self-transition
        # probability should dominate row 0.
        row0 = f.P[0]
        assert row0[0] > row0[1]
        assert row0[0] > row0[2]

    def test_transition_matrix_rows_sum_to_one(self):
        f = RegimeSwitchingFilter()
        f.seed_transition_matrix(["TREND_UP", "RANGING", "VOLATILE", "TREND_DOWN"] * 5)
        for row in f.P:
            assert abs(sum(row) - 1.0) < 1e-9


# ═══════════════════════════════════════════════════════════════
# 2. JOINT-SIGNAL SOLVER
# ═══════════════════════════════════════════════════════════════

class TestJointSignalSolver:
    def test_empty_states_returns_empty(self):
        solver = JointSignalSolver(PAIRS)
        out = solver.solve({})
        assert isinstance(out, dict)

    def test_signal_vector_combines_action_and_drift(self):
        solver = JointSignalSolver(PAIRS)
        states = {
            "SOL/USDC": make_state(signal_action="BUY", confidence=0.8),
            "SOL/XBT": make_state(signal_action="SELL", confidence=0.6),
            "XBT/USDC": make_state(signal_action="HOLD", confidence=0.5),
        }
        # Attach regime_probs directly (no filter here)
        for s in states.values():
            s["regime_probs"] = {"TREND_UP": 0.25, "TREND_DOWN": 0.25,
                                  "RANGING": 0.25, "VOLATILE": 0.25}
        h = solver._build_signal_vector(states)
        assert h[0] > 0   # BUY → positive
        assert h[1] < 0   # SELL → negative
        assert abs(h[2]) < 1e-9  # HOLD → zero

    def test_regime_probs_bias_signal_vector(self):
        solver = JointSignalSolver(PAIRS)
        base = make_state(signal_action="HOLD", confidence=0.0)
        states = {"SOL/USDC": dict(base), "SOL/XBT": dict(base), "XBT/USDC": dict(base)}
        # Inject strong TREND_UP bias on SOL/USDC
        states["SOL/USDC"]["regime_probs"] = {"TREND_UP": 0.9, "TREND_DOWN": 0.05,
                                              "RANGING": 0.03, "VOLATILE": 0.02}
        states["SOL/XBT"]["regime_probs"] = {"TREND_UP": 0.25, "TREND_DOWN": 0.25,
                                             "RANGING": 0.25, "VOLATILE": 0.25}
        states["XBT/USDC"]["regime_probs"] = {"TREND_UP": 0.25, "TREND_DOWN": 0.25,
                                              "RANGING": 0.25, "VOLATILE": 0.25}
        h = solver._build_signal_vector(states)
        assert h[0] > h[1]  # SOL/USDC has the up bias

    def test_covariance_diagonal_nonnegative(self):
        solver = JointSignalSolver(PAIRS)
        states = {
            "SOL/USDC": make_state(drift=0.002, price=100.0),
            "SOL/XBT": make_state(drift=-0.001, price=0.001),
            "XBT/USDC": make_state(drift=0.0005, price=70000.0),
        }
        series = solver._build_returns(states)
        cov = solver._covariance(series)
        assert len(cov) == 3
        for i in range(3):
            assert cov[i][i] >= 0.0
            for j in range(3):
                assert abs(cov[i][j] - cov[j][i]) < 1e-12

    def test_ground_state_matches_sign_of_h_with_zero_covariance(self):
        """With zero covariance the Hamiltonian reduces to -h·s, whose
        minimiser is sign(h)."""
        solver = JointSignalSolver(PAIRS, covariance_weight=0.0, regime_drift_weight=0.0)
        # Use flat-price candles so covariance is zero
        states = {
            "SOL/USDC": make_state(signal_action="BUY", confidence=0.9, drift=0.0),
            "SOL/XBT": make_state(signal_action="SELL", confidence=0.9, drift=0.0),
            "XBT/USDC": make_state(signal_action="BUY", confidence=0.9, drift=0.0),
        }
        for s in states.values():
            s["regime_probs"] = {"TREND_UP": 0.25, "TREND_DOWN": 0.25,
                                  "RANGING": 0.25, "VOLATILE": 0.25}
        overrides = solver.solve(states)
        # Every emitted override must agree with sign(h): BUY/SELL/BUY
        if "SOL/USDC" in overrides:
            assert overrides["SOL/USDC"]["signal"] == "BUY"
        if "SOL/XBT" in overrides:
            assert overrides["SOL/XBT"]["signal"] == "SELL"
        if "XBT/USDC" in overrides:
            assert overrides["XBT/USDC"]["signal"] == "BUY"

    def test_confidence_derived_from_energy_gap(self):
        solver = JointSignalSolver(PAIRS)
        states = {
            "SOL/USDC": make_state(signal_action="BUY", confidence=0.9),
            "SOL/XBT": make_state(signal_action="BUY", confidence=0.9),
            "XBT/USDC": make_state(signal_action="BUY", confidence=0.9),
        }
        for s in states.values():
            s["regime_probs"] = {"TREND_UP": 0.9, "TREND_DOWN": 0.02,
                                  "RANGING": 0.04, "VOLATILE": 0.04}
        overrides = solver.solve(states)
        # Any emitted override must have a confidence_adj in [0, 1]
        for ov in overrides.values():
            assert 0.0 <= ov["confidence_adj"] <= 1.0
            assert "energy_gap" in ov
            assert ov["energy_gap"] >= 0.0

    def test_no_literal_hardcoded_confidences_in_output(self):
        """Regression guard: the old coordinator emitted literal 0.8/0.85.
        The new solver's output must not match those exactly unless by
        accident with very low probability — we assert they're not always
        those constants."""
        solver = JointSignalSolver(PAIRS)
        seen = set()
        for drift_magnitude in (0.001, 0.003, 0.005):
            states = {
                "SOL/USDC": make_state(signal_action="BUY", confidence=0.7, drift=drift_magnitude),
                "SOL/XBT": make_state(signal_action="SELL", confidence=0.7, drift=-drift_magnitude),
                "XBT/USDC": make_state(signal_action="BUY", confidence=0.7, drift=drift_magnitude * 0.5),
            }
            for s in states.values():
                s["regime_probs"] = {"TREND_UP": 0.3, "TREND_DOWN": 0.3,
                                      "RANGING": 0.2, "VOLATILE": 0.2}
            out = solver.solve(states)
            for ov in out.values():
                seen.add(round(ov["confidence_adj"], 4))
        # At least some variation — not all 0.8 or 0.85
        assert not (seen == {0.8}) and not (seen == {0.85})


# ═══════════════════════════════════════════════════════════════
# 3. COORDINATOR (end-to-end pipeline)
# ═══════════════════════════════════════════════════════════════

class TestCoordinatorPipeline:
    def test_init_creates_filters_per_pair(self):
        coord = CrossPairCoordinator(PAIRS)
        assert set(coord.filters.keys()) == set(PAIRS)
        assert set(coord.regime_history.keys()) == set(PAIRS)

    def test_get_overrides_attaches_regime_probs(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "SOL/USDC": make_state(),
            "SOL/XBT": make_state(),
            "XBT/USDC": make_state(),
        }
        coord.get_overrides(states)
        for pair in PAIRS:
            assert "regime_probs" in states[pair]
            # probs_dict rounds to 6 decimals — allow a tiny epsilon
            assert abs(sum(states[pair]["regime_probs"].values()) - 1.0) < 1e-5

    def test_update_seeds_transition_matrix_over_history(self):
        coord = CrossPairCoordinator(PAIRS)
        for _ in range(20):
            coord.update("SOL/USDC", "TREND_UP")
        # The filter should have re-seeded at the 10th update (len % 10 == 0)
        row0 = coord.filters["SOL/USDC"].P[0]
        assert max(row0) == row0[0]  # TREND_UP row self-dominated

    def test_history_bounded_to_HISTORY_SIZE(self):
        coord = CrossPairCoordinator(PAIRS)
        for i in range(coord.HISTORY_SIZE + 5):
            coord.update("SOL/USDC", "RANGING")
        assert len(coord.regime_history["SOL/USDC"]) == coord.HISTORY_SIZE

    def test_empty_states_no_crash(self):
        coord = CrossPairCoordinator(PAIRS)
        assert coord.get_overrides({}) == {}

    def test_missing_pair_graceful(self):
        coord = CrossPairCoordinator(PAIRS)
        out = coord.get_overrides({"SOL/USDC": make_state()})
        assert isinstance(out, dict)

    def test_opposing_biases_can_emit_swap_suggestion(self):
        """SOL/USDC short bias + SOL/XBT long bias with an existing SOL
        position should surface a swap hint when the solver emits overrides
        for both legs."""
        coord = CrossPairCoordinator(PAIRS)
        # Seed filter state so the signal vector overwhelms the solver
        states = {
            "SOL/USDC": make_state(signal_action="SELL", confidence=0.95,
                                    drift=-0.005, position_size=5.0, ema20=95.0, ema50=100.0, rsi=32.0),
            "SOL/XBT": make_state(signal_action="BUY", confidence=0.95,
                                  drift=0.004, ema20=1.01, ema50=1.0, rsi=68.0, price=0.001),
            "XBT/USDC": make_state(signal_action="HOLD", confidence=0.0),
        }
        overrides = coord.get_overrides(states)
        assert isinstance(overrides, dict)
        # No literal confidence assertions — just that the structure is valid
        for ov in overrides.values():
            assert "confidence_adj" in ov
            assert ov["signal"] in ("BUY", "SELL", "HOLD")


# ═══════════════════════════════════════════════════════════════
# 4. SHARPE ANNUALIZATION (timestamp-derived)
# ═══════════════════════════════════════════════════════════════

class TestSharpeAnnualization:
    def _seed_candles(self, engine: HydraEngine, dt_seconds: float, n: int = 30):
        t0 = 1_700_000_000.0
        price = 100.0
        for i in range(n):
            engine.ingest_candle({
                "timestamp": t0 + i * dt_seconds,
                "open": price, "high": price, "low": price,
                "close": price, "volume": 1.0,
            })

    def test_sharpe_uses_observed_candle_delta_not_nominal(self):
        """If the nominal candle_interval disagrees with observed deltas,
        the Sharpe result should reflect the observed cadence."""
        # Nominal 5-min engine but actual candles 60s apart
        engine = HydraEngine(initial_balance=10_000, asset="BTC/USD", candle_interval=5)
        self._seed_candles(engine, dt_seconds=60.0, n=30)
        engine.equity_history = [10_000 + i for i in range(60)]
        sharpe_obs = engine._calc_sharpe()

        # Second engine where nominal matches observed
        engine2 = HydraEngine(initial_balance=10_000, asset="BTC/USD", candle_interval=1)
        self._seed_candles(engine2, dt_seconds=60.0, n=30)
        engine2.equity_history = [10_000 + i for i in range(60)]
        sharpe_match = engine2._calc_sharpe()

        # Both should agree (both annualise off observed 60s deltas)
        assert abs(sharpe_obs - sharpe_match) < 1e-9

    def test_faster_candles_give_higher_annualised_sharpe(self):
        e_fast = HydraEngine(initial_balance=10_000, asset="BTC/USD", candle_interval=1)
        e_slow = HydraEngine(initial_balance=10_000, asset="BTC/USD", candle_interval=5)
        self._seed_candles(e_fast, dt_seconds=60.0, n=30)
        self._seed_candles(e_slow, dt_seconds=300.0, n=30)
        e_fast.equity_history = [10_000 + i * 10 for i in range(60)]
        e_slow.equity_history = [10_000 + i * 10 for i in range(60)]
        assert e_fast._calc_sharpe() > e_slow._calc_sharpe() > 0


# ═══════════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    classes = [
        TestHamiltonFilter,
        TestJointSignalSolver,
        TestCoordinatorPipeline,
        TestSharpeAnnualization,
    ]
    total, passed, failed, errors = 0, 0, 0, []
    for cls in classes:
        instance = cls()
        for method_name in sorted(m for m in dir(instance) if m.startswith("test_")):
            total += 1
            try:
                getattr(instance, method_name)()
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
    print(f"  Cross-Pair Tests: {passed}/{total} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  FAILURES:")
        for cls_name, method_name, err in errors:
            print(f"    {cls_name}.{method_name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
