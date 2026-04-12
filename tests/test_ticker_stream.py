"""
HYDRA TickerStream Test Suite
Validates WS ticker message dispatch, latest_ticker storage, multi-pair
symbol mapping, and paper-mode behavior.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import TickerStream


PAIRS = ["SOL/USDC", "SOL/XBT", "XBT/USDC"]


def _make_stream(paper=False):
    return TickerStream(pairs=PAIRS, paper=paper)


class TestTickerStreamDispatch:

    def test_heartbeat_bumps_timestamp(self):
        ts = _make_stream()
        ts._last_heartbeat = 0.0
        ts._on_message({"channel": "heartbeat"})
        assert ts._last_heartbeat > time.monotonic() - 1.0

    def test_ticker_snapshot_stores_data(self):
        ts = _make_stream()
        ts._on_message({
            "channel": "ticker",
            "type": "snapshot",
            "data": [{
                "symbol": "SOL/USDC",
                "bid": 82.77, "ask": 82.81, "last": 82.84,
                "volume": 13380.0, "vwap": 83.28,
                "high": 86.17, "low": 81.35,
            }],
        })
        ticker = ts.latest_ticker("SOL/USDC")
        assert ticker is not None
        assert ticker["bid"] == 82.77
        assert ticker["ask"] == 82.81
        assert ticker["last"] == 82.84

    def test_ticker_update_overwrites(self):
        ts = _make_stream()
        ts._on_message({
            "channel": "ticker", "type": "snapshot",
            "data": [{"symbol": "SOL/USDC", "bid": 80.0, "ask": 80.1, "last": 80.0}],
        })
        ts._on_message({
            "channel": "ticker", "type": "update",
            "data": [{"symbol": "SOL/USDC", "bid": 85.0, "ask": 85.1, "last": 85.0}],
        })
        assert ts.latest_ticker("SOL/USDC")["bid"] == 85.0

    def test_multi_pair_stored_independently(self):
        """WS v2 returns SOL/BTC — mapped to our friendly SOL/XBT."""
        ts = _make_stream()
        ts._on_message({
            "channel": "ticker", "type": "snapshot",
            "data": [
                {"symbol": "SOL/USDC", "bid": 82.0, "ask": 82.1, "last": 82.0},
                {"symbol": "SOL/BTC", "bid": 0.00116, "ask": 0.00117, "last": 0.00117},
            ],
        })
        sol = ts.latest_ticker("SOL/USDC")
        xbt = ts.latest_ticker("SOL/XBT")
        assert sol is not None and sol["bid"] == 82.0
        assert xbt is not None and xbt["bid"] == 0.00116

    def test_unknown_symbol_ignored(self):
        ts = _make_stream()
        ts._on_message({
            "channel": "ticker", "type": "update",
            "data": [{"symbol": "ETH/USDC", "bid": 3500.0, "ask": 3501.0}],
        })
        assert ts.latest_ticker("ETH/USDC") is None

    def test_status_message_ignored(self):
        ts = _make_stream()
        ts._last_heartbeat = 12345.0
        ts._on_message({"channel": "status", "data": [{"system": "online"}]})
        assert ts._last_heartbeat == 12345.0

    def test_non_dict_data_entries_skipped(self):
        ts = _make_stream()
        ts._on_message({
            "channel": "ticker", "type": "update",
            "data": ["not a dict", None],
        })
        assert ts.latest_ticker("SOL/USDC") is None

    def test_latest_ticker_returns_none_before_data(self):
        ts = _make_stream()
        assert ts.latest_ticker("SOL/USDC") is None

    def test_ticker_channel_bumps_heartbeat(self):
        ts = _make_stream()
        ts._last_heartbeat = 0.0
        ts._on_message({
            "channel": "ticker", "type": "update",
            "data": [{"symbol": "SOL/USDC", "bid": 82.0, "ask": 82.1}],
        })
        assert ts._last_heartbeat > time.monotonic() - 1.0

    def test_btc_usdc_maps_to_xbt_usdc(self):
        ts = _make_stream()
        ts._on_message({
            "channel": "ticker", "type": "snapshot",
            "data": [{"symbol": "BTC/USDC", "bid": 95000.0, "ask": 95100.0, "last": 95050.0}],
        })
        ticker = ts.latest_ticker("XBT/USDC")
        assert ticker is not None
        assert ticker["bid"] == 95000.0


class TestTickerStreamBuildCmd:

    def test_build_cmd_includes_pairs(self):
        ts = _make_stream()
        cmd = ts._build_cmd()
        assert "ws ticker" in cmd
        assert "-o json" in cmd
        assert "--snapshot true" in cmd

    def test_build_cmd_uses_exec(self):
        ts = _make_stream()
        cmd = ts._build_cmd()
        assert cmd.startswith("exec ")


class TestTickerStreamPaper:

    def test_paper_mode_healthy(self):
        ts = _make_stream(paper=True)
        ts.start()
        assert ts.healthy

    def test_paper_mode_start_returns_true(self):
        ts = _make_stream(paper=True)
        assert ts.start() is True


class TestTickerStreamLabel:

    def test_label(self):
        ts = _make_stream()
        assert ts._stream_label() == "TICKER_WS"


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestTickerStreamDispatch,
        TestTickerStreamBuildCmd,
        TestTickerStreamPaper,
        TestTickerStreamLabel,
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
    print(f"  TickerStream Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
