"""HYDRA Canonical Historical Store — SQLite-backed OHLC + regression snapshots.

Stdlib-only. Single source of truth for backtest history (Mode B) and
release regression snapshots (Mode C). See
docs/superpowers/specs/2026-04-26-research-tab-redesign-design.md.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ohlc (
  pair         TEXT    NOT NULL,
  grain_sec    INTEGER NOT NULL,
  ts           INTEGER NOT NULL,
  open         REAL    NOT NULL,
  high         REAL    NOT NULL,
  low          REAL    NOT NULL,
  close        REAL    NOT NULL,
  volume       REAL    NOT NULL,
  source       TEXT    NOT NULL,
  ingested_at  INTEGER NOT NULL,
  PRIMARY KEY (pair, grain_sec, ts)
);

CREATE INDEX IF NOT EXISTS ix_ohlc_pair_grain_ts
  ON ohlc(pair, grain_sec, ts);
"""


class HistoryStore:
    def __init__(self, path: str = "hydra_history.sqlite"):
        self.path = path
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
