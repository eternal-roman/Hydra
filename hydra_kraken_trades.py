"""HYDRA Kraken Personal Trade History — SQLite-backed state store.

Authoritative record of every fill Kraken's private API has on the
configured account. Distinct from `hydra_order_journal.json` (Hydra-only,
write-when-Hydra-places) and `hydra_history.sqlite` (public OHLC market
history). This file is the ledger-of-truth for accounting, P&L
reconciliation, and tax reporting.

Stdlib-only. Single-file SQLite at `hydra_kraken_trades.sqlite` by default.

Schema (canonical, in perpetuity):

  trades(
    txid          TEXT PRIMARY KEY,    -- Kraken trade tx ID
    ordertxid     TEXT NOT NULL,
    postxid       TEXT,
    pair_kraken   TEXT NOT NULL,       -- raw "SOLUSD" / "XXBTZUSD" / etc
    pair_canonical TEXT NOT NULL,      -- "SOL/USD" / "BTC/USD" — registry-canonical
    side          TEXT NOT NULL,       -- "buy" | "sell"
    ordertype     TEXT NOT NULL,
    price         REAL NOT NULL,
    vol           REAL NOT NULL,
    cost          REAL NOT NULL,       -- gross trade cost in quote currency
    fee           REAL NOT NULL,       -- fee charged in quote currency
    margin        REAL NOT NULL DEFAULT 0,
    leverage      REAL NOT NULL DEFAULT 0,
    maker         INTEGER NOT NULL DEFAULT 1,    -- 1=maker, 0=taker
    time_unix     REAL NOT NULL,       -- fractional seconds, Kraken precision
    trade_id      INTEGER NOT NULL,
    raw_json      TEXT NOT NULL,       -- full original Kraken response (forensic)
    ingested_at   INTEGER NOT NULL
  )
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional

SCHEMA_VERSION = 1

# Kraken pair codes -> canonical "BASE/QUOTE". Extend as new pairs traded.
_PAIR_TO_CANONICAL = {
    "SOLUSD":   "SOL/USD",
    "SOLUSDC":  "SOL/USDC",
    "SOLUSDT":  "SOL/USDT",
    "SOLXBT":   "SOL/BTC",
    "SOLBTC":   "SOL/BTC",
    "XBTUSD":   "BTC/USD",
    "XBTUSDC":  "BTC/USDC",
    "XBTUSDT":  "BTC/USDT",
    "XXBTZUSD": "BTC/USD",
    "BTCUSD":   "BTC/USD",
    "BTCUSDC":  "BTC/USDC",
}


def kraken_pair_to_canonical(p: str) -> str:
    return _PAIR_TO_CANONICAL.get(p, p)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  txid           TEXT PRIMARY KEY,
  ordertxid      TEXT NOT NULL,
  postxid        TEXT,
  pair_kraken    TEXT NOT NULL,
  pair_canonical TEXT NOT NULL,
  side           TEXT NOT NULL,
  ordertype      TEXT NOT NULL,
  price          REAL NOT NULL,
  vol            REAL NOT NULL,
  cost           REAL NOT NULL,
  fee            REAL NOT NULL,
  margin         REAL NOT NULL DEFAULT 0,
  leverage       REAL NOT NULL DEFAULT 0,
  maker          INTEGER NOT NULL DEFAULT 1,
  time_unix      REAL NOT NULL,
  trade_id       INTEGER NOT NULL,
  raw_json       TEXT NOT NULL,
  ingested_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_trades_time ON trades(time_unix);
CREATE INDEX IF NOT EXISTS ix_trades_pair_time ON trades(pair_canonical, time_unix);
"""


@dataclass(frozen=True)
class Trade:
    txid: str
    ordertxid: str
    postxid: Optional[str]
    pair_kraken: str
    pair_canonical: str
    side: str
    ordertype: str
    price: float
    vol: float
    cost: float
    fee: float
    margin: float
    leverage: float
    maker: bool
    time_unix: float
    trade_id: int


class KrakenTradesStore:
    """Append-only ledger of Kraken-private trade history.

    Idempotent inserts (PRIMARY KEY on txid). Safe to re-run any sync.
    """

    def __init__(self, path: str = "hydra_kraken_trades.sqlite"):
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
            existing = None
            try:
                cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                row = cur.fetchone()
                if row is not None:
                    existing = int(row[0])
            except sqlite3.OperationalError:
                existing = None
            if existing is not None and existing != SCHEMA_VERSION:
                raise RuntimeError(
                    f"hydra_kraken_trades: schema_version={existing} on disk, "
                    f"code expects {SCHEMA_VERSION}."
                )
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    def upsert_kraken_trades(self, kraken_trades_dict: dict) -> int:
        """Bulk-insert from the raw Kraken trades-history `trades` map.
        Returns count of newly-inserted rows (existing txids are skipped)."""
        if not kraken_trades_dict:
            return 0
        now = int(time.time())
        n = 0
        with self._lock, self._conn() as conn:
            for txid, t in kraken_trades_dict.items():
                pair_kraken = str(t.get("pair", "") or "")
                pair_canonical = kraken_pair_to_canonical(pair_kraken)
                try:
                    res = conn.execute(
                        """INSERT OR IGNORE INTO trades
                           (txid, ordertxid, postxid, pair_kraken, pair_canonical,
                            side, ordertype, price, vol, cost, fee, margin,
                            leverage, maker, time_unix, trade_id, raw_json,
                            ingested_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(txid),
                            str(t.get("ordertxid", "") or ""),
                            t.get("postxid") or None,
                            pair_kraken,
                            pair_canonical,
                            str(t.get("type", "") or ""),
                            str(t.get("ordertype", "") or ""),
                            float(t.get("price", 0) or 0),
                            float(t.get("vol", 0) or 0),
                            float(t.get("cost", 0) or 0),
                            float(t.get("fee", 0) or 0),
                            float(t.get("margin", 0) or 0),
                            float(t.get("leverage", 0) or 0),
                            1 if t.get("maker", True) else 0,
                            float(t.get("time", 0) or 0),
                            int(t.get("trade_id", 0) or 0),
                            json.dumps(t, sort_keys=True),
                            now,
                        ),
                    )
                    if res.rowcount > 0:
                        n += 1
                except (TypeError, ValueError, sqlite3.IntegrityError) as e:
                    print(f"  [KRAKEN-TRADES] skip {txid}: {type(e).__name__}: {e}")
            conn.commit()
        return n

    def latest_time(self) -> Optional[float]:
        """Most recent trade's time_unix, or None if empty.

        Used as the `--start` cursor for incremental syncs (Kraken's
        trades-history `--start` is exclusive — pass the last seen time
        verbatim and Kraken returns trades strictly after it)."""
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(time_unix) FROM trades").fetchone()
        return row[0] if row and row[0] is not None else None

    def count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return int(row[0]) if row else 0

    def trades_for_pair(self, pair_canonical: str,
                        start_ts: float = 0.0,
                        end_ts: float = 9_999_999_999.0) -> Iterator[Trade]:
        """Yield Trades for a given canonical pair within a time window.

        Materializes inside the connection (T2-pattern from hydra_history_store
        — generators yielding from inside a `with conn` block break under
        non-CPython runtimes)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT txid, ordertxid, postxid, pair_kraken, pair_canonical,
                          side, ordertype, price, vol, cost, fee, margin, leverage,
                          maker, time_unix, trade_id
                   FROM trades
                   WHERE pair_canonical=? AND time_unix>=? AND time_unix<=?
                   ORDER BY time_unix ASC""",
                (pair_canonical, start_ts, end_ts),
            ).fetchall()
        for r in rows:
            yield Trade(
                txid=r[0], ordertxid=r[1], postxid=r[2],
                pair_kraken=r[3], pair_canonical=r[4],
                side=r[5], ordertype=r[6],
                price=r[7], vol=r[8], cost=r[9], fee=r[10],
                margin=r[11], leverage=r[12],
                maker=bool(r[13]), time_unix=r[14], trade_id=r[15],
            )

    def list_pairs(self) -> List[str]:
        """Distinct canonical pairs present in the store, alphabetic."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT pair_canonical FROM trades ORDER BY pair_canonical"
            ).fetchall()
        return [r[0] for r in rows]
