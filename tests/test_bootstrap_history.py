"""Tests for tools.bootstrap_history — Kraken trade archive → 1h OHLC candles."""
import io
import os
import zipfile
from pathlib import Path
import pytest
from hydra_history_store import HistoryStore
from tools.bootstrap_history import bootstrap_zip, kraken_pair_to_canonical


def _make_fixture_zip(path, pair_filename, trades):
    """Build a Kraken-archive-shaped zip with one pair file."""
    buf = io.StringIO()
    for ts, price, vol in trades:
        buf.write(f"{ts},{price},{vol}\n")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"TimeAndSales_Combined/{pair_filename}", buf.getvalue())


def test_bootstrap_rolls_trades_to_1h_candles(tmp_path):
    z = tmp_path / "k.zip"
    # Two 1h buckets: [00:00, 01:00) gets 3 trades; [01:00, 02:00) gets 2.
    # Timestamps are aligned to 1h bucket boundaries (grain=3600).
    # base_a = 1699999200 (hour boundary), base_b = base_a + 3600 = 1700002800.
    # Bucket A gets 3 trades; bucket B gets 2 trades.
    _make_fixture_zip(z, "XBTUSD.csv", [
        (1_699_999_200, 10.0, 1.0),  # bucket A open
        (1_700_000_200, 12.0, 1.0),  # bucket A high
        (1_700_002_200, 9.0, 1.0),   # bucket A low+close
        (1_700_002_900, 11.0, 1.0),  # bucket B open
        (1_700_006_000, 13.0, 2.0),  # bucket B high+close
    ])
    db = tmp_path / "h.sqlite"
    bootstrap_zip(str(z), str(db), pairs=["XBTUSD"], grain_sec=3600)
    store = HistoryStore(str(db))
    rows = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert len(rows) == 2
    a, b = rows
    # bucket A: open=10, high=12, low=9, close=9, vol=3
    assert (a.open, a.high, a.low, a.close, a.volume) == (10.0, 12.0, 9.0, 9.0, 3.0)
    # bucket B: open=11, high=13, low=11, close=13, vol=3
    assert (b.open, b.high, b.low, b.close, b.volume) == (11.0, 13.0, 11.0, 13.0, 3.0)
    assert a.source == "kraken_archive"


def test_kraken_pair_alias_resolution():
    assert kraken_pair_to_canonical("XBTUSD") == "BTC/USD"
    assert kraken_pair_to_canonical("SOLUSD") == "SOL/USD"
    assert kraken_pair_to_canonical("SOLXBT") == "SOL/BTC"
