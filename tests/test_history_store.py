import os
import sqlite3
import tempfile
import pytest
from hydra_history_store import HistoryStore, SCHEMA_VERSION


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
