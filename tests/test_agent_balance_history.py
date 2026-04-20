"""Tests for HydraAgent's in-memory balance history buffer."""
from collections import deque

from hydra_agent import HydraAgent


def test_balance_history_appends_each_tick_and_bounds_at_720():
    a = HydraAgent.__new__(HydraAgent)  # bypass __init__; test the buffer only
    a._balance_history = deque(maxlen=720)
    a._record_balance_sample(ts=100.0, balance=1000.0)
    a._record_balance_sample(ts=160.0, balance=1005.0)
    assert list(a._balance_history) == [(100.0, 1000.0), (160.0, 1005.0)]

    # Fill past cap to verify deque bounds eviction
    for i in range(720):
        a._record_balance_sample(ts=200.0 + i * 60, balance=1100.0 + i)
    assert len(a._balance_history) == 720
    # Original samples (1000.0, 1005.0) evicted
    assert a._balance_history[0][1] != 1000.0
    assert a._balance_history[0][1] != 1005.0
