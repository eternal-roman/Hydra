"""
HYDRA Order Book Analyzer Test Suite
Validates depth parsing, imbalance ratios, confidence modifiers,
wall detection, spread calculation, and edge cases.
All tests use deterministic synthetic depth data.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_engine import OrderBookAnalyzer


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def make_depth(bid_prices, bid_volumes, ask_prices, ask_volumes, nested=False):
    """Build a synthetic Kraken depth dict.

    Args:
        bid_prices: list of bid prices (descending)
        bid_volumes: list of bid volumes
        ask_prices: list of ask prices (ascending)
        ask_volumes: list of ask volumes
        nested: if True, wrap in Kraken's {"PAIR": {...}} format
    """
    bids = [[str(p), str(v), 1000000] for p, v in zip(bid_prices, bid_volumes)]
    asks = [[str(p), str(v), 1000000] for p, v in zip(ask_prices, ask_volumes)]
    data = {"bids": bids, "asks": asks}
    if nested:
        return {"BTCUSDC": data}
    return data


def balanced_depth(price=100.0, volume=10.0, levels=5):
    """Perfectly balanced order book — equal volume on both sides."""
    bid_prices = [price - i * 0.1 for i in range(levels)]
    ask_prices = [price + 0.1 + i * 0.1 for i in range(levels)]
    bid_vols = [volume] * levels
    ask_vols = [volume] * levels
    return make_depth(bid_prices, bid_vols, ask_prices, ask_vols)


def bullish_depth(price=100.0, levels=5):
    """Heavy bid side — 2x more bid volume than ask."""
    bid_prices = [price - i * 0.1 for i in range(levels)]
    ask_prices = [price + 0.1 + i * 0.1 for i in range(levels)]
    bid_vols = [20.0] * levels  # 100 total
    ask_vols = [5.0] * levels   # 25 total → ratio = 4.0
    return make_depth(bid_prices, bid_vols, ask_prices, ask_vols)


def bearish_depth(price=100.0, levels=5):
    """Heavy ask side — 3x more ask volume than bid."""
    bid_prices = [price - i * 0.1 for i in range(levels)]
    ask_prices = [price + 0.1 + i * 0.1 for i in range(levels)]
    bid_vols = [5.0] * levels   # 25 total
    ask_vols = [20.0] * levels  # 100 total → ratio = 0.25
    return make_depth(bid_prices, bid_vols, ask_prices, ask_vols)


# ═══════════════════════════════════════════════════════════════
# 1. BASIC PARSING
# ═══════════════════════════════════════════════════════════════

class TestParsing:
    def test_direct_format(self):
        depth = balanced_depth()
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_volume"] > 0
        assert result["ask_volume"] > 0

    def test_nested_format(self):
        depth = make_depth(
            [100, 99.9], [10, 10],
            [100.1, 100.2], [10, 10],
            nested=True,
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_volume"] == 20.0
        assert result["ask_volume"] == 20.0

    def test_empty_depth(self):
        result = OrderBookAnalyzer.analyze({})
        assert result["bid_volume"] == 0.0
        assert result["ask_volume"] == 0.0
        assert result["confidence_modifier"] == 0.0

    def test_missing_bids(self):
        result = OrderBookAnalyzer.analyze({"asks": [["100", "10", 1000]]})
        assert result["confidence_modifier"] == 0.0

    def test_missing_asks(self):
        result = OrderBookAnalyzer.analyze({"bids": [["100", "10", 1000]]})
        assert result["confidence_modifier"] == 0.0

    def test_top_10_only(self):
        """Should only use top 10 levels even if more are provided."""
        # Top 10 levels carry volume 10.0, levels 11-20 carry volume 999.0.
        # If the analyzer took all 20 levels the sum would be 10090; if it
        # took the top 10 it is exactly 100. An equal-volume test would pass
        # even if the cap were broken, so we make the levels distinguishable.
        bid_prices = [100.0 - i * 0.1 for i in range(20)]
        ask_prices = [100.1 + i * 0.1 for i in range(20)]
        bid_vols = [10.0] * 10 + [999.0] * 10
        ask_vols = [10.0] * 10 + [999.0] * 10
        depth = make_depth(bid_prices, bid_vols, ask_prices, ask_vols)
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_volume"] == 100.0  # 10 * 10, not 10090
        assert result["ask_volume"] == 100.0


# ═══════════════════════════════════════════════════════════════
# 2. IMBALANCE RATIO
# ═══════════════════════════════════════════════════════════════

class TestImbalanceRatio:
    def test_balanced_ratio_is_one(self):
        depth = balanced_depth()
        result = OrderBookAnalyzer.analyze(depth)
        assert result["imbalance_ratio"] == 1.0

    def test_bullish_ratio_above_threshold(self):
        depth = bullish_depth()
        result = OrderBookAnalyzer.analyze(depth)
        assert result["imbalance_ratio"] > 1.5

    def test_bearish_ratio_below_threshold(self):
        depth = bearish_depth()
        result = OrderBookAnalyzer.analyze(depth)
        assert result["imbalance_ratio"] < 0.67

    def test_exact_ratio_calculation(self):
        depth = make_depth(
            [100], [30],
            [100.1], [10],
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["imbalance_ratio"] == 3.0


# ═══════════════════════════════════════════════════════════════
# 3. SPREAD CALCULATION
# ═══════════════════════════════════════════════════════════════

class TestSpread:
    def test_spread_bps(self):
        depth = make_depth(
            [99.95], [10],
            [100.05], [10],
        )
        result = OrderBookAnalyzer.analyze(depth)
        # Spread = 0.10, mid = 100.0, bps = 0.10/100.0 * 10000 = 10.0
        assert result["spread_bps"] == 10.0

    def test_tight_spread(self):
        depth = make_depth(
            [99.999], [10],
            [100.001], [10],
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["spread_bps"] < 1.0

    def test_wide_spread(self):
        depth = make_depth(
            [99.0], [10],
            [101.0], [10],
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["spread_bps"] > 100.0


# ═══════════════════════════════════════════════════════════════
# 4. WALL DETECTION
# ═══════════════════════════════════════════════════════════════

class TestWallDetection:
    def test_no_wall_uniform_volume(self):
        depth = balanced_depth()
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_wall"] is False
        assert result["ask_wall"] is False

    def test_bid_wall_detected(self):
        """A single bid level with >3x the average should trigger wall detection."""
        depth = make_depth(
            [100, 99.9, 99.8, 99.7, 99.6], [100, 5, 5, 5, 5],  # avg=24, 100 > 24*3=72
            [100.1, 100.2, 100.3, 100.4, 100.5], [10, 10, 10, 10, 10],
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_wall"] is True
        assert result["ask_wall"] is False

    def test_ask_wall_detected(self):
        depth = make_depth(
            [100, 99.9, 99.8, 99.7, 99.6], [10, 10, 10, 10, 10],
            [100.1, 100.2, 100.3, 100.4, 100.5], [5, 5, 5, 5, 100],  # avg=24, 100 > 24*3=72
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["ask_wall"] is True

    def test_both_walls(self):
        depth = make_depth(
            [100, 99.9, 99.8], [200, 5, 5],  # avg=70, 200 > 70*3=210? No. Use bigger.
            [100.1, 100.2, 100.3], [5, 5, 200],  # same
        )
        # avg_bid = 210/3 = 70, 200 > 70*3=210? No. Let's use [500, 5, 5]: avg=170, 500>510? No.
        # Need single level > 3x avg. With [1000, 1, 1]: avg=334, 1000>1002? No.
        # The wall IS the average. Use more levels: [100, 1, 1, 1, 1]: avg=20.8, 100>62.4 YES.
        depth = make_depth(
            [100, 99.9, 99.8, 99.7, 99.6], [100, 1, 1, 1, 1],  # avg=20.8, 100 > 62.4
            [100.1, 100.2, 100.3, 100.4, 100.5], [1, 1, 1, 1, 100],  # avg=20.8, 100 > 62.4
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_wall"] is True
        assert result["ask_wall"] is True


# ═══════════════════════════════════════════════════════════════
# 5. CONFIDENCE MODIFIER — BUY SIGNALS
# ═══════════════════════════════════════════════════════════════

class TestModifierBuy:
    def test_bullish_book_boosts_buy(self):
        depth = bullish_depth()
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] > 0
        assert result["confidence_modifier"] <= 0.07

    def test_bearish_book_reduces_buy(self):
        depth = bearish_depth()
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] < 0
        assert result["confidence_modifier"] >= -0.07

    def test_balanced_book_no_modifier_for_buy(self):
        depth = balanced_depth()
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] == 0.0

    def test_modifier_capped_at_007(self):
        """Even with extreme imbalance, modifier should not exceed 0.07."""
        depth = make_depth(
            [100], [1000],
            [100.1], [1],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] <= 0.07

    def test_negative_modifier_capped_at_minus_007(self):
        depth = make_depth(
            [100], [1],
            [100.1], [1000],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] >= -0.07

    def test_modifier_range_max_positive(self):
        """Strongly bullish book + BUY should not exceed +0.07."""
        depth = make_depth(
            [100, 99, 98], [500, 400, 300],
            [101, 102, 103], [50, 40, 30],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] <= 0.07
        assert result["confidence_modifier"] > 0

    def test_modifier_range_max_negative(self):
        """Strongly bearish book + BUY should not go below -0.07."""
        depth = make_depth(
            [100, 99, 98], [50, 40, 30],
            [101, 102, 103], [500, 400, 300],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] >= -0.07
        assert result["confidence_modifier"] < 0


# ═══════════════════════════════════════════════════════════════
# 6. CONFIDENCE MODIFIER — SELL SIGNALS
# ═══════════════════════════════════════════════════════════════

class TestModifierSell:
    def test_bullish_book_reduces_sell(self):
        """Don't sell into strength — strong bids should reduce sell confidence."""
        depth = bullish_depth()
        result = OrderBookAnalyzer.analyze(depth, signal_action="SELL")
        assert result["confidence_modifier"] == -0.035

    def test_bearish_book_confirms_sell(self):
        """Weak bids confirm selling pressure."""
        depth = bearish_depth()
        result = OrderBookAnalyzer.analyze(depth, signal_action="SELL")
        assert result["confidence_modifier"] > 0

    def test_balanced_book_no_modifier_for_sell(self):
        depth = balanced_depth()
        result = OrderBookAnalyzer.analyze(depth, signal_action="SELL")
        assert result["confidence_modifier"] == 0.0


# ═══════════════════════════════════════════════════════════════
# 7. CONFIDENCE MODIFIER — HOLD SIGNALS
# ═══════════════════════════════════════════════════════════════

class TestModifierHold:
    def test_hold_always_zero_modifier(self):
        """HOLD signals should never get a confidence modifier."""
        for depth_fn in [balanced_depth, bullish_depth, bearish_depth]:
            result = OrderBookAnalyzer.analyze(depth_fn(), signal_action="HOLD")
            assert result["confidence_modifier"] == 0.0


# ═══════════════════════════════════════════════════════════════
# 8. EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_single_level_each_side(self):
        depth = make_depth([100], [10], [100.1], [10])
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_volume"] == 10.0
        assert result["ask_volume"] == 10.0
        assert result["imbalance_ratio"] == 1.0

    def test_string_prices_and_volumes(self):
        """Kraken returns strings — verify parsing handles them."""
        depth = {
            "bids": [["99.95", "10.5", "1000000"]],
            "asks": [["100.05", "10.5", "1000000"]],
        }
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_volume"] == 10.5
        assert result["ask_volume"] == 10.5

    def test_very_small_prices(self):
        """SOL/BTC trades at ~0.0012 — verify no division issues."""
        depth = make_depth(
            [0.001200, 0.001199], [100, 100],
            [0.001201, 0.001202], [100, 100],
        )
        result = OrderBookAnalyzer.analyze(depth)
        assert result["spread_bps"] > 0
        assert result["imbalance_ratio"] == 1.0

    def test_zero_ask_volume_no_crash(self):
        """Zero ask volume should not cause division by zero."""
        depth = {
            "bids": [["100", "10", "1000"]],
            "asks": [["100.1", "0", "1000"]],
        }
        result = OrderBookAnalyzer.analyze(depth)
        assert result["ask_volume"] == 0.0
        # imbalance_ratio defaults to 1.0 when ask_volume is 0
        assert result["imbalance_ratio"] == 1.0

    def test_malformed_entries_skipped(self):
        """Entries with too few elements should be skipped gracefully."""
        depth = {
            "bids": [["100"], ["99.9", "10", "1000"]],  # first entry malformed
            "asks": [["100.1", "10", "1000"]],
        }
        result = OrderBookAnalyzer.analyze(depth)
        assert result["bid_volume"] == 10.0  # only second bid parsed


# ═══════════════════════════════════════════════════════════════
# 9. TOTAL MODIFIER CAP — exercises OrderBookAnalyzer.analyze()
#    with extreme order books to verify MAX_BOOK_MODIFIER cap,
#    plus the agent-level clamp formula: max(0.0, min(1.0, conf + mod))
# ═══════════════════════════════════════════════════════════════

class TestTotalModifierCap:
    def test_extreme_bullish_buy_capped_at_max(self):
        """Massively lopsided bid book on BUY must not exceed MAX_BOOK_MODIFIER (+0.07)."""
        depth = make_depth(
            [100, 99, 98, 97, 96], [10000, 10000, 10000, 10000, 10000],
            [101, 102, 103, 104, 105], [1, 1, 1, 1, 1],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] > 0
        assert result["confidence_modifier"] <= OrderBookAnalyzer.MAX_BOOK_MODIFIER

    def test_extreme_bearish_buy_capped_at_neg_max(self):
        """Massively lopsided ask book on BUY must not go below -MAX_BOOK_MODIFIER (-0.07)."""
        depth = make_depth(
            [100, 99, 98, 97, 96], [1, 1, 1, 1, 1],
            [101, 102, 103, 104, 105], [10000, 10000, 10000, 10000, 10000],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        assert result["confidence_modifier"] < 0
        assert result["confidence_modifier"] >= -OrderBookAnalyzer.MAX_BOOK_MODIFIER

    def test_extreme_bearish_sell_capped_at_max(self):
        """Massively lopsided ask book on SELL must not exceed MAX_BOOK_MODIFIER (+0.07)."""
        depth = make_depth(
            [100, 99, 98, 97, 96], [1, 1, 1, 1, 1],
            [101, 102, 103, 104, 105], [10000, 10000, 10000, 10000, 10000],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="SELL")
        assert result["confidence_modifier"] > 0
        assert result["confidence_modifier"] <= OrderBookAnalyzer.MAX_BOOK_MODIFIER

    def test_agent_clamp_floor_at_zero(self):
        """Agent-level clamp: conf + negative modifier must not go below 0.0.

        This tests the formula from hydra_agent.py line ~2007:
            new_conf = max(0.0, min(1.0, old_conf + confidence_modifier))
        We drive the analyzer to produce a real negative modifier and apply
        the agent formula to a low starting confidence.
        """
        depth = make_depth(
            [100, 99, 98, 97, 96], [1, 1, 1, 1, 1],
            [101, 102, 103, 104, 105], [10000, 10000, 10000, 10000, 10000],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        mod = result["confidence_modifier"]
        assert mod < 0  # confirm it's negative
        # Simulate agent-level clamp with a very low starting confidence
        old_conf = 0.02
        new_conf = max(0.0, min(1.0, old_conf + mod))
        assert new_conf == 0.0

    def test_agent_clamp_ceiling_at_one(self):
        """Agent-level clamp: conf + positive modifier must not exceed 1.0.

        Same agent formula as above, applied to a high starting confidence.
        """
        depth = make_depth(
            [100, 99, 98, 97, 96], [10000, 10000, 10000, 10000, 10000],
            [101, 102, 103, 104, 105], [1, 1, 1, 1, 1],
        )
        result = OrderBookAnalyzer.analyze(depth, signal_action="BUY")
        mod = result["confidence_modifier"]
        assert mod > 0  # confirm it's positive
        # Simulate agent-level clamp with a near-ceiling starting confidence
        old_conf = 0.99
        new_conf = max(0.0, min(1.0, old_conf + mod))
        assert new_conf == 1.0


# ═══════════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    test_classes = [
        TestParsing,
        TestImbalanceRatio,
        TestSpread,
        TestWallDetection,
        TestModifierBuy,
        TestModifierSell,
        TestModifierHold,
        TestEdgeCases,
        TestTotalModifierCap,
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
    print(f"  Order Book Tests: {passed}/{total} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  FAILURES:")
        for cls_name, method_name, err in errors:
            print(f"    {cls_name}.{method_name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
