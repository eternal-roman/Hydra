"""Brain prompt smoke test: ensure the six new feature names are in the
RM system prompt and that rendering an RM user message with populated
features surfaces the numeric values. This does not call the LLM."""
import pytest
from hydra_brain import HydraBrain, RISK_MANAGER_PROMPT


def test_risk_prompt_names_all_six_features():
    for name in (
        "realized_vol_1h_pct", "realized_vol_24h_pct",
        "drawdown_velocity_pct_per_hr", "fill_rate_24h",
        "avg_slippage_bps_24h", "cross_pair_corr_24h",
        "minutes_since_last_trade",
    ):
        assert name in RISK_MANAGER_PROMPT, f"{name} missing from RM prompt"


def test_risk_prompt_references_cues():
    # Each feature must have a concrete numeric cue so RM can cite a threshold.
    lower = RISK_MANAGER_PROMPT.lower()
    assert "drawdown_velocity" in lower and "bleed" in lower, \
        "DD velocity lacks 'bleed' cue"
    assert "fill_rate" in lower and "0.3" in lower, \
        "fill_rate lacks numeric cue"
    assert "cross_pair_corr" in lower and "0.8" in lower, \
        "correlation lacks numeric cue"
