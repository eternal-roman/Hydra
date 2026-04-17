"""
HYDRA covariance / Rule 4 helper tests.

Validates the pure-Python correlation + confluence_bonus helpers on
CrossPairCoordinator. These are stdlib-only (no numpy) and must be
safe on insufficient data or degenerate series.
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_engine import CrossPairCoordinator


class TestLogReturns:
    def test_empty_returns_empty(self):
        assert CrossPairCoordinator._log_returns([]) == []

    def test_single_price_returns_empty(self):
        assert CrossPairCoordinator._log_returns([100.0]) == []

    def test_two_prices_returns_one_ratio(self):
        r = CrossPairCoordinator._log_returns([100.0, 110.0])
        assert len(r) == 1
        assert abs(r[0] - math.log(1.1)) < 1e-12

    def test_non_positive_price_returns_empty(self):
        # log is undefined at zero — the helper must not raise.
        assert CrossPairCoordinator._log_returns([100.0, 0.0, 120.0]) == []
        assert CrossPairCoordinator._log_returns([100.0, -5.0]) == []


class TestPairCorrelation:
    def test_identical_series_is_one(self):
        # 100 walk-up candles on both sides, identical.
        prices = [100.0 + i * 0.5 for i in range(100)]
        rho = CrossPairCoordinator.pair_correlation(prices, prices, window=60)
        assert abs(rho - 1.0) < 1e-9

    def test_perfectly_anti_correlated_is_minus_one(self):
        # Build two series with VARYING log-returns (non-zero variance) that
        # are exact mirrors: r_b[i] = -r_a[i]. Forces ρ to -1 exactly.
        shocks = [math.sin(i / 3.0) * 0.02 for i in range(100)]
        a = [100.0]
        b = [100.0]
        for s in shocks:
            a.append(a[-1] * math.exp(s))
            b.append(b[-1] * math.exp(-s))
        rho = CrossPairCoordinator.pair_correlation(a, b, window=60)
        assert abs(rho - (-1.0)) < 1e-9, f"expected ρ=-1, got {rho}"

    def test_independent_noise_is_near_zero(self):
        # Two independent log-normal random walks — ρ should be small-ish.
        rng_a = random.Random(42)
        rng_b = random.Random(1337)
        a = [100.0]
        b = [100.0]
        for _ in range(200):
            a.append(a[-1] * math.exp(rng_a.gauss(0, 0.01)))
            b.append(b[-1] * math.exp(rng_b.gauss(0, 0.01)))
        rho = CrossPairCoordinator.pair_correlation(a, b, window=60)
        # Deterministic under fixed seeds; empirical bound.
        assert abs(rho) < 0.35, f"expected |ρ| < 0.35, got {rho}"

    def test_insufficient_data_is_zero(self):
        prices = [100.0 + i for i in range(10)]  # only 9 returns
        rho = CrossPairCoordinator.pair_correlation(prices, prices, window=60)
        assert rho == 0.0

    def test_zero_variance_is_zero(self):
        flat = [100.0] * 100
        varied = [100.0 + i * 0.5 for i in range(100)]
        assert CrossPairCoordinator.pair_correlation(flat, varied, window=60) == 0.0
        assert CrossPairCoordinator.pair_correlation(flat, flat, window=60) == 0.0

    def test_mismatched_lengths_uses_last_window(self):
        # a has 200 points, b has 80 points. Both exceed window=60, so the
        # last 60 returns on each side are compared independently. Identical
        # tails should still produce ρ ≈ 1 even though a is longer.
        tail = [100.0 + i * 0.7 for i in range(80)]
        a = [50.0 + i * 0.2 for i in range(120)] + tail
        b = tail
        rho = CrossPairCoordinator.pair_correlation(a, b, window=60)
        assert abs(rho - 1.0) < 1e-9


class TestConfluenceBonus:
    def test_positive_rho_and_conf_gives_bonus(self):
        bonus = CrossPairCoordinator.confluence_bonus(rho=0.8, other_conf=0.75)
        assert bonus > 0
        assert bonus <= 0.10

    def test_caps_at_max(self):
        # Saturated inputs — ρ=1, conf=1 → raw = 1 * 0.5 * 0.3 = 0.15, capped at 0.10.
        bonus = CrossPairCoordinator.confluence_bonus(rho=1.0, other_conf=1.0)
        assert abs(bonus - 0.10) < 1e-12

    def test_zero_rho_no_bonus(self):
        assert CrossPairCoordinator.confluence_bonus(rho=0.0, other_conf=0.9) == 0.0

    def test_negative_rho_no_bonus(self):
        assert CrossPairCoordinator.confluence_bonus(rho=-0.7, other_conf=0.9) == 0.0

    def test_sub_threshold_conf_no_bonus(self):
        # conf below 0.5 = no Kelly edge → no boost. The sizer already
        # returns 0 in that region, so promoting it would be meaningless.
        assert CrossPairCoordinator.confluence_bonus(rho=0.9, other_conf=0.5) == 0.0
        assert CrossPairCoordinator.confluence_bonus(rho=0.9, other_conf=0.4) == 0.0

    def test_custom_max_bonus(self):
        bonus = CrossPairCoordinator.confluence_bonus(rho=1.0, other_conf=1.0, max_bonus=0.05)
        assert abs(bonus - 0.05) < 1e-12


if __name__ == "__main__":
    # Minimal standalone runner so `python tests/test_covariance.py` works.
    # pytest remains the canonical runner per CLAUDE.md.
    for cls in (TestLogReturns, TestPairCorrelation, TestConfluenceBonus):
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                getattr(inst, name)()
                print(f"  OK  {cls.__name__}.{name}")
    print("\nAll covariance tests passed.")
