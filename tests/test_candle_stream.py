"""
HYDRA CandleStream Test Suite
Validates WS ohlc message dispatch, latest_candle storage, multi-pair
symbol mapping, and paper-mode behavior.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import CandleStream


PAIRS = ["SOL/USDC", "SOL/XBT", "XBT/USDC"]


def _make_stream(paper=False):
    return CandleStream(pairs=PAIRS, interval=5, paper=paper)


class TestCandleStreamDispatch:

    def test_heartbeat_bumps_timestamp(self):
        cs = _make_stream()
        cs._last_heartbeat = 0.0
        cs._on_message({"channel": "heartbeat"})
        assert cs._last_heartbeat > time.monotonic() - 1.0

    def test_ohlc_snapshot_stores_candle(self):
        cs = _make_stream()
        cs._on_message({
            "channel": "ohlc",
            "type": "snapshot",
            "data": [{
                "symbol": "SOL/USDC",
                "open": 82.0, "high": 83.0, "low": 81.0, "close": 82.5,
                "volume": 100.0, "interval": 5,
                "interval_begin": "2026-04-12T20:00:00.000000000Z",
                "timestamp": "2026-04-12T20:05:00.000000Z",
            }],
        })
        candle = cs.latest_candle("SOL/USDC")
        assert candle is not None
        assert candle["close"] == 82.5
        assert candle["symbol"] == "SOL/USDC"

    def test_ohlc_update_overwrites_candle(self):
        cs = _make_stream()
        # First snapshot
        cs._on_message({
            "channel": "ohlc", "type": "snapshot",
            "data": [{"symbol": "SOL/USDC", "close": 80.0, "open": 80.0,
                       "high": 80.0, "low": 80.0, "volume": 10.0}],
        })
        assert cs.latest_candle("SOL/USDC")["close"] == 80.0
        # Update
        cs._on_message({
            "channel": "ohlc", "type": "update",
            "data": [{"symbol": "SOL/USDC", "close": 85.0, "open": 82.0,
                       "high": 86.0, "low": 81.0, "volume": 50.0}],
        })
        assert cs.latest_candle("SOL/USDC")["close"] == 85.0

    def test_multi_pair_stored_independently(self):
        """WS v2 returns SOL/BTC and BTC/USDC — mapped to our SOL/XBT and XBT/USDC."""
        cs = _make_stream()
        cs._on_message({
            "channel": "ohlc", "type": "snapshot",
            "data": [
                {"symbol": "SOL/USDC", "close": 82.0, "open": 82.0,
                 "high": 82.0, "low": 82.0, "volume": 1.0},
                {"symbol": "SOL/BTC", "close": 0.00117, "open": 0.00117,
                 "high": 0.00117, "low": 0.00117, "volume": 0.5},
            ],
        })
        sol = cs.latest_candle("SOL/USDC")
        xbt = cs.latest_candle("SOL/XBT")  # queried by friendly name
        assert sol is not None and sol["close"] == 82.0
        assert xbt is not None and xbt["close"] == 0.00117

    def test_unknown_symbol_ignored(self):
        cs = _make_stream()
        cs._on_message({
            "channel": "ohlc", "type": "update",
            "data": [{"symbol": "ETH/USDC", "close": 3500.0, "open": 3500.0,
                       "high": 3500.0, "low": 3500.0, "volume": 1.0}],
        })
        assert cs.latest_candle("ETH/USDC") is None

    def test_status_message_ignored(self):
        cs = _make_stream()
        cs._last_heartbeat = 12345.0
        cs._on_message({"channel": "status", "data": [{"system": "online"}]})
        # Should not bump heartbeat
        assert cs._last_heartbeat == 12345.0

    def test_subscribe_success_ignored(self):
        cs = _make_stream()
        cs._on_message({"method": "subscribe", "success": True})
        # No crash, no candle stored

    def test_non_dict_data_entries_skipped(self):
        cs = _make_stream()
        cs._on_message({
            "channel": "ohlc", "type": "update",
            "data": ["not a dict", None, 42],
        })
        assert cs.latest_candle("SOL/USDC") is None

    def test_latest_candle_returns_none_before_any_data(self):
        cs = _make_stream()
        assert cs.latest_candle("SOL/USDC") is None

    def test_ohlc_channel_bumps_heartbeat(self):
        cs = _make_stream()
        cs._last_heartbeat = 0.0
        cs._on_message({
            "channel": "ohlc", "type": "update",
            "data": [{"symbol": "SOL/USDC", "close": 82.0, "open": 82.0,
                       "high": 82.0, "low": 82.0, "volume": 1.0}],
        })
        assert cs._last_heartbeat > time.monotonic() - 1.0


class TestCandleStreamBuildCmd:

    def test_build_cmd_includes_pairs_and_interval(self):
        cs = _make_stream()
        cmd = cs._build_cmd()
        assert "ws ohlc" in cmd
        assert "--interval 5" in cmd
        assert "-o json" in cmd
        assert "--snapshot true" in cmd
        # Should include resolved pair names
        assert "SOL/USDC" in cmd or "SOLUSDC" in cmd

    def test_build_cmd_uses_exec(self):
        cs = _make_stream()
        cmd = cs._build_cmd()
        assert cmd.startswith("exec ")


class TestCandleStreamPaper:

    def test_paper_mode_healthy(self):
        cs = _make_stream(paper=True)
        cs.start()
        assert cs.healthy
        assert cs.latest_candle("SOL/USDC") is None

    def test_paper_mode_start_returns_true(self):
        cs = _make_stream(paper=True)
        assert cs.start() is True


class TestCandleStreamLabel:

    def test_label(self):
        cs = _make_stream()
        assert cs._stream_label() == "CANDLE_WS"


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestCandleStreamDispatch,
        TestCandleStreamBuildCmd,
        TestCandleStreamPaper,
        TestCandleStreamLabel,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name} (error): {e}")

    print(f"\n  {'='*60}")
    print(f"  CandleStream Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
