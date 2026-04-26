import os
import sqlite3
import tempfile
import pytest
from hydra_history_store import HistoryStore, SCHEMA_VERSION, CandleRow


def test_init_creates_schema(tmp_path):
    db = tmp_path / "h.sqlite"
    store = HistoryStore(str(db))
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = [r[0] for r in rows]
    assert "ohlc" in names
    assert "meta" in names


def test_schema_version_recorded(tmp_path):
    db = tmp_path / "h.sqlite"
    HistoryStore(str(db))
    with sqlite3.connect(str(db)) as conn:
        v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert v is not None
    assert int(v[0]) == SCHEMA_VERSION


def test_upsert_and_fetch(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    rows = [
        CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                  open=10.0, high=11.0, low=9.0, close=10.5, volume=100.0,
                  source="kraken_archive"),
        CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_003_600,
                  open=10.5, high=12.0, low=10.0, close=11.5, volume=200.0,
                  source="kraken_archive"),
    ]
    n = store.upsert_candles(rows)
    assert n == 2
    fetched = list(store.fetch("BTC/USD", 3600, 1_700_000_000, 1_700_003_600))
    assert len(fetched) == 2
    assert fetched[0].close == 10.5
    assert fetched[1].close == 11.5


def test_archive_tier_immutable(tmp_path):
    """tape/rest writes must NOT overwrite kraken_archive rows."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    archive_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                            open=10.0, high=11.0, low=9.0, close=10.5,
                            volume=100.0, source="kraken_archive")
    store.upsert_candles([archive_row])
    tape_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                         open=99.0, high=99.0, low=99.0, close=99.0,
                         volume=99.0, source="tape")
    store.upsert_candles([tape_row])
    [got] = list(store.fetch("BTC/USD", 3600, 1_700_000_000, 1_700_000_000))
    assert got.close == 10.5  # archive preserved


def test_rest_overwrites_tape(tmp_path):
    """kraken_rest is more authoritative than tape for trailing window."""
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    tape_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                         open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
                         source="tape")
    store.upsert_candles([tape_row])
    rest_row = CandleRow(pair="BTC/USD", grain_sec=3600, ts=1_700_000_000,
                         open=2.0, high=2.0, low=2.0, close=2.0, volume=2.0,
                         source="kraken_rest")
    store.upsert_candles([rest_row])
    [got] = list(store.fetch("BTC/USD", 3600, 1_700_000_000, 1_700_000_000))
    assert got.close == 2.0
