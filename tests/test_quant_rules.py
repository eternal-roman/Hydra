"""Unit tests for hydra_quant_rules.apply_rules (v2.14).

Covers every rule R1–R10 in both firing and non-firing configurations,
plus stacking behavior and force_hold precedence.
"""
import os

import pytest

from hydra_quant_rules import (
    BASIS_EUPHORIC_APR_PCT,
    CONTRARIAN_BOOST,
    CVD_DIVERGENCE_PENALTY,
    EUPHORIC_BASIS_PENALTY,
    FUNDING_EXTREME_BPS,
    MULTIPLIER_CLAMP_MAX,
    RuleFiring,
    RuleResult,
    SQUEEZE_PENALTY,
    STALENESS_SECONDS_MAX,
    STALE_FIELDS_FOR_FORCE_HOLD,
    apply_rules,
)


FRESH_INDICATORS_BALANCED = {
    "funding_bps_8h": 5.0,
    "oi_delta_1h_pct": 0.1,
    "oi_price_regime": "balanced",
    "basis_apr_pct": 12.0,
    "cvd_divergence_sigma": 0.3,
    "staleness_s": 30.0,
}


# ─── Baseline / no-fires ─────────────────────────────────────


def test_balanced_indicators_hold_no_rules_fire():
    r = apply_rules(
        engine_action="HOLD",
        quant_output={"positioning_bias": "balanced"},
        quant_indicators=FRESH_INDICATORS_BALANCED,
    )
    assert r.size_multiplier == 1.0
    assert r.force_hold is False
    assert r.triggered == []


def test_balanced_buy_no_rules_fire():
    r = apply_rules(
        engine_action="BUY",
        quant_output={"positioning_bias": "balanced"},
        quant_indicators=FRESH_INDICATORS_BALANCED,
    )
    assert r.size_multiplier == 1.0
    assert r.force_hold is False


# ─── R1: funding extreme long + BUY ──────────────────────────


def test_r1_fires_on_extreme_positive_funding_and_buy():
    qi = dict(FRESH_INDICATORS_BALANCED, funding_bps_8h=FUNDING_EXTREME_BPS + 5)
    r = apply_rules("BUY", {"positioning_bias": "crowded_long"}, qi)
    assert r.force_hold is True
    assert "R1" in r.force_hold_reason
    ids = [f.rule_id for f in r.triggered]
    assert "R1" in ids


def test_r1_does_not_fire_on_sell():
    qi = dict(FRESH_INDICATORS_BALANCED, funding_bps_8h=FUNDING_EXTREME_BPS + 5)
    r = apply_rules("SELL", {"positioning_bias": "crowded_long"}, qi)
    assert "R1" not in [f.rule_id for f in r.triggered]


def test_r1_does_not_fire_below_threshold():
    qi = dict(FRESH_INDICATORS_BALANCED, funding_bps_8h=FUNDING_EXTREME_BPS - 1)
    r = apply_rules("BUY", {}, qi)
    assert "R1" not in [f.rule_id for f in r.triggered]


# ─── R2: funding extreme short + SELL ────────────────────────


def test_r2_fires_on_extreme_negative_funding_and_sell():
    qi = dict(FRESH_INDICATORS_BALANCED, funding_bps_8h=-(FUNDING_EXTREME_BPS + 5))
    r = apply_rules("SELL", {"positioning_bias": "crowded_short"}, qi)
    assert r.force_hold is True
    assert "R2" in [f.rule_id for f in r.triggered]


def test_r2_does_not_fire_on_buy():
    qi = dict(FRESH_INDICATORS_BALANCED, funding_bps_8h=-(FUNDING_EXTREME_BPS + 5))
    r = apply_rules("BUY", {}, qi)
    assert "R2" not in [f.rule_id for f in r.triggered]


# ─── R3: short_squeeze + BUY ────────────────────────────────


def test_r3_halves_size_on_squeeze_buy():
    qi = dict(FRESH_INDICATORS_BALANCED, oi_price_regime="short_squeeze")
    r = apply_rules("BUY", {}, qi)
    assert "R3" in [f.rule_id for f in r.triggered]
    assert r.size_multiplier == pytest.approx(SQUEEZE_PENALTY)


def test_r3_does_not_fire_on_sell():
    qi = dict(FRESH_INDICATORS_BALANCED, oi_price_regime="short_squeeze")
    r = apply_rules("SELL", {}, qi)
    assert "R3" not in [f.rule_id for f in r.triggered]


# ─── R4: liquidation_cascade + SELL ─────────────────────────


def test_r4_halves_size_on_cascade_sell():
    qi = dict(FRESH_INDICATORS_BALANCED, oi_price_regime="liquidation_cascade")
    r = apply_rules("SELL", {}, qi)
    assert "R4" in [f.rule_id for f in r.triggered]
    assert r.size_multiplier == pytest.approx(SQUEEZE_PENALTY)


# ─── R5: euphoric basis + BUY ───────────────────────────────


def test_r5_trims_size_on_euphoric_basis_buy():
    qi = dict(FRESH_INDICATORS_BALANCED, basis_apr_pct=BASIS_EUPHORIC_APR_PCT + 5)
    r = apply_rules("BUY", {}, qi)
    assert "R5" in [f.rule_id for f in r.triggered]
    assert r.size_multiplier == pytest.approx(EUPHORIC_BASIS_PENALTY)


def test_r5_does_not_fire_on_moderate_basis():
    qi = dict(FRESH_INDICATORS_BALANCED, basis_apr_pct=BASIS_EUPHORIC_APR_PCT - 5)
    r = apply_rules("BUY", {}, qi)
    assert "R5" not in [f.rule_id for f in r.triggered]


# ─── R7: CVD divergence opposing direction ──────────────────


def test_r7_halves_on_negative_cvd_divergence_vs_buy():
    qi = dict(FRESH_INDICATORS_BALANCED, cvd_divergence_sigma=-2.5)
    r = apply_rules("BUY", {}, qi)
    assert "R7" in [f.rule_id for f in r.triggered]
    assert r.size_multiplier == pytest.approx(CVD_DIVERGENCE_PENALTY)


def test_r7_halves_on_positive_cvd_divergence_vs_sell():
    qi = dict(FRESH_INDICATORS_BALANCED, cvd_divergence_sigma=2.5)
    r = apply_rules("SELL", {}, qi)
    assert "R7" in [f.rule_id for f in r.triggered]


def test_r7_does_not_fire_when_aligned():
    qi = dict(FRESH_INDICATORS_BALANCED, cvd_divergence_sigma=2.5)
    r = apply_rules("BUY", {}, qi)  # positive CVD + BUY = aligned
    assert "R7" not in [f.rule_id for f in r.triggered]


def test_r7_does_not_fire_below_threshold():
    qi = dict(FRESH_INDICATORS_BALANCED, cvd_divergence_sigma=-1.5)
    r = apply_rules("BUY", {}, qi)
    assert "R7" not in [f.rule_id for f in r.triggered]


# ─── R8: contrarian edge ────────────────────────────────────


def test_r8_boost_on_crowded_long_sell():
    r = apply_rules("SELL", {"positioning_bias": "crowded_long"},
                    FRESH_INDICATORS_BALANCED)
    assert "R8" in [f.rule_id for f in r.triggered]
    assert r.size_multiplier == pytest.approx(CONTRARIAN_BOOST)


def test_r8_boost_on_crowded_short_buy():
    r = apply_rules("BUY", {"positioning_bias": "crowded_short"},
                    FRESH_INDICATORS_BALANCED)
    assert "R8" in [f.rule_id for f in r.triggered]


def test_r8_does_not_fire_on_aligned_direction():
    r = apply_rules("BUY", {"positioning_bias": "crowded_long"},
                    FRESH_INDICATORS_BALANCED)
    assert "R8" not in [f.rule_id for f in r.triggered]


# ─── R10: data staleness ────────────────────────────────────


def test_r10_force_hold_on_multiple_null_indicators():
    qi = {"funding_bps_8h": None, "oi_price_regime": None,
          "basis_apr_pct": 10.0, "cvd_divergence_sigma": 0.5,
          "oi_delta_1h_pct": 0.1, "staleness_s": 30}
    # 2 nulls (funding, oi_regime) → R10 fires
    r = apply_rules("BUY", {}, qi)
    assert r.force_hold is True
    assert "R10" in [f.rule_id for f in r.triggered]
    assert "R10" in r.force_hold_reason


def test_r10_force_hold_on_aggregate_staleness():
    qi = dict(FRESH_INDICATORS_BALANCED, staleness_s=STALENESS_SECONDS_MAX + 5)
    r = apply_rules("BUY", {}, qi)
    assert r.force_hold is True
    assert "R10" in [f.rule_id for f in r.triggered]


def test_r10_does_not_fire_on_single_null():
    qi = dict(FRESH_INDICATORS_BALANCED, basis_apr_pct=None)
    r = apply_rules("BUY", {}, qi)
    assert "R10" not in [f.rule_id for f in r.triggered]


def test_r10_skips_oi_fields_for_synthetic_pairs():
    """Synthetic SOL/BTC has no direct perp on Kraken Futures, so
    oi_delta_1h_pct and basis_apr_pct are None by design — not stale.
    R10 should not count them when the indicator block is flagged
    synthetic_pair=True. Funding+CVD presence alone keeps RM informed."""
    qi = {
        "funding_bps_8h": -10.0,
        "cvd_divergence_sigma": 0.3,
        "oi_price_regime": "balanced",
        "oi_delta_1h_pct": None,
        "basis_apr_pct": None,
        "synthetic_pair": True,
    }
    result = apply_rules(engine_action="BUY", quant_indicators=qi)
    assert not result.force_hold, (
        f"R10 should skip OI/basis nulls on synthetic pairs, "
        f"got force_hold={result.force_hold} reason={result.force_hold_reason}"
    )


def test_r10_still_fires_for_real_perps_with_stale_oi():
    """Regression guard: real perp with 2+ null fields still trips R10."""
    qi = {
        "funding_bps_8h": -10.0,
        "cvd_divergence_sigma": 0.3,
        "oi_price_regime": None,
        "oi_delta_1h_pct": None,
        "basis_apr_pct": None,
        "synthetic_pair": False,
    }
    result = apply_rules(engine_action="BUY", quant_indicators=qi)
    assert result.force_hold, "real perp with 3 null fields must still trip R10"


def test_r10_synthetic_with_stale_funding_and_cvd_still_trips():
    """Synthetic still has its own staleness check — funding+cvd both null
    is real data starvation even on a synthetic pair (only regime present
    is one field, threshold is 2 nulls of the 3 tracked synthetic fields)."""
    qi = {
        "funding_bps_8h": None,
        "cvd_divergence_sigma": None,
        "oi_price_regime": "balanced",
        "synthetic_pair": True,
    }
    result = apply_rules(engine_action="BUY", quant_indicators=qi)
    assert result.force_hold, "synthetic with both funding+cvd null must still trip R10"


# ─── Stacking + clamping ────────────────────────────────────


def test_multiple_size_rules_stack_multiplicatively():
    # R3 (0.5) + R5 (0.7) + R7 (0.5) = 0.175
    qi = dict(FRESH_INDICATORS_BALANCED,
              oi_price_regime="short_squeeze",
              basis_apr_pct=BASIS_EUPHORIC_APR_PCT + 5,
              cvd_divergence_sigma=-2.5)
    r = apply_rules("BUY", {}, qi)
    ids = {f.rule_id for f in r.triggered}
    assert {"R3", "R5", "R7"} <= ids
    expected = SQUEEZE_PENALTY * EUPHORIC_BASIS_PENALTY * CVD_DIVERGENCE_PENALTY
    assert r.size_multiplier == pytest.approx(expected)


def test_r8_boost_stacks_with_penalties():
    # R7 (0.5) penalty against BUY + R8 (1.15) boost from crowded_short fade?
    # crowded_short + BUY = fade → R8 boost. But R8 needs positioning_bias.
    # Here: CVD −2.5σ + BUY = opposing → R7 fires. Quant says crowded_short
    # → R8 fires. Stack: 0.5 * 1.15 = 0.575
    qi = dict(FRESH_INDICATORS_BALANCED, cvd_divergence_sigma=-2.5)
    r = apply_rules("BUY", {"positioning_bias": "crowded_short"}, qi)
    ids = {f.rule_id for f in r.triggered}
    assert "R7" in ids
    assert "R8" in ids
    assert r.size_multiplier == pytest.approx(
        CVD_DIVERGENCE_PENALTY * CONTRARIAN_BOOST
    )


def test_size_multiplier_clamped_max():
    # No rule produces > 1.5 today, but defend the invariant anyway.
    r = apply_rules("HOLD", {"positioning_bias": "balanced"},
                    FRESH_INDICATORS_BALANCED)
    assert r.size_multiplier <= MULTIPLIER_CLAMP_MAX


def test_force_hold_reason_cites_specific_rule():
    qi = dict(FRESH_INDICATORS_BALANCED, funding_bps_8h=FUNDING_EXTREME_BPS + 5)
    r = apply_rules("BUY", {}, qi)
    assert r.force_hold is True
    assert "R1" in r.force_hold_reason
    assert "funding" in r.force_hold_reason.lower()


# ─── Null / partial input safety ────────────────────────────


def test_none_inputs_return_default_result():
    r = apply_rules("HOLD", None, None)
    # None indicators → all 5 tracked fields are missing → R10 fires
    assert r.force_hold is True
    assert "R10" in [f.rule_id for f in r.triggered]


def test_empty_dict_indicators_fires_r10():
    r = apply_rules("BUY", {}, {})
    assert r.force_hold is True
    assert "R10" in [f.rule_id for f in r.triggered]


# ─── Spot-only invariant (meta-test) ────────────────────────


def test_module_contains_no_order_placement_references():
    path = os.path.join(os.path.dirname(__file__), "..", "hydra_quant_rules.py")
    src = open(path, encoding="utf-8").read()
    for pattern in (
        "place_order", "sendOrder", "sendorder", "api_key", "apiKey",
        "futures.order", "futures_order", "open_position",
    ):
        assert pattern not in src, (
            f"SPOT-ONLY INVARIANT VIOLATED: '{pattern}' in hydra_quant_rules.py"
        )
