"""
HYDRA BookStream Test Suite
Validates WS book message dispatch, REST-format conversion for OrderBookAnalyzer,
multi-pair symbol mapping, and paper-mode behavior.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import BookStream
from hydra_engine import OrderBookAnalyzer


PAIRS = ["SOL/USDC", "SOL/XBT", "XBT/USDC"]


def _make_stream(paper=False):
    return BookStream(pairs=PAIRS, depth=10, paper=paper)


class TestBookStreamDispatch:

    def test_heartbeat_bumps_timestamp(self):
        bs = _make_stream()
        bs._last_heartbeat = 0.0
        bs._on_message({"channel": "heartbeat"})
        assert bs._last_heartbeat > time.monotonic() - 1.0

    def test_snapshot_stores_book(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": [{
                "symbol": "SOL/USDC",
                "bids": [{"price": 82.72, "qty": 1.39}, {"price": 82.70, "qty": 61.65}],
                "asks": [{"price": 82.78, "qty": 73.83}, {"price": 82.79, "qty": 25.52}],
                "checksum": 12345,
            }],
        })
        book = bs.latest_book("SOL/USDC")
        assert book is not None
        assert len(book["bids"]) == 2
        assert len(book["asks"]) == 2

    def test_converts_to_rest_format(self):
        """WS uses {price, qty} dicts; REST uses [price, qty, ts] lists."""
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": [{
                "symbol": "SOL/USDC",
                "bids": [{"price": 82.72, "qty": 1.39}],
                "asks": [{"price": 82.78, "qty": 73.83}],
            }],
        })
        book = bs.latest_book("SOL/USDC")
        bid = book["bids"][0]
        ask = book["asks"][0]
        # REST format: [price, volume, timestamp]
        assert isinstance(bid, list) and len(bid) == 3
        assert bid[0] == 82.72
        assert bid[1] == 1.39
        assert isinstance(ask, list) and len(ask) == 3
        assert ask[0] == 82.78
        assert ask[1] == 73.83

    def test_compatible_with_order_book_analyzer(self):
        """Book from WS stream should be directly usable by OrderBookAnalyzer."""
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": [{
                "symbol": "SOL/USDC",
                "bids": [
                    {"price": 82.72, "qty": 10.0},
                    {"price": 82.70, "qty": 10.0},
                    {"price": 82.68, "qty": 10.0},
                ],
                "asks": [
                    {"price": 82.78, "qty": 10.0},
                    {"price": 82.80, "qty": 10.0},
                    {"price": 82.82, "qty": 10.0},
                ],
            }],
        })
        book = bs.latest_book("SOL/USDC")
        result = OrderBookAnalyzer.analyze(book, "BUY")
        assert result["bid_volume"] > 0
        assert result["ask_volume"] > 0
        assert result["imbalance_ratio"] > 0
        assert result["spread_bps"] > 0

    def test_update_overwrites_book(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": [{"symbol": "SOL/USDC",
                       "bids": [{"price": 80.0, "qty": 1.0}],
                       "asks": [{"price": 81.0, "qty": 1.0}]}],
        })
        bs._on_message({
            "channel": "book", "type": "update",
            "data": [{"symbol": "SOL/USDC",
                       "bids": [{"price": 85.0, "qty": 2.0}],
                       "asks": [{"price": 86.0, "qty": 2.0}]}],
        })
        book = bs.latest_book("SOL/USDC")
        assert book["bids"][0][0] == 85.0

    def test_multi_pair_independent(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": [
                {"symbol": "SOL/USDC",
                 "bids": [{"price": 82.0, "qty": 1.0}],
                 "asks": [{"price": 83.0, "qty": 1.0}]},
                {"symbol": "SOL/XBT",
                 "bids": [{"price": 0.00116, "qty": 5.0}],
                 "asks": [{"price": 0.00117, "qty": 5.0}]},
            ],
        })
        sol = bs.latest_book("SOL/USDC")
        xbt = bs.latest_book("SOL/XBT")
        assert sol["bids"][0][0] == 82.0
        assert xbt["bids"][0][0] == 0.00116

    def test_unknown_symbol_ignored(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "update",
            "data": [{"symbol": "ETH/USDC",
                       "bids": [{"price": 3500.0, "qty": 1.0}],
                       "asks": [{"price": 3501.0, "qty": 1.0}]}],
        })
        assert bs.latest_book("ETH/USDC") is None

    def test_non_dict_data_entries_skipped(self):
        bs = _make_stream()
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": ["not a dict"],
        })
        assert bs.latest_book("SOL/USDC") is None

    def test_latest_book_none_before_data(self):
        bs = _make_stream()
        assert bs.latest_book("SOL/USDC") is None

    def test_book_channel_bumps_heartbeat(self):
        bs = _make_stream()
        bs._last_heartbeat = 0.0
        bs._on_message({
            "channel": "book", "type": "snapshot",
            "data": [{"symbol": "SOL/USDC",
                       "bids": [{"price": 82.0, "qty": 1.0}],
                       "asks": [{"price": 83.0, "qty": 1.0}]}],
        })
        assert bs._last_heartbeat > time.monotonic() - 1.0


class TestBookStreamBuildCmd:

    def test_build_cmd(self):
        bs = _make_stream()
        cmd = bs._build_cmd()
        assert "ws book" in cmd
        assert "--depth 10" in cmd
        assert "-o json" in cmd
        assert "--snapshot true" in cmd
        assert cmd.startswith("exec ")


class TestBookStreamPaper:

    def test_paper_mode_healthy(self):
        bs = _make_stream(paper=True)
        bs.start()
        assert bs.healthy

    def test_paper_start_returns_true(self):
        bs = _make_stream(paper=True)
        assert bs.start() is True


class TestBookStreamLabel:

    def test_label(self):
        bs = _make_stream()
        assert bs._stream_label() == "BOOK_WS"


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestBookStreamDispatch,
        TestBookStreamBuildCmd,
        TestBookStreamPaper,
        TestBookStreamLabel,
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
    print(f"  BookStream Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
