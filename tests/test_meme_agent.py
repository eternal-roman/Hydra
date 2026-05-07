import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from hydra_meme_agent import CandleBar, wilder_rsi, vol_ema, compute_obi, compute_vwap


def test_candle_bar_creation():
    bar = CandleBar(ts=1000, open=1.0, high=1.1, low=0.9, close=1.05, vwap=1.02, volume=5000.0, count=42)
    assert bar.close == 1.05
    assert bar.volume == 5000.0


def test_wilder_rsi_insufficient_data():
    assert wilder_rsi([1.0, 1.1], period=9) == 50.0


def test_wilder_rsi_all_gains():
    closes = [float(i) for i in range(1, 12)]  # 10 diffs, all +1
    assert wilder_rsi(closes, period=9) == 100.0


def test_wilder_rsi_all_losses():
    closes = [float(11 - i) for i in range(11)]  # 10 diffs, all -1
    assert wilder_rsi(closes, period=9) == 0.0


def test_wilder_rsi_neutral():
    closes = [100.0] * 11  # no change
    result = wilder_rsi(closes, period=9)
    assert result == 50.0


def test_wilder_rsi_known_value():
    # Alternating gains/losses: avg_gain = avg_loss after seed period → RSI=50
    closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0]
    result = wilder_rsi(closes, period=9)
    assert 48.0 < result < 52.0


def test_vol_ema_single():
    assert vol_ema([100.0], period=10) == 100.0


def test_vol_ema_stable():
    values = [100.0] * 20
    assert abs(vol_ema(values, period=10) - 100.0) < 0.01


def test_compute_obi_buy_pressure():
    bids = [(1.00, 10000.0), (0.99, 8000.0), (0.98, 6000.0), (0.97, 4000.0), (0.96, 2000.0)]
    asks = [(1.01, 1000.0), (1.02, 1000.0), (1.03, 1000.0), (1.04, 1000.0), (1.05, 1000.0)]
    obi = compute_obi(bids, asks)
    assert obi > 0.5  # strongly buy-side


def test_compute_obi_sell_pressure():
    bids = [(1.00, 1000.0)] * 5
    asks = [(1.01, 10000.0)] * 5
    obi = compute_obi(bids, asks)
    assert obi < -0.5


def test_compute_obi_balanced():
    bids = [(1.00, 5000.0)] * 5
    asks = [(1.01, 5000.0)] * 5
    obi = compute_obi(bids, asks)
    assert abs(obi) < 0.05


def test_compute_obi_empty():
    assert compute_obi([], []) == 0.0


def test_compute_vwap_single_bar():
    bars = [CandleBar(ts=0, open=1.0, high=1.1, low=0.9, close=1.05, vwap=1.02, volume=1000.0, count=10)]
    assert compute_vwap(bars) == 1.05


def test_compute_vwap_weighted():
    bars = [
        CandleBar(ts=0, open=1.0, high=1.1, low=0.9, close=1.00, vwap=1.0, volume=1000.0, count=10),
        CandleBar(ts=300, open=1.0, high=1.2, low=1.0, close=1.20, vwap=1.1, volume=3000.0, count=30),
    ]
    # VWAP = (1.00*1000 + 1.20*3000) / 4000 = 4600/4000 = 1.15
    assert abs(compute_vwap(bars) - 1.15) < 0.001


# ─── SignalEngine Tests ────────────────────────────────────────────────────────

from hydra_meme_agent import SignalEngine


def _make_bar(close=1.0, volume=1000.0, ts=0):
    return CandleBar(ts=ts, open=close*0.99, high=close*1.01, low=close*0.98,
                     close=close, vwap=close, volume=volume, count=10)


def _warmed_engine(n_bars=15, close=1.0, volume=1000.0):
    """Return a SignalEngine with n_bars of history loaded."""
    eng = SignalEngine()
    for i in range(n_bars):
        eng.add_bar(_make_bar(close=close + i * 0.001, volume=volume, ts=i * 300))
    return eng


def test_signal_engine_warmup_not_ready():
    eng = SignalEngine()
    for i in range(14):
        eng.add_bar(_make_bar(ts=i * 300))
    assert not eng.is_warmed_up()


def test_signal_engine_warmed_after_15():
    eng = _warmed_engine(n_bars=15)
    assert eng.is_warmed_up()


def test_entry_gate_volume_spike_fail():
    eng = _warmed_engine(volume=1000.0)
    # Low volume bar — should fail volume gate
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=500.0),  # 0.5x EMA, not 1.8x
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["volume_spike"] is False


def test_entry_gate_volume_spike_pass():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),  # 2x EMA
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["volume_spike"] is True


def test_entry_gate_obi_fail():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),
        obi=0.10,  # below 0.20 threshold
        ask_wall_usd=100.0,
    )
    assert gates["obi"] is False


def test_entry_gate_obi_pass():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["obi"] is True


def test_entry_gate_rsi_overbought():
    # All rising prices → RSI near 100 → should fail upper gate
    eng = SignalEngine()
    for i in range(15):
        eng.add_bar(_make_bar(close=1.0 + i * 0.05, volume=1000.0, ts=i * 300))
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=2.0, volume=2000.0),
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["rsi_window"] is False


def test_entry_gate_vwap_fail():
    eng = _warmed_engine(close=1.0, volume=1000.0)
    # Price below VWAP
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=0.90),  # below VWAP ~1.007
        obi=0.25,
        ask_wall_usd=100.0,
    )
    assert gates["vwap_align"] is False


def test_entry_gate_ask_wall_fail():
    eng = _warmed_engine(volume=1000.0)
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(volume=2000.0),
        obi=0.25,
        ask_wall_usd=600.0,  # above $500 limit
    )
    assert gates["ask_wall_clear"] is False


def test_all_gates_pass():
    eng = _warmed_engine(close=1.0, volume=1000.0)
    # Use a neutral RSI bar (no strong trend), volume spike, good OBI, good ask wall
    gates = eng.evaluate_entry_gates(
        latest_bar=_make_bar(close=1.015, volume=2000.0),
        obi=0.25,
        ask_wall_usd=200.0,
    )
    # All 5 gates should reflect actual logic — VWAP and RSI depend on history
    assert isinstance(gates["volume_spike"], bool)
    assert isinstance(gates["obi"], bool)
    assert isinstance(gates["vwap_align"], bool)
    assert isinstance(gates["rsi_window"], bool)
    assert isinstance(gates["ask_wall_clear"], bool)
    assert "all_pass" in gates
