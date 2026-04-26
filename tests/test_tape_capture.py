import threading
import time
from hydra_history_store import HistoryStore
from hydra_tape_capture import TapeCapture


def test_capture_writes_closed_candle(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cap = TapeCapture(store, queue_max=8)
    cap.start()
    try:
        cap.on_candle("BTC/USD", {
            "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10,
            "interval_begin": "2024-01-01T00:00:00.000Z",
            "interval": 60,
        })
        cap.flush(timeout=2.0)
    finally:
        cap.stop()
    rows = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert len(rows) == 1
    assert rows[0].source == "tape"


def test_capture_drops_when_queue_full(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cap = TapeCapture(store, queue_max=1)
    # Don't start the worker — queue can't drain. Both calls must NOT raise.
    cap.on_candle("BTC/USD", {"close": 1, "interval_begin": "2024-01-01T00:00:00.000Z", "interval": 60})
    cap.on_candle("BTC/USD", {"close": 2, "interval_begin": "2024-01-01T00:01:00.000Z", "interval": 60})
    assert cap.dropped >= 1
