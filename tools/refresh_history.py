"""Daily REST refresh of the trailing window for the canonical OHLC store.

Walks each (pair, grain_sec) currently present, calls KrakenCLI.ohlc(),
and upserts results. Tier policy in HistoryStore prevents overwrites of
kraken_archive rows.

Usage:
    python -m tools.refresh_history [--db hydra_history.sqlite]
"""
from __future__ import annotations

import argparse
from typing import Any, List, Optional

from hydra_history_store import CandleRow, HistoryStore


def refresh_pair(store: HistoryStore, pair: str, grain_sec: int,
                 cli: Optional[Any] = None) -> int:
    """Refresh one (pair, grain_sec) combination. cli is injectable for tests."""
    if cli is None:
        from hydra_kraken_cli import KrakenCLI
        cli = KrakenCLI
    rows = cli.ohlc(pair, interval=grain_sec // 60) or []
    out: List[CandleRow] = []
    for r in rows:
        ts = int(float(r.get("timestamp", 0)))
        if ts <= 0:
            continue
        out.append(CandleRow(
            pair=pair, grain_sec=grain_sec, ts=ts,
            open=float(r.get("open", 0)), high=float(r.get("high", 0)),
            low=float(r.get("low", 0)), close=float(r.get("close", 0)),
            volume=float(r.get("volume", 0)),
            source="kraken_rest",
        ))
    return store.upsert_candles(out)


def refresh_all(db_path: str = "hydra_history.sqlite") -> int:
    store = HistoryStore(db_path)
    total = 0
    for pair, grain_sec in store.list_pairs():
        n = refresh_pair(store, pair, grain_sec)
        print(f"  [REFRESH] {pair} {grain_sec}s: {n} rows touched")
        total += n
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="hydra_history.sqlite")
    args = ap.parse_args()
    refresh_all(args.db)


if __name__ == "__main__":
    main()
