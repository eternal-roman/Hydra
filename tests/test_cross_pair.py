"""
HYDRA Cross-Pair Coordinator Test Suite
Validates regime correlation detection, override generation, coordinated swap
signals, and edge cases. All tests use deterministic synthetic state dicts.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_engine import CrossPairCoordinator, HydraEngine, Candle


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

PAIRS = ["SOL/USDC", "SOL/XBT", "XBT/USDC"]


def make_state(regime, signal_action="HOLD", confidence=0.5, position_size=0.0, price=100.0):
    """Build a minimal engine state dict for testing."""
    return {
        "regime": regime,
        "signal": {"action": signal_action, "confidence": confidence, "reason": "test"},
        "position": {"size": position_size, "avg_entry": price, "unrealized_pnl": 0.0},
        "price": price,
        "portfolio": {"balance": 100.0, "equity": 100.0, "pnl_pct": 0.0},
    }


# ═══════════════════════════════════════════════════════════════
# 1. INITIALIZATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestInit:
    def test_creates_history_for_all_pairs(self):
        coord = CrossPairCoordinator(PAIRS)
        assert set(coord.regime_history.keys()) == set(PAIRS)
        for history in coord.regime_history.values():
            assert history == []

    def test_stores_pairs(self):
        coord = CrossPairCoordinator(PAIRS)
        assert coord.pairs == PAIRS


# ═══════════════════════════════════════════════════════════════
# 2. REGIME HISTORY TRACKING
# ═══════════════════════════════════════════════════════════════

class TestRegimeHistory:
    def test_update_appends_regime(self):
        coord = CrossPairCoordinator(PAIRS)
        coord.update("SOL/USDC", "TREND_UP")
        coord.update("SOL/USDC", "RANGING")
        assert coord.regime_history["SOL/USDC"] == ["TREND_UP", "RANGING"]

    def test_history_bounded_to_10(self):
        coord = CrossPairCoordinator(PAIRS)
        for i in range(15):
            coord.update("SOL/USDC", f"R{i}")
        assert len(coord.regime_history["SOL/USDC"]) == 10
        assert coord.regime_history["SOL/USDC"][0] == "R5"
        assert coord.regime_history["SOL/USDC"][-1] == "R14"

    def test_update_unknown_pair_creates_entry(self):
        coord = CrossPairCoordinator(PAIRS)
        coord.update("ETH/USDC", "TREND_UP")
        assert coord.regime_history["ETH/USDC"] == ["TREND_UP"]


# ═══════════════════════════════════════════════════════════════
# 3. RULE 1: BTC LEADS SOL DOWN
# ═══════════════════════════════════════════════════════════════

class TestRule1BtcLeadsDown:
    def test_xbt_down_sol_up_triggers_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("TREND_UP"),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert overrides["SOL/USDC"]["signal"] == "SELL"
        assert overrides["SOL/USDC"]["confidence_adj"] == 0.8

    def test_xbt_down_sol_ranging_triggers_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("RANGING"),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["signal"] == "SELL"

    def test_xbt_down_sol_also_down_no_override(self):
        """If SOL is already trending down, no override needed."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("TREND_DOWN"),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        # Rule 1 should NOT fire (SOL already down), Rule 3 won't fire (no position)
        assert "SOL/USDC" not in overrides


# ═══════════════════════════════════════════════════════════════
# 4. RULE 2: BTC RECOVERY BOOST
# ═══════════════════════════════════════════════════════════════

class TestRule2BtcRecovery:
    def test_xbt_up_sol_down_boosts_confidence(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_DOWN", confidence=0.4),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "ADJUST"
        assert overrides["SOL/USDC"]["signal"] == "BUY"
        assert overrides["SOL/USDC"]["confidence_adj"] == 0.55  # 0.4 + 0.15

    def test_confidence_capped_at_095(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_DOWN", confidence=0.9),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert overrides["SOL/USDC"]["confidence_adj"] == 0.95

    def test_xbt_up_sol_up_no_boost(self):
        """No boost needed if SOL is already trending up."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_UP"),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" not in overrides


# ═══════════════════════════════════════════════════════════════
# 5. RULE 3: COORDINATED SWAP
# ═══════════════════════════════════════════════════════════════

class TestRule3CoordinatedSwap:
    def test_sol_down_solxbt_up_with_position_triggers_swap(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("TREND_DOWN", position_size=5.0),
            "SOL/XBT": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert overrides["SOL/USDC"]["signal"] == "SELL"
        assert "swap" in overrides["SOL/USDC"]
        swap = overrides["SOL/USDC"]["swap"]
        assert swap["sell_pair"] == "SOL/USDC"
        assert swap["buy_pair"] == "SOL/XBT"

    def test_no_swap_without_position(self):
        """Don't suggest a swap if we don't hold any SOL via SOL/USDC."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("TREND_DOWN", position_size=0.0),
            "SOL/XBT": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        # Without position, rule 3 shouldn't fire. Rule 1 also doesn't apply
        # (XBT isn't down). So no overrides.
        assert "SOL/USDC" not in overrides

    def test_sol_down_solxbt_also_down_no_swap(self):
        """No swap if SOL/XBT is also trending down."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("TREND_DOWN", position_size=5.0),
            "SOL/XBT": make_state("TREND_DOWN"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" not in overrides or "swap" not in overrides.get("SOL/USDC", {})


# ═══════════════════════════════════════════════════════════════
# 6. NO OVERRIDE BASELINES
# ═══════════════════════════════════════════════════════════════

class TestNoOverride:
    def test_all_ranging_no_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("RANGING"),
            "SOL/USDC": make_state("RANGING"),
            "SOL/XBT": make_state("RANGING"),
        }
        overrides = coord.get_overrides(states)
        assert len(overrides) == 0

    def test_all_trending_up_no_override(self):
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_UP"),
            "SOL/USDC": make_state("TREND_UP"),
            "SOL/XBT": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert len(overrides) == 0

    def test_missing_pair_graceful(self):
        """Should not crash if a pair is missing from states."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "SOL/USDC": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert isinstance(overrides, dict)

    def test_empty_states_no_crash(self):
        coord = CrossPairCoordinator(PAIRS)
        overrides = coord.get_overrides({})
        assert overrides == {}


# ═══════════════════════════════════════════════════════════════
# 7. RULE PRIORITY (Rule 1 vs Rule 3 conflict)
# ═══════════════════════════════════════════════════════════════

class TestRulePriority:
    def test_rule1_fires_when_sol_up_and_xbt_down(self):
        """Rule 1 fires when XBT is down and SOL is still up. Rule 3 cannot fire
        simultaneously because it requires SOL/USDC TREND_DOWN."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("TREND_DOWN"),
            "SOL/USDC": make_state("TREND_UP", position_size=5.0),
            "SOL/XBT": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert overrides["SOL/USDC"]["action"] == "OVERRIDE"
        assert overrides["SOL/USDC"]["signal"] == "SELL"
        # Rule 3 requires SOL/USDC TREND_DOWN, so no swap here
        assert "swap" not in overrides["SOL/USDC"]

    def test_rule3_fires_independently_of_rule1(self):
        """Rule 3 fires when SOL/USDC is TREND_DOWN (Rule 1 doesn't apply)."""
        coord = CrossPairCoordinator(PAIRS)
        states = {
            "XBT/USDC": make_state("RANGING"),  # Not TREND_DOWN, so Rule 1 doesn't fire
            "SOL/USDC": make_state("TREND_DOWN", position_size=5.0),
            "SOL/XBT": make_state("TREND_UP"),
        }
        overrides = coord.get_overrides(states)
        assert "SOL/USDC" in overrides
        assert "swap" in overrides["SOL/USDC"]


# ═══════════════════════════════════════════════════════════════
# 8. SHARPE ANNUALIZATION FIX
# ═══════════════════════════════════════════════════════════════

class TestSharpeAnnualization:
    def test_5min_candle_different_from_1min(self):
        """Sharpe should differ based on candle interval."""
        engine_1m = HydraEngine(initial_balance=10000, asset="BTC/USD", candle_interval=1)
        engine_5m = HydraEngine(initial_balance=10000, asset="BTC/USD", candle_interval=5)

        # Feed identical equity history
        equity = [10000 + i * 10 for i in range(60)]
        engine_1m.equity_history = list(equity)
        engine_5m.equity_history = list(equity)

        sharpe_1m = engine_1m._calc_sharpe()
        sharpe_5m = engine_5m._calc_sharpe()

        # 1-min should have higher annualized Sharpe (more periods per year)
        assert sharpe_1m > sharpe_5m
        assert sharpe_5m > 0

    def test_1min_candle_uses_full_annualization(self):
        """With 1-min candles, periods_per_year = 525600."""
        import math
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD", candle_interval=1)
        engine.equity_history = [10000 + i for i in range(60)]
        sharpe = engine._calc_sharpe()
        assert sharpe > 0


# ═══════════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    test_classes = [
        TestInit,
        TestRegimeHistory,
        TestRule1BtcLeadsDown,
        TestRule2BtcRecovery,
        TestRule3CoordinatedSwap,
        TestNoOverride,
        TestRulePriority,
        TestSharpeAnnualization,
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
