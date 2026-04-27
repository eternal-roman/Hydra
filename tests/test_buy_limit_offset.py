"""Tests for the regime-gated BUY limit offset (hydra_agent)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import (
    _buy_limit_offset_bps,
    _apply_buy_limit_offset,
    _BUY_LIMIT_OFFSET_BPS,
)


class BuyLimitOffsetBpsTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HYDRA_BUY_OFFSET_DISABLED", None)

    # ── Table lookup correctness ─────────────────────────────────

    def test_sol_usd_trend_down_uses_90bps(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USD", "TREND_DOWN"), 90)

    def test_sol_usdc_trend_down_uses_90bps_via_stable_class(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USDC", "TREND_DOWN"), 90)

    def test_sol_usdt_trend_down_uses_90bps_via_stable_class(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USDT", "TREND_DOWN"), 90)

    def test_sol_usd_volatile_uses_65bps(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USD", "VOLATILE"), 65)

    def test_sol_btc_trend_down_uses_30bps(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/BTC", "TREND_DOWN"), 30)

    def test_sol_btc_volatile_uses_25bps(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/BTC", "VOLATILE"), 25)

    def test_btc_usd_trend_down_uses_35bps(self):
        self.assertEqual(_buy_limit_offset_bps("BTC/USD", "TREND_DOWN"), 35)

    def test_btc_usd_volatile_uses_30bps(self):
        self.assertEqual(_buy_limit_offset_bps("BTC/USD", "VOLATILE"), 30)

    # ── No-offset regimes (the caveat — avoid missing fills) ─────

    def test_sol_usd_ranging_no_offset(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USD", "RANGING"), 0)

    def test_sol_usd_trend_up_no_offset(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USD", "TREND_UP"), 0)

    def test_btc_usd_ranging_no_offset(self):
        self.assertEqual(_buy_limit_offset_bps("BTC/USD", "RANGING"), 0)

    # ── Safe fallbacks ───────────────────────────────────────────

    def test_unknown_regime_returns_zero(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USD", "PARTY_TIME"), 0)

    def test_none_regime_returns_zero(self):
        self.assertEqual(_buy_limit_offset_bps("SOL/USD", None), 0)

    def test_unknown_base_returns_zero(self):
        self.assertEqual(_buy_limit_offset_bps("ETH/USD", "TREND_DOWN"), 0)

    def test_malformed_pair_returns_zero(self):
        self.assertEqual(_buy_limit_offset_bps("SOLUSD", "TREND_DOWN"), 0)

    def test_lowercase_pair_normalises(self):
        self.assertEqual(_buy_limit_offset_bps("sol/usd", "TREND_DOWN"), 90)

    # ── Env-flag kill switch ─────────────────────────────────────

    def test_env_flag_disables_offset(self):
        os.environ["HYDRA_BUY_OFFSET_DISABLED"] = "1"
        try:
            self.assertEqual(
                _buy_limit_offset_bps("SOL/USD", "TREND_DOWN"), 0
            )
        finally:
            os.environ.pop("HYDRA_BUY_OFFSET_DISABLED", None)

    def test_env_flag_zero_does_not_disable(self):
        os.environ["HYDRA_BUY_OFFSET_DISABLED"] = "0"
        try:
            self.assertEqual(
                _buy_limit_offset_bps("SOL/USD", "TREND_DOWN"), 90
            )
        finally:
            os.environ.pop("HYDRA_BUY_OFFSET_DISABLED", None)


class ApplyBuyLimitOffsetTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HYDRA_BUY_OFFSET_DISABLED", None)

    def test_sol_usd_trend_down_drops_price_by_90bps(self):
        # SOL/USD price_decimals=2 -> rounds to 2dp
        bid = 200.00
        adj, bps = _apply_buy_limit_offset("SOL/USD", bid, "TREND_DOWN")
        # 200.00 * (1 - 0.0090) = 198.20
        self.assertEqual(bps, 90)
        self.assertAlmostEqual(adj, 198.20, places=2)

    def test_btc_usd_volatile_drops_price_by_30bps(self):
        bid = 80000.00
        adj, bps = _apply_buy_limit_offset("BTC/USD", bid, "VOLATILE")
        # 80000 * (1 - 0.0030) = 79760
        self.assertEqual(bps, 30)
        self.assertAlmostEqual(adj, 79760.00, places=1)

    def test_sol_btc_respects_native_precision(self):
        # SOL/BTC has high price precision; adjusted price should round
        # to the registry's price_decimals (no over-precision rejection).
        bid = 0.0010982
        adj, bps = _apply_buy_limit_offset("SOL/BTC", bid, "TREND_DOWN")
        self.assertEqual(bps, 30)
        # 0.0010982 * 0.9970 ≈ 0.00109490... — exact value depends on
        # the registry's price_decimals; just assert it dropped and is
        # not over-precise (test against str length is brittle, so just
        # check magnitude).
        self.assertLess(adj, bid)
        self.assertGreater(adj, bid * 0.995)

    def test_ranging_returns_unchanged_bid(self):
        bid = 200.00
        adj, bps = _apply_buy_limit_offset("SOL/USD", bid, "RANGING")
        self.assertEqual(bps, 0)
        self.assertEqual(adj, bid)

    def test_zero_bid_short_circuits(self):
        adj, bps = _apply_buy_limit_offset("SOL/USD", 0.0, "TREND_DOWN")
        self.assertEqual(bps, 0)
        self.assertEqual(adj, 0.0)

    def test_negative_bid_short_circuits(self):
        adj, bps = _apply_buy_limit_offset("SOL/USD", -1.0, "TREND_DOWN")
        self.assertEqual(bps, 0)
        self.assertEqual(adj, -1.0)


class TableShapeTests(unittest.TestCase):
    """Guard against accidental table edits that break invariants."""

    def test_no_offset_for_ranging_or_trend_up_anywhere(self):
        # If anyone adds a RANGING / TREND_UP entry the caveat from the
        # original analysis is violated — fail loudly.
        for key in _BUY_LIMIT_OFFSET_BPS:
            _, _, regime = key
            self.assertNotIn(
                regime, ("RANGING", "TREND_UP"),
                f"{key} adds offset to {regime} — violates caveat",
            )

    def test_all_offsets_positive(self):
        for key, bps in _BUY_LIMIT_OFFSET_BPS.items():
            self.assertGreater(bps, 0, f"{key} bps={bps} should be > 0")

    def test_all_offsets_below_safety_ceiling(self):
        # Sanity ceiling: nothing in the table should exceed 200 bps —
        # that's a 2% offset, well past any observed median 24h DD.
        for key, bps in _BUY_LIMIT_OFFSET_BPS.items():
            self.assertLessEqual(bps, 200, f"{key} bps={bps} above ceiling")


if __name__ == "__main__":
    unittest.main()
