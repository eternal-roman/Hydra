"""Integration test: quant_indicators contains the six new RM features."""
import os
from collections import deque
from unittest.mock import MagicMock

from hydra_agent import HydraAgent


def _minimal_agent_with_engines():
    a = HydraAgent.__new__(HydraAgent)
    a.engines = {}
    a.derivatives_stream = None
    a._balance_history = deque(maxlen=720)
    a.order_journal = []
    # Add two engines with fake candle buffers
    for pair in ("BTC/USDC", "SOL/USDC"):
        eng = MagicMock()
        eng.cvd_divergence_sigma.return_value = 0.0
        eng.get_candles.return_value = [
            {"ts": 1_700_000_000 + i * 900, "close": 100.0 + i * 0.1}
            for i in range(100)
        ]
        a.engines[pair] = eng
    return a


def test_rm_feature_keys_present_in_quant_indicators():
    a = _minimal_agent_with_engines()
    state = {"signal": {"action": "BUY"}}
    a._build_quant_indicators("BTC/USDC", state)  # populates state["quant_indicators"]
    qi = state.get("quant_indicators", {})
    for key in (
        "realized_vol_1h_pct", "realized_vol_24h_pct",
        "drawdown_velocity_pct_per_hr", "fill_rate_24h",
        "avg_slippage_bps_24h", "cross_pair_corr_24h",
        "minutes_since_last_trade",
    ):
        assert key in qi, f"missing {key} in quant_indicators"


def test_rm_features_disabled_env_flag_skips_computation():
    a = _minimal_agent_with_engines()
    os.environ["HYDRA_RM_FEATURES_DISABLED"] = "1"
    try:
        state = {"signal": {"action": "BUY"}}
        a._build_quant_indicators("BTC/USDC", state)
        qi = state.get("quant_indicators", {})
        # Key should be absent entirely (clean rollback semantics)
        assert "realized_vol_1h_pct" not in qi
        assert "drawdown_velocity_pct_per_hr" not in qi
    finally:
        del os.environ["HYDRA_RM_FEATURES_DISABLED"]
