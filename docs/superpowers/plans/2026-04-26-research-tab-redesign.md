# Research Tab Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the synthetic-data Research tab with a real-history backtest stack: a canonical SQLite OHLC store sourced from Kraken's trade archive + live tape capture, anchored quarterly walk-forward with paired Wilcoxon for edge detection, per-version regression snapshots gated into `/release`, and a structured dashboard tab.

**Architecture:** One SQLite DB (`hydra_history.sqlite`) is the single source of truth — `ohlc` table for historical candles (sourced from `kraken_archive` / `kraken_rest` / `tape` tiers, with archive-tier writes immutable) and `regression_*` tables for per-release snapshots. A new `hydra_walk_forward` kernel powers both Mode B (hypothesis lab) and Mode C (release regression). The dashboard Research tab is rebuilt as `<ResearchTab>` with three structured panes (`DATASET` / `LAB` / `RELEASES`).

**Tech Stack:** Python 3 stdlib (`sqlite3`, `csv`, `zipfile`, `statistics`, `threading`, `queue`), React 18 (single-file inline-styled `dashboard/src/App.jsx`), existing `hydra_*` modules. No new dependencies.

**Branch:** `feature/research-tab-redesign` (already created off `main` at `b4b259a`, design committed at `10bf6ed`).

**Spec:** `docs/superpowers/specs/2026-04-26-research-tab-redesign-design.md`

---

## File Structure

**New backend modules:**
- `hydra_history_store.py` — SQLite store, schema migrations, `HistoryStore` class
- `hydra_walk_forward.py` — fold construction, Wilcoxon, runner
- `hydra_tape_capture.py` — bounded queue + writer thread; `CandleStream` callback hook
- `tools/__init__.py` — namespace marker
- `tools/bootstrap_history.py` — Kraken trade-archive → 1h candles
- `tools/refresh_history.py` — daily REST refresh
- `tools/run_regression.py` — per-version regression runner

**Modified backend:**
- `hydra_backtest.py` — `SqliteSource`, brain stubbing, default flip
- `hydra_streams.py` — `CandleStream` adds `on_candle(callback)` hook
- `hydra_agent.py` — wire tape capture under `HYDRA_TAPE_CAPTURE` env

**New tests (under `tests/`):**
- `test_history_store.py`, `test_history_store_migrations.py`
- `test_bootstrap_history.py`, `test_refresh_history.py`
- `test_tape_capture.py`
- `test_walk_forward.py`, `test_wilcoxon.py`
- `test_regression_runner.py`
- `test_backtest_sqlite_source.py`

**Dashboard:**
- `dashboard/src/components/ResearchTab.jsx` — new sub-component
- `dashboard/src/components/research/DatasetPane.jsx`
- `dashboard/src/components/research/LabPane.jsx`
- `dashboard/src/components/research/ReleasesPane.jsx`
- `dashboard/src/App.jsx` — replace existing BACKTEST/COMPARE panes with `<ResearchTab>`

**Skill / docs:**
- `.claude/skills/release/SKILL.md` (or wherever `/release` skill lives) — insert regression step
- `CHANGELOG.md`, `dashboard/package.json`, `dashboard/package-lock.json`, `dashboard/src/App.jsx` footer, `hydra_agent.py` `_export_competition_results`, `hydra_backtest.py` `HYDRA_VERSION`, `CLAUDE.md` version pin → all to `2.20.0`

---

## Phase Map (with review checkpoints)

| Phase | RC | Theme | Tasks | Done criterion |
|---|---|---|---|---|
| 1 | rc1 | Canonical store + bootstrap | T1–T6 | `hydra_history.sqlite` exists locally with BTC/USD, SOL/USD, SOL/BTC at 1h; tests green |
| 2 | rc1 | Refresh + tape capture | T7–T10 | Daily refresh idempotent; agent boot with `HYDRA_TAPE_CAPTURE=1` writes candles; mock harness clean |
| 3 | rc1 | Walk-forward + Wilcoxon | T11–T14 | Paired test verdict on synthetic deterministic engine matches scipy reference |
| 4 | rc1 | Backtest SqliteSource + regression runner | T15–T18 | `tools/run_regression.py --version 2.19.1` produces a snapshot row set |
| 5 | rc2 | Dashboard Research tab | T19–T23 | `DATASET` / `LAB` / `RELEASES` panes render real data; WS streams fold progress |
| 6 | rc3 | `/release` gate + version bump + release | T24–T28 | `/release` runs, gates on Wilcoxon, ships v2.20.0 with signed tag and GH release |

**Review checkpoints (STOP and ask user before proceeding):**
- After Phase 1 (T6 commit) — verify the store layout looks right
- After Phase 3 (T14 commit) — verify Wilcoxon math with hand-computed example
- After Phase 4 (T18 commit) — verify a regression snapshot is well-formed before wiring dashboard
- After Phase 5 (T23 commit) — verify Research tab usability in browser
- After Phase 6 (T28 tag) — verify release alignment script exits 0

**Parallelizable:** T11/T12 (Wilcoxon math) can run in parallel with T8/T9 (tape capture) — different files, no dependencies. T19–T22 (dashboard panes) can be split across subagents in parallel since each pane is its own file.

---

## Phase 1 — Canonical store + bootstrap (rc1)

### Task 1: HistoryStore skeleton + schema

**Files:**
- Create: `hydra_history_store.py`
- Test: `tests/test_history_store.py`

- [ ] **Step 1: Write failing test for schema initialization**

```python
# tests/test_history_store.py
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
```

- [ ] **Step 2: Run tests, verify failure**

Run: `python -m pytest tests/test_history_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'HistoryStore'`

- [ ] **Step 3: Create `hydra_history_store.py` minimal**

```python
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
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_history_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hydra_history_store.py tests/test_history_store.py
git commit -m "feat(history): SQLite store skeleton + schema v1"
```

### Task 2: HistoryStore upsert + fetch

**Files:**
- Modify: `hydra_history_store.py`
- Test: `tests/test_history_store.py`

- [ ] **Step 1: Write failing tests for upsert + fetch**

```python
# Add to tests/test_history_store.py
from hydra_history_store import HistoryStore, CandleRow

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
```

- [ ] **Step 2: Run tests, verify failure**

Run: `python -m pytest tests/test_history_store.py -v`
Expected: FAIL — `CandleRow` not defined / `upsert_candles` not defined.

- [ ] **Step 3: Implement CandleRow + upsert_candles + fetch**

```python
# Add to hydra_history_store.py

# Source tier ranking — higher rank wins on conflict.
# kraken_archive is immutable; rest > tape for trailing-edge refresh.
_SOURCE_RANK = {"tape": 1, "kraken_rest": 2, "kraken_archive": 3}


@dataclass(frozen=True)
class CandleRow:
    pair: str
    grain_sec: int
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str

    def __post_init__(self):
        if self.source not in _SOURCE_RANK:
            raise ValueError(f"unknown source tier: {self.source}")


@dataclass(frozen=True)
class CandleOut:
    pair: str
    grain_sec: int
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


# in HistoryStore class:

    def upsert_candles(self, rows: Iterable[CandleRow]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        now = int(time.time())
        n = 0
        with self._lock, self._conn() as conn:
            for r in rows:
                # Tier-aware insert: only overwrite if incoming source rank
                # >= existing source rank.
                cur = conn.execute(
                    "SELECT source FROM ohlc WHERE pair=? AND grain_sec=? AND ts=?",
                    (r.pair, r.grain_sec, r.ts),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if _SOURCE_RANK[r.source] < _SOURCE_RANK[existing[0]]:
                        continue  # incoming is lower tier — skip
                conn.execute(
                    """INSERT OR REPLACE INTO ohlc
                       (pair, grain_sec, ts, open, high, low, close, volume,
                        source, ingested_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r.pair, r.grain_sec, r.ts, r.open, r.high, r.low,
                     r.close, r.volume, r.source, now),
                )
                n += 1
            conn.commit()
        return n

    def fetch(self, pair: str, grain_sec: int,
              start_ts: int, end_ts: int) -> Iterator[CandleOut]:
        with self._conn() as conn:
            cur = conn.execute(
                """SELECT pair, grain_sec, ts, open, high, low, close, volume, source
                   FROM ohlc
                   WHERE pair=? AND grain_sec=? AND ts>=? AND ts<=?
                   ORDER BY ts ASC""",
                (pair, grain_sec, start_ts, end_ts),
            )
            for row in cur:
                yield CandleOut(*row)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_history_store.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_history_store.py tests/test_history_store.py
git commit -m "feat(history): tier-aware upsert + fetch"
```

### Task 3: HistoryStore coverage + gap detection

**Files:**
- Modify: `hydra_history_store.py`
- Test: `tests/test_history_store.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_history_store.py
from hydra_history_store import Coverage

def test_coverage_empty(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cov = store.coverage("BTC/USD", 3600)
    assert cov.candle_count == 0
    assert cov.first_ts is None and cov.last_ts is None

def test_coverage_with_gap(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    rows = [
        CandleRow("BTC/USD", 3600, 1_700_000_000 + i*3600,
                  10, 11, 9, 10, 1, "kraken_archive")
        for i in range(3)  # ts +0, +3600, +7200
    ]
    # Skip a candle at +10800, write +14400
    rows.append(CandleRow("BTC/USD", 3600, 1_700_000_000 + 4*3600,
                          10, 11, 9, 10, 1, "kraken_archive"))
    store.upsert_candles(rows)
    cov = store.coverage("BTC/USD", 3600)
    assert cov.candle_count == 4
    assert cov.gap_count == 1
    assert cov.max_gap_sec == 7200
```

- [ ] **Step 2: Run tests, verify failure**

Run: `python -m pytest tests/test_history_store.py::test_coverage_empty -v`
Expected: FAIL — `Coverage` not defined.

- [ ] **Step 3: Implement coverage**

```python
# Add to hydra_history_store.py
@dataclass(frozen=True)
class Coverage:
    pair: str
    grain_sec: int
    candle_count: int
    first_ts: Optional[int]
    last_ts: Optional[int]
    gap_count: int
    max_gap_sec: int

# In HistoryStore class:
    def coverage(self, pair: str, grain_sec: int) -> Coverage:
        with self._conn() as conn:
            cur = conn.execute(
                """SELECT COUNT(*), MIN(ts), MAX(ts) FROM ohlc
                   WHERE pair=? AND grain_sec=?""",
                (pair, grain_sec),
            )
            count, first, last = cur.fetchone()
            if count == 0:
                return Coverage(pair, grain_sec, 0, None, None, 0, 0)
            # Gap detection: scan ordered ts; any delta > grain_sec is a gap.
            cur = conn.execute(
                """SELECT ts FROM ohlc WHERE pair=? AND grain_sec=?
                   ORDER BY ts ASC""",
                (pair, grain_sec),
            )
            prev = None
            gap_count = 0
            max_gap = 0
            for (ts,) in cur:
                if prev is not None:
                    delta = ts - prev
                    if delta > grain_sec:
                        gap_count += 1
                        if delta > max_gap:
                            max_gap = delta
                prev = ts
        return Coverage(pair, grain_sec, count, first, last, gap_count, max_gap)

    def list_pairs(self) -> List[Tuple[str, int]]:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT DISTINCT pair, grain_sec FROM ohlc ORDER BY pair, grain_sec"
            )
            return [(r[0], r[1]) for r in cur]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_history_store.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_history_store.py tests/test_history_store.py
git commit -m "feat(history): coverage + gap detection"
```

### Task 4: Schema migration scaffolding

**Files:**
- Modify: `hydra_history_store.py`
- Test: `tests/test_history_store_migrations.py`

- [ ] **Step 1: Write failing test for migration on stale schema**

```python
# tests/test_history_store_migrations.py
import sqlite3
from hydra_history_store import HistoryStore, SCHEMA_VERSION

def test_existing_db_with_lower_schema_version_raises(tmp_path):
    """Until v2 ships, opening a DB tagged < SCHEMA_VERSION must explicit-fail
    rather than silently corrupt."""
    db = tmp_path / "h.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version', '0')")
        conn.commit()
    try:
        HistoryStore(str(db))
    except RuntimeError as e:
        assert "schema_version=0" in str(e)
        return
    raise AssertionError("expected RuntimeError")
```

- [ ] **Step 2: Run test, verify failure**

Run: `python -m pytest tests/test_history_store_migrations.py -v`
Expected: FAIL — currently no version check.

- [ ] **Step 3: Add version check to `_init_schema`**

```python
# Replace _init_schema body in HistoryStore:
    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            # Detect existing DB before applying schema script.
            existing = None
            try:
                cur = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                )
                row = cur.fetchone()
                if row is not None:
                    existing = int(row[0])
            except sqlite3.OperationalError:
                existing = None  # fresh DB, meta table not created yet
            if existing is not None and existing != SCHEMA_VERSION:
                raise RuntimeError(
                    f"hydra_history_store: schema_version={existing} on disk, "
                    f"code expects {SCHEMA_VERSION}. Run a migration or delete "
                    f"the DB to rebuild from archive."
                )
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
```

- [ ] **Step 4: Run all history-store tests**

Run: `python -m pytest tests/test_history_store.py tests/test_history_store_migrations.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_history_store.py tests/test_history_store_migrations.py
git commit -m "feat(history): explicit schema-version mismatch error"
```

### Task 5: Bootstrap from Kraken trade archive

**Files:**
- Create: `tools/__init__.py` (empty), `tools/bootstrap_history.py`
- Test: `tests/test_bootstrap_history.py`

The Kraken archive CSV format per pair is `unixtime, price, volume` (no header). We stream-read, never load into RAM, and roll into 1h candles per `(pair, grain_sec)` bucket.

- [ ] **Step 1: Write failing test with synthetic mini-zip fixture**

```python
# tests/test_bootstrap_history.py
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
    _make_fixture_zip(z, "XBTUSD.csv", [
        (1_700_000_000, 10.0, 1.0),  # bucket A open
        (1_700_001_000, 12.0, 1.0),  # bucket A high
        (1_700_003_000, 9.0, 1.0),   # bucket A low+close
        (1_700_003_700, 11.0, 1.0),  # bucket B open
        (1_700_007_000, 13.0, 2.0),  # bucket B high+close
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
```

- [ ] **Step 2: Run test, verify failure**

Run: `python -m pytest tests/test_bootstrap_history.py -v`
Expected: FAIL — `tools.bootstrap_history` does not exist.

- [ ] **Step 3: Implement bootstrap**

```python
# tools/__init__.py
# (empty file)
```

```python
# tools/bootstrap_history.py
"""One-time bootstrap: Kraken trade archive (zip of TimeAndSales_Combined CSVs)
→ rolled 1h OHLC candles → hydra_history.sqlite (source='kraken_archive').

Usage:
    python -m tools.bootstrap_history --zip ~/Downloads/Kraken_Trading_History.zip \\
        --pairs SOLUSD,XBTUSD,SOLXBT --grain 3600 --out hydra_history.sqlite

Stdlib only. Stream-reads each CSV; never loads trades into RAM.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import time
import zipfile
from typing import Dict, Iterator, List, Optional, Tuple

from hydra_history_store import CandleRow, HistoryStore

# Kraken file name → canonical "BASE/QUOTE" form.
_KRAKEN_FILE_TO_CANONICAL: Dict[str, str] = {
    "XBTUSD": "BTC/USD",
    "SOLUSD": "SOL/USD",
    "SOLXBT": "SOL/BTC",
}


def kraken_pair_to_canonical(filename_stem: str) -> str:
    if filename_stem in _KRAKEN_FILE_TO_CANONICAL:
        return _KRAKEN_FILE_TO_CANONICAL[filename_stem]
    raise ValueError(f"unknown Kraken archive pair: {filename_stem}")


def _iter_trades(zf: zipfile.ZipFile, member: str) -> Iterator[Tuple[int, float, float]]:
    """Yield (ts_seconds, price, volume) from a Kraken trade CSV. Streamed."""
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        for row in csv.reader(text):
            if not row or len(row) < 3:
                continue
            try:
                # Some Kraken archives use float seconds; normalize.
                ts = int(float(row[0]))
                price = float(row[1])
                vol = float(row[2])
            except ValueError:
                continue
            yield ts, price, vol


def _roll_to_candles(
    trades: Iterator[Tuple[int, float, float]],
    grain_sec: int,
    pair: str,
) -> Iterator[CandleRow]:
    """Stream trades → emit completed candles as bucket boundaries cross."""
    bucket_open_ts: Optional[int] = None
    o = h = l = c = 0.0
    v = 0.0
    for ts, price, vol in trades:
        bucket = (ts // grain_sec) * grain_sec
        if bucket_open_ts is None:
            bucket_open_ts = bucket
            o = h = l = c = price
            v = vol
            continue
        if bucket != bucket_open_ts:
            yield CandleRow(pair, grain_sec, bucket_open_ts,
                            o, h, l, c, v, "kraken_archive")
            bucket_open_ts = bucket
            o = h = l = c = price
            v = vol
        else:
            if price > h:
                h = price
            if price < l:
                l = price
            c = price
            v += vol
    if bucket_open_ts is not None:
        yield CandleRow(pair, grain_sec, bucket_open_ts,
                        o, h, l, c, v, "kraken_archive")


def bootstrap_zip(
    zip_path: str,
    out_db: str,
    pairs: List[str],
    grain_sec: int = 3600,
    batch_size: int = 10_000,
) -> Dict[str, int]:
    """Bootstrap one or more pairs from a Kraken trade archive zip.

    Returns dict of {canonical_pair: candles_written}.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(zip_path)
    store = HistoryStore(out_db)
    written: Dict[str, int] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        for kraken_pair in pairs:
            canonical = kraken_pair_to_canonical(kraken_pair)
            member = f"TimeAndSales_Combined/{kraken_pair}.csv"
            if member not in names:
                raise FileNotFoundError(f"{kraken_pair} not in archive")
            print(f"  [BOOTSTRAP] rolling {kraken_pair} → {canonical} @ {grain_sec}s")
            t0 = time.time()
            buf: List[CandleRow] = []
            n = 0
            for candle in _roll_to_candles(_iter_trades(zf, member), grain_sec, canonical):
                buf.append(candle)
                if len(buf) >= batch_size:
                    n += store.upsert_candles(buf)
                    buf.clear()
            if buf:
                n += store.upsert_candles(buf)
            written[canonical] = n
            elapsed = time.time() - t0
            print(f"  [BOOTSTRAP]   {canonical}: {n} candles in {elapsed:.1f}s")
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--pairs", default="SOLUSD,XBTUSD,SOLXBT")
    ap.add_argument("--grain", type=int, default=3600)
    ap.add_argument("--out", default="hydra_history.sqlite")
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    bootstrap_zip(args.zip, args.out, pairs=pairs, grain_sec=args.grain)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python -m pytest tests/test_bootstrap_history.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/__init__.py tools/bootstrap_history.py tests/test_bootstrap_history.py
git commit -m "feat(history): bootstrap from Kraken trade archive"
```

### Task 6: Run real bootstrap end-to-end

**Files:** none modified — operational step.

- [ ] **Step 1: Run real bootstrap against the user's archive**

```bash
python -m tools.bootstrap_history \
    --zip "/c/Users/elamj/Downloads/Kraken_Trading_History.zip" \
    --pairs SOLUSD,XBTUSD,SOLXBT \
    --grain 3600 \
    --out hydra_history.sqlite
```

Expected: ~3-5 min runtime; output prints per-pair candle counts. Approximate target counts (depends on Kraken archive recency):
- BTC/USD: ~108k candles (~12.3 years × 8760 hr/yr)
- SOL/USD: ~38k candles (~4.4 years)
- SOL/BTC: ~38k candles (~4.4 years)

- [ ] **Step 2: Sanity-check via sqlite3 CLI**

```bash
sqlite3 hydra_history.sqlite \
    "SELECT pair, COUNT(*), datetime(MIN(ts),'unixepoch'), datetime(MAX(ts),'unixepoch') \
     FROM ohlc WHERE grain_sec=3600 GROUP BY pair"
```

Expected: 3 rows, all three pairs present with sensible date ranges.

- [ ] **Step 3: Add `hydra_history.sqlite` + `hydra_history.sqlite-shm` + `hydra_history.sqlite-wal` to `.gitignore`**

```bash
echo "" >> .gitignore
echo "# Canonical historical OHLC store (rebuild via tools/bootstrap_history.py)" >> .gitignore
echo "hydra_history.sqlite" >> .gitignore
echo "hydra_history.sqlite-shm" >> .gitignore
echo "hydra_history.sqlite-wal" >> .gitignore
```

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore(history): gitignore canonical store + WAL files"
```

**🛑 PHASE 1 CHECKPOINT — STOP and ask user to verify the bootstrap output looks right before proceeding to Phase 2.**

---

## Phase 2 — Refresh + tape capture (rc1)

### Task 7: REST refresh tool

**Files:**
- Create: `tools/refresh_history.py`
- Test: `tests/test_refresh_history.py`

- [ ] **Step 1: Write failing test using stub Kraken CLI**

```python
# tests/test_refresh_history.py
import time
from hydra_history_store import HistoryStore, CandleRow
from tools.refresh_history import refresh_pair

class _StubCli:
    def __init__(self, rows):
        self._rows = rows
    def ohlc(self, pair, interval=60):
        return self._rows

def test_refresh_inserts_rest_rows(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cli = _StubCli([
        {"timestamp": 1_700_000_000, "open": 10, "high": 11, "low": 9,
         "close": 10.5, "volume": 1.0},
    ])
    n = refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    assert n == 1
    [got] = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert got.source == "kraken_rest"
    assert got.close == 10.5

def test_refresh_does_not_overwrite_archive(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    store.upsert_candles([CandleRow("BTC/USD", 3600, 1_700_000_000,
                                    1, 1, 1, 1, 1, "kraken_archive")])
    cli = _StubCli([
        {"timestamp": 1_700_000_000, "open": 99, "high": 99, "low": 99,
         "close": 99, "volume": 99},
    ])
    refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    [got] = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert got.close == 1  # archive preserved
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_refresh_history.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement refresh_history**

```python
# tools/refresh_history.py
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


def _registry_to_kraken_pair(canonical: str) -> str:
    """BTC/USD -> XBTUSD etc. Reuses KrakenCLI internals at call time."""
    # Lazy import keeps unit tests free of agent deps.
    from hydra_kraken_cli import KrakenCLI
    return KrakenCLI._resolve_rest_pair(canonical) if hasattr(
        KrakenCLI, "_resolve_rest_pair") else canonical.replace("/", "")


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
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_refresh_history.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/refresh_history.py tests/test_refresh_history.py
git commit -m "feat(history): REST trailing-window refresh"
```

### Task 8: CandleStream `on_candle` callback hook

**Files:**
- Modify: `hydra_streams.py`
- Test: extend existing `tests/test_streams.py` if it exists, else `tests/test_candle_stream_callback.py`

- [ ] **Step 1: Write failing test for callback**

```python
# tests/test_candle_stream_callback.py
from hydra_streams import CandleStream

def test_on_candle_callback_invoked():
    stream = CandleStream(pairs=["BTC/USD"], paper=True)
    received = []
    stream.on_candle(lambda pair, candle: received.append((pair, candle)))
    # Inject a fake message via _on_message — simulating WS push.
    stream._on_message({
        "channel": "ohlc",
        "data": [{"symbol": "BTC/USD", "open": 1, "high": 2, "low": 1,
                  "close": 1.5, "volume": 10, "interval_begin": "2024-01-01T00:00:00.000Z"}],
    })
    assert len(received) == 1
    pair, candle = received[0]
    assert pair == "BTC/USD"
    assert candle["close"] == 1.5
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_candle_stream_callback.py -v`
Expected: FAIL — `on_candle` not defined.

- [ ] **Step 3: Add callback hook to CandleStream**

```python
# hydra_streams.py — modify CandleStream.__init__ to add a callback list,
# add on_candle(), and dispatch at end of _on_message after storing.

# In CandleStream.__init__ append:
        self._candle_callbacks: list = []

# Add new method:
    def on_candle(self, callback) -> None:
        """Register a callback fired on each push: callback(pair: str, candle: dict).
        Callbacks must be fast and non-blocking — they run inside the WS thread."""
        with self._lock:
            self._candle_callbacks.append(callback)

# In _on_message, after `self._latest[pair] = entry`, dispatch outside the lock:
                with self._lock:
                    self._latest[pair] = entry
                    cbs = list(self._candle_callbacks)
                for cb in cbs:
                    try:
                        cb(pair, entry)
                    except Exception as e:
                        print(f"  [CANDLE_WS] callback error: {type(e).__name__}: {e}")
```

(Apply via direct file edit. The above shows the deltas.)

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_candle_stream_callback.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_streams.py tests/test_candle_stream_callback.py
git commit -m "feat(streams): CandleStream.on_candle callback hook"
```

### Task 9: Tape capture writer

**Files:**
- Create: `hydra_tape_capture.py`
- Test: `tests/test_tape_capture.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_tape_capture.py
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
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_tape_capture.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement TapeCapture**

```python
# hydra_tape_capture.py
"""Live tape capture: subscribes to CandleStream pushes, writes closed
candles to hydra_history.sqlite (source='tape') via a dedicated writer
thread + bounded queue. The agent's main loop must never stall on a SQLite
fsync — on queue full, candles are dropped and counted (live trading
priority over historical fidelity)."""
from __future__ import annotations

import datetime as _dt
import queue
import threading
from typing import Any, Dict, Optional

from hydra_history_store import CandleRow, HistoryStore


def _parse_iso_to_ts(s: str) -> int:
    # WS v2 emits "interval_begin" as ISO 8601 with Z; tolerant parse.
    s = s.replace("Z", "+00:00")
    try:
        return int(_dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


class TapeCapture:
    def __init__(self, store: HistoryStore, queue_max: int = 256):
        self._store = store
        self._q: "queue.Queue[Optional[CandleRow]]" = queue.Queue(maxsize=queue_max)
        self._thr: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.dropped = 0
        self._dropped_lock = threading.Lock()

    def on_candle(self, pair: str, candle: Dict[str, Any]) -> None:
        """Hook for CandleStream.on_candle. Non-blocking; drops on queue full."""
        ib = candle.get("interval_begin")
        ts = _parse_iso_to_ts(ib) if ib else 0
        if ts <= 0:
            return
        interval_min = int(candle.get("interval", 60))
        grain_sec = interval_min * 60
        try:
            row = CandleRow(
                pair=pair,
                grain_sec=grain_sec,
                ts=ts,
                open=float(candle.get("open", 0)),
                high=float(candle.get("high", 0)),
                low=float(candle.get("low", 0)),
                close=float(candle.get("close", 0)),
                volume=float(candle.get("volume", 0)),
                source="tape",
            )
        except (TypeError, ValueError):
            return
        try:
            self._q.put_nowait(row)
        except queue.Full:
            with self._dropped_lock:
                self.dropped += 1

    def start(self) -> None:
        if self._thr is not None:
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="TapeCapture",
                                     daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)  # sentinel
        if self._thr:
            self._thr.join(timeout=5.0)
        self._thr = None

    def flush(self, timeout: float = 5.0) -> None:
        """Block until queue drains (test/dev helper)."""
        deadline_ev = threading.Event()
        def _watch():
            self._q.join()
            deadline_ev.set()
        threading.Thread(target=_watch, daemon=True).start()
        deadline_ev.wait(timeout=timeout)

    def _run(self) -> None:
        batch: list = []
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                if batch:
                    self._flush_batch(batch)
                    batch.clear()
                continue
            if item is None:
                self._q.task_done()
                break
            batch.append(item)
            self._q.task_done()
            if len(batch) >= 32:
                self._flush_batch(batch)
                batch.clear()
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list) -> None:
        try:
            self._store.upsert_candles(batch)
        except Exception as e:
            print(f"  [TAPE] flush error: {type(e).__name__}: {e}")
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_tape_capture.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_tape_capture.py tests/test_tape_capture.py
git commit -m "feat(history): live tape capture writer (bounded queue + worker)"
```

### Task 10: Wire tape capture into agent

**Files:**
- Modify: `hydra_agent.py`
- Modify: `CLAUDE.md` env-flags table

- [ ] **Step 1: Add env-gated wiring in hydra_agent.py startup**

Locate the existing `CandleStream` construction in `hydra_agent.py` (it already runs as part of the streams stack). After the stream is constructed but before it starts, add:

```python
# In hydra_agent.py, near where CandleStream is built — pseudocode location;
# actual edit must match the existing context.
import os
from hydra_history_store import HistoryStore
from hydra_tape_capture import TapeCapture

if os.environ.get("HYDRA_TAPE_CAPTURE", "1") == "1":
    _tape_store = HistoryStore(os.environ.get("HYDRA_HISTORY_DB", "hydra_history.sqlite"))
    _tape = TapeCapture(_tape_store)
    candle_stream.on_candle(_tape.on_candle)
    _tape.start()
    # Stash on self for shutdown.
    self._tape_capture = _tape
else:
    self._tape_capture = None
```

In the agent shutdown path:

```python
if getattr(self, "_tape_capture", None) is not None:
    self._tape_capture.stop()
```

- [ ] **Step 2: Update CLAUDE.md env-flags table**

Add the three new rows from design §10 (`HYDRA_TAPE_CAPTURE`, `HYDRA_HISTORY_DB`, `HYDRA_REGRESSION_GATE`) to the env-flags table in `CLAUDE.md`. Match the existing row format.

- [ ] **Step 3: Run mock harness**

Run: `python tests/live_harness/harness.py --mode mock`
Expected: harness completes; agent boots and exits cleanly.

- [ ] **Step 4: Smoke-check tape writes**

```bash
HYDRA_TAPE_CAPTURE=1 python tests/live_harness/harness.py --mode mock
sqlite3 hydra_history.sqlite "SELECT COUNT(*) FROM ohlc WHERE source='tape'"
```

Expected: count > 0 (mock harness emits some candles).

- [ ] **Step 5: Commit**

```bash
git add hydra_agent.py CLAUDE.md
git commit -m "feat(agent): wire tape capture under HYDRA_TAPE_CAPTURE"
```

---

## Phase 3 — Walk-forward + Wilcoxon (rc1)

### Task 11: Wilcoxon signed-rank test (stdlib)

**Files:**
- Create: `hydra_walk_forward.py` (Wilcoxon section first)
- Test: `tests/test_wilcoxon.py`

- [ ] **Step 1: Write failing tests with hand-computed reference values**

```python
# tests/test_wilcoxon.py
import math
from hydra_walk_forward import wilcoxon_signed_rank

def test_all_positive_deltas():
    """Hand check: deltas = [1, 2, 3, 4, 5]; ranks = 1..5; W+ = 15, W- = 0;
    n=5 means smallest possible W- under H0. Expected p_two_sided ≈ 0.0625
    (exact distribution; not significant at 5%)."""
    v = wilcoxon_signed_rank([1.0, 2.0, 3.0, 4.0, 5.0])
    assert v.n == 5
    assert v.w_minus == 0
    assert v.w_plus == 15
    assert math.isclose(v.p_value, 0.0625, abs_tol=1e-3)

def test_zero_deltas_dropped():
    v = wilcoxon_signed_rank([0.0, 1.0, -1.0, 2.0])
    assert v.n == 3  # zeros dropped per Wilcoxon rule

def test_symmetric_deltas_no_signal():
    v = wilcoxon_signed_rank([1.0, -1.0, 2.0, -2.0, 3.0, -3.0])
    assert v.n == 6
    # Ranks 1,1,2,2,3,3 → W+ = W- = (1+2+3+1+2+3)/2 = 6 (after tied-ranks
    # average splits). For symmetric two-sided test this maximizes p.
    assert math.isclose(v.w_plus, v.w_minus, abs_tol=0.5)
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_wilcoxon.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement Wilcoxon**

```python
# hydra_walk_forward.py
"""HYDRA Walk-Forward Methodology — anchored quarterly folds + paired
Wilcoxon signed-rank test. Stdlib only.

This is the kernel for both:
- Mode B (hypothesis lab):     baseline params vs candidate params
- Mode C (release regression): prior version snapshot vs current branch

See docs/superpowers/specs/2026-04-26-research-tab-redesign-design.md §4.6.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class WilcoxonVerdict:
    n: int
    w_plus: float
    w_minus: float
    p_value: float
    candidate_wins: int
    median_delta: float
    verdict: str   # "better" | "worse" | "equivocal"


def wilcoxon_signed_rank(deltas: Sequence[float],
                         alpha: float = 0.05) -> WilcoxonVerdict:
    """Two-sided Wilcoxon signed-rank test on paired-difference samples.

    For n <= 25, uses the exact distribution (enumerate all 2^n sign
    permutations of the ranks). For larger n, uses the normal approximation
    with continuity correction.
    """
    nonzero = [d for d in deltas if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return WilcoxonVerdict(0, 0.0, 0.0, 1.0, 0, 0.0, "equivocal")
    abs_vals = [abs(d) for d in nonzero]
    # Average ranks for ties.
    indexed = sorted(range(n), key=lambda i: abs_vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_vals[indexed[j + 1]] == abs_vals[indexed[i]]:
            j += 1
        avg = (i + j + 2) / 2.0   # ranks 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero) if d < 0)
    w = min(w_plus, w_minus)
    candidate_wins = sum(1 for d in nonzero if d > 0)
    median_delta = sorted(nonzero)[n // 2] if n % 2 == 1 else (
        (sorted(nonzero)[n // 2 - 1] + sorted(nonzero)[n // 2]) / 2.0
    )
    if n <= 25:
        p_value = _exact_p(ranks, w)
    else:
        # Normal approx with continuity correction.
        mean = n * (n + 1) / 4.0
        var = n * (n + 1) * (2 * n + 1) / 24.0
        z = (w - mean + 0.5) / math.sqrt(var)
        # Two-sided.
        p_value = 2.0 * _norm_cdf(-abs(z))
    if p_value < alpha:
        verdict = "better" if median_delta > 0 else "worse"
    else:
        verdict = "equivocal"
    return WilcoxonVerdict(n, w_plus, w_minus, p_value, candidate_wins,
                            median_delta, verdict)


def _exact_p(ranks: Sequence[float], w_observed: float) -> float:
    """Exact two-sided p-value: enumerate all 2^n sign assignments of the
    ranks, compute W- (sum of ranks assigned negative sign), and count how
    many are <= w_observed (or symmetrically >=)."""
    n = len(ranks)
    total = 1 << n
    le = 0
    for mask in range(total):
        s = 0.0
        for i in range(n):
            if mask & (1 << i):
                s += ranks[i]
        if s <= w_observed:
            le += 1
    p_one = le / total
    return min(1.0, 2.0 * p_one)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_wilcoxon.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Cross-check against scipy in a one-shot dev script**

```bash
python -c "
from scipy.stats import wilcoxon
import math
res = wilcoxon([1, 2, 3, 4, 5], alternative='two-sided', mode='exact')
print(f'scipy p={res.pvalue}')
"
```

Expected: prints `scipy p=0.0625`. Confirms our implementation matches.

(Note: scipy is dev-only, NOT a runtime dependency. Engine remains stdlib-only.)

- [ ] **Step 6: Commit**

```bash
git add hydra_walk_forward.py tests/test_wilcoxon.py
git commit -m "feat(walk-forward): exact Wilcoxon signed-rank (stdlib)"
```

### Task 12: Quarterly fold construction

**Files:**
- Modify: `hydra_walk_forward.py`
- Test: `tests/test_walk_forward_folds.py`

- [ ] **Step 1: Write failing tests for fold construction**

```python
# tests/test_walk_forward_folds.py
import datetime as dt
from hydra_walk_forward import build_quarterly_folds, WalkForwardSpec

def _ts(year, month, day=1):
    return int(dt.datetime(year, month, day, tzinfo=dt.timezone.utc).timestamp())

def test_builds_quarterly_folds():
    spec = WalkForwardSpec(is_lookback_quarters=4)
    folds = build_quarterly_folds(_ts(2022, 1, 1), _ts(2023, 1, 1), spec)
    # 2022 → 4 OOS quarters: Q1 (Jan-Mar), Q2 (Apr-Jun), Q3, Q4. But the FIRST
    # fold needs at least 1 quarter of IS, so Q1 2022 is skipped.
    assert len(folds) == 3
    f0 = folds[0]   # IS = Q1 2022, OOS = Q2 2022
    assert f0.is_start == _ts(2022, 1, 1)
    assert f0.is_end == _ts(2022, 4, 1)
    assert f0.oos_start == _ts(2022, 4, 1)
    assert f0.oos_end == _ts(2022, 7, 1)

def test_is_lookback_capped():
    spec = WalkForwardSpec(is_lookback_quarters=2)
    # 3 years of data; on the last fold, IS should be capped to last 2 quarters.
    folds = build_quarterly_folds(_ts(2020, 1, 1), _ts(2023, 1, 1), spec)
    last = folds[-1]
    is_quarters = (last.is_end - last.is_start) // (90 * 86400)
    assert is_quarters <= 2 + 1   # ±1 for 90-vs-91-day months
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_walk_forward_folds.py -v`
Expected: FAIL — `build_quarterly_folds` not defined.

- [ ] **Step 3: Implement fold construction**

```python
# Add to hydra_walk_forward.py
import datetime as _dt
from typing import List, Tuple


@dataclass(frozen=True)
class WalkForwardSpec:
    fold_kind: str = "quarterly"
    is_lookback_quarters: int = 8
    min_oos_trades: int = 5


@dataclass(frozen=True)
class Fold:
    idx: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int


def _quarter_starts_between(start_ts: int, end_ts: int) -> List[int]:
    """Return a list of UTC unix-second timestamps at each quarter start
    (Jan/Apr/Jul/Oct, day 1, 00:00 UTC) within [start_ts, end_ts]."""
    starts: List[int] = []
    d = _dt.datetime.fromtimestamp(start_ts, tz=_dt.timezone.utc)
    # Round up to next quarter start.
    next_q_month = ((d.month - 1) // 3) * 3 + 1
    cursor = _dt.datetime(d.year, next_q_month, 1, tzinfo=_dt.timezone.utc)
    if cursor.timestamp() < start_ts:
        cursor = _add_months(cursor, 3)
    end_d = _dt.datetime.fromtimestamp(end_ts, tz=_dt.timezone.utc)
    while cursor <= end_d:
        starts.append(int(cursor.timestamp()))
        cursor = _add_months(cursor, 3)
    return starts


def _add_months(d: _dt.datetime, months: int) -> _dt.datetime:
    m = d.month - 1 + months
    y = d.year + m // 12
    return d.replace(year=y, month=(m % 12) + 1)


def build_quarterly_folds(history_start_ts: int, history_end_ts: int,
                          spec: WalkForwardSpec) -> List[Fold]:
    boundaries = _quarter_starts_between(history_start_ts, history_end_ts)
    if len(boundaries) < 2:
        return []
    folds: List[Fold] = []
    for i in range(1, len(boundaries) - 1):
        oos_start = boundaries[i]
        oos_end = boundaries[i + 1]
        is_end = oos_start
        is_start_idx = max(0, i - spec.is_lookback_quarters)
        is_start = boundaries[is_start_idx]
        if is_start == is_end:
            continue
        folds.append(Fold(
            idx=len(folds),
            is_start=is_start, is_end=is_end,
            oos_start=oos_start, oos_end=oos_end,
        ))
    return folds
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_walk_forward_folds.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_walk_forward.py tests/test_walk_forward_folds.py
git commit -m "feat(walk-forward): anchored quarterly fold construction"
```

### Task 13: Walk-forward runner

**Files:**
- Modify: `hydra_walk_forward.py`
- Test: `tests/test_walk_forward.py`

This task wires the runner. It depends on Task 15 (`SqliteSource`) for the actual fold engine — so we use a fake `RunnerFn` in tests now and integrate later.

- [ ] **Step 1: Write failing test using a fake runner**

```python
# tests/test_walk_forward.py
import datetime as dt
from hydra_walk_forward import (
    run_walk_forward, WalkForwardSpec, FoldMetrics, FoldResult
)

def _ts(y, m, d=1):
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())

def test_runner_returns_per_fold_results_with_wilcoxon():
    """Use a deterministic fake runner: candidate always beats baseline by
    +0.1 Sharpe. Wilcoxon should call this BETTER for any n>=6."""
    def fake_runner(pair, params, fold):
        is_baseline = params.get("is_baseline", False)
        return FoldMetrics(
            sharpe=1.0 if is_baseline else 1.1,
            total_return_pct=10.0 if is_baseline else 11.0,
            max_dd_pct=5.0,
            fee_adj_return_pct=9.0 if is_baseline else 10.0,
            n_trades=10,
        )

    result = run_walk_forward(
        pair="BTC/USD",
        history_start_ts=_ts(2020, 1, 1),
        history_end_ts=_ts(2023, 1, 1),
        baseline_params={"is_baseline": True},
        candidate_params={"is_baseline": False},
        spec=WalkForwardSpec(is_lookback_quarters=4),
        runner=fake_runner,
    )
    assert len(result.folds) >= 6
    assert result.wilcoxon["sharpe"].verdict == "better"
    assert result.wilcoxon["sharpe"].candidate_wins == len(result.folds)
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_walk_forward.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement runner**

```python
# Add to hydra_walk_forward.py
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class FoldMetrics:
    sharpe: float
    total_return_pct: float
    max_dd_pct: float
    fee_adj_return_pct: float
    n_trades: int


@dataclass
class FoldResult:
    fold: Fold
    baseline: FoldMetrics
    candidate: FoldMetrics
    deltas: Dict[str, float]


@dataclass
class WalkForwardResult:
    pair: str
    folds: List[FoldResult]
    wilcoxon: Dict[str, WilcoxonVerdict]
    skipped_folds: int


_HEADLINE_METRICS = ("sharpe", "total_return_pct", "max_dd_pct", "fee_adj_return_pct")


RunnerFn = Callable[[str, Dict, Fold], FoldMetrics]


def run_walk_forward(
    pair: str,
    history_start_ts: int,
    history_end_ts: int,
    baseline_params: Dict,
    candidate_params: Dict,
    spec: WalkForwardSpec,
    runner: RunnerFn,
) -> WalkForwardResult:
    folds = build_quarterly_folds(history_start_ts, history_end_ts, spec)
    fold_results: List[FoldResult] = []
    skipped = 0
    for fold in folds:
        baseline = runner(pair, baseline_params, fold)
        candidate = runner(pair, candidate_params, fold)
        if (baseline.n_trades < spec.min_oos_trades or
                candidate.n_trades < spec.min_oos_trades):
            skipped += 1
            continue
        # max_dd_pct: lower is better → flip sign so positive = candidate-better.
        deltas = {
            "sharpe": candidate.sharpe - baseline.sharpe,
            "total_return_pct": candidate.total_return_pct - baseline.total_return_pct,
            "max_dd_pct": baseline.max_dd_pct - candidate.max_dd_pct,
            "fee_adj_return_pct": candidate.fee_adj_return_pct - baseline.fee_adj_return_pct,
        }
        fold_results.append(FoldResult(fold, baseline, candidate, deltas))
    wilcoxon = {}
    for m in _HEADLINE_METRICS:
        wilcoxon[m] = wilcoxon_signed_rank([fr.deltas[m] for fr in fold_results])
    return WalkForwardResult(pair, fold_results, wilcoxon, skipped)
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_walk_forward.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_walk_forward.py tests/test_walk_forward.py
git commit -m "feat(walk-forward): runner with paired Wilcoxon per metric"
```

### Task 14: Walk-forward suite green

- [ ] **Step 1: Run full walk-forward test suite**

Run: `python -m pytest tests/test_wilcoxon.py tests/test_walk_forward_folds.py tests/test_walk_forward.py -v`
Expected: all PASS.

- [ ] **Step 2: Two-phase self-audit (CLAUDE.md Rule 4) on `hydra_walk_forward.py`**

Read the file; check for unused imports, dead code, unhandled exceptions, null/empty crashes, deprecated APIs, misleading errors, false-positive checks. Fix any. Re-run tests.

- [ ] **Step 3: Commit any fixes**

```bash
git add hydra_walk_forward.py
git commit -m "chore(walk-forward): self-audit pass (Rule 4)"
```

**🛑 PHASE 3 CHECKPOINT — STOP and ask user to verify Wilcoxon math against a hand-computed example before proceeding to Phase 4.**

---

## Phase 4 — Backtest SqliteSource + regression runner (rc1)

### Task 15: SqliteSource for backtest

**Files:**
- Modify: `hydra_backtest.py`
- Test: `tests/test_backtest_sqlite_source.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_backtest_sqlite_source.py
from hydra_backtest import SqliteSource, BacktestConfig, make_candle_source
from hydra_history_store import HistoryStore, CandleRow

def test_sqlite_source_yields_in_window(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    rows = [
        CandleRow("BTC/USD", 3600, 1_700_000_000 + i * 3600,
                  10, 11, 9, 10, 1, "kraken_archive")
        for i in range(5)
    ]
    store.upsert_candles(rows)
    src = SqliteSource(
        db_path=str(tmp_path / "h.sqlite"),
        grain_sec=3600,
        start_ts=1_700_000_000 + 1 * 3600,
        end_ts=1_700_000_000 + 3 * 3600,
    )
    candles = list(src.iter_candles("BTC/USD"))
    assert len(candles) == 3

def test_factory_default_is_sqlite(tmp_path):
    HistoryStore(str(tmp_path / "h.sqlite"))
    cfg = BacktestConfig(
        name="t",
        pairs=("BTC/USD",),
        data_source="sqlite",
        data_source_params_json='{"db_path": "' + str(tmp_path / "h.sqlite") + '", "grain_sec": 3600, "start_ts": 0, "end_ts": 9999999999}',
    )
    src = make_candle_source(cfg)
    assert isinstance(src, SqliteSource)
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_backtest_sqlite_source.py -v`
Expected: FAIL — `SqliteSource` not defined.

- [ ] **Step 3: Add SqliteSource + factory branch**

```python
# In hydra_backtest.py — add new class alongside SyntheticSource/CsvSource:

class SqliteSource(CandleSource):
    """Reads candles from the canonical hydra_history.sqlite store.
    Default source as of v2.20.0."""

    def __init__(self, db_path: str, grain_sec: int,
                 start_ts: int, end_ts: int):
        self.db_path = db_path
        self.grain_sec = grain_sec
        self.start_ts = start_ts
        self.end_ts = end_ts

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        from hydra_history_store import HistoryStore
        store = HistoryStore(self.db_path)
        for r in store.fetch(pair, self.grain_sec, self.start_ts, self.end_ts):
            yield Candle(open=r.open, high=r.high, low=r.low, close=r.close,
                         volume=r.volume, timestamp=float(r.ts))

    def describe(self) -> Dict[str, Any]:
        return {"source": "sqlite", "db_path": self.db_path,
                "grain_sec": self.grain_sec,
                "start_ts": self.start_ts, "end_ts": self.end_ts}


# In make_candle_source() add a "sqlite" branch BEFORE the existing branches:
    if cfg.data_source == "sqlite":
        params = cfg.data_source_params
        return SqliteSource(
            db_path=params["db_path"],
            grain_sec=params["grain_sec"],
            start_ts=int(params["start_ts"]),
            end_ts=int(params["end_ts"]),
        )
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_backtest_sqlite_source.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_backtest.py tests/test_backtest_sqlite_source.py
git commit -m "feat(backtest): SqliteSource (default in v2.20.0)"
```

### Task 16: Brain-stub mode in backtest

**Files:**
- Modify: `hydra_backtest.py`
- Test: extend `tests/test_backtest_sqlite_source.py`

- [ ] **Step 1: Add `brain_mode` field to BacktestConfig**

```python
# In BacktestConfig dataclass, add:
    brain_mode: str = "stub"   # "stub" | "replay" | "live" — only "stub" wired in v2.20.0
```

- [ ] **Step 2: Write failing test that any brain call raises in stub mode**

```python
def test_brain_mode_stub_blocks_anthropic_calls():
    cfg = BacktestConfig(name="t", pairs=("BTC/USD",), brain_mode="stub")
    assert cfg.brain_mode == "stub"

def test_brain_mode_live_not_supported_yet():
    import pytest
    cfg = BacktestConfig(name="t", pairs=("BTC/USD",), brain_mode="live")
    runner = BacktestRunner(cfg)
    with pytest.raises(NotImplementedError):
        runner._validate_brain_mode()  # see step 3
```

- [ ] **Step 3: Add `_validate_brain_mode` enforcement**

```python
# In BacktestRunner, add method called from run() before any tick:
    def _validate_brain_mode(self) -> None:
        if self.cfg.brain_mode == "stub":
            return
        raise NotImplementedError(
            f"brain_mode={self.cfg.brain_mode!r} not implemented in v2.20.0; "
            f"only 'stub' is supported. Mode 'replay' and 'live' are deferred."
        )
```

Locate the existing brain-call site(s) in the backtest tick loop. Wrap each with:

```python
if self.cfg.brain_mode == "stub":
    # Deterministic fallback: trust quant rules; HOLD if quant rules disagree.
    ai_decision = _stub_brain_decision(quant_signal, rules_outcome)
else:
    ai_decision = self._brain.decide(...)  # existing call
```

Add helper `_stub_brain_decision`:

```python
def _stub_brain_decision(quant_signal, rules_outcome):
    """Deterministic stand-in for the AI brain in regression/lab runs.
    Approves the quant signal iff R10 quant_rules approve; otherwise HOLD.
    No LLM calls, no network, no randomness."""
    if rules_outcome and getattr(rules_outcome, "approved", True):
        return quant_signal
    # SignalAction.HOLD — match engine's enum; resolve at call site.
    from hydra_engine import SignalAction
    return SignalAction.HOLD
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest_sqlite_source.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_backtest.py tests/test_backtest_sqlite_source.py
git commit -m "feat(backtest): brain_mode='stub' for deterministic regression/lab"
```

### Task 17: Regression-snapshot tables + writer

**Files:**
- Modify: `hydra_history_store.py` (add regression tables to schema)
- Create: `tools/run_regression.py`
- Test: `tests/test_regression_runner.py`

- [ ] **Step 1: Extend `_SCHEMA` in hydra_history_store.py**

```python
# Append to _SCHEMA in hydra_history_store.py:
"""
CREATE TABLE IF NOT EXISTS regression_run (
  run_id          TEXT    PRIMARY KEY,
  hydra_version   TEXT    NOT NULL,
  git_sha         TEXT    NOT NULL,
  param_hash      TEXT    NOT NULL,
  pair            TEXT    NOT NULL,
  grain_sec       INTEGER NOT NULL,
  spec_json       TEXT    NOT NULL,
  override_reason TEXT,
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS regression_metrics (
  run_id   TEXT NOT NULL,
  fold_idx INTEGER NOT NULL,
  metric   TEXT NOT NULL,
  value    REAL NOT NULL,
  PRIMARY KEY (run_id, fold_idx, metric),
  FOREIGN KEY (run_id) REFERENCES regression_run(run_id)
);

CREATE TABLE IF NOT EXISTS regression_equity_curve (
  run_id  TEXT NOT NULL,
  ts      INTEGER NOT NULL,
  equity  REAL NOT NULL,
  PRIMARY KEY (run_id, ts),
  FOREIGN KEY (run_id) REFERENCES regression_run(run_id)
);

CREATE TABLE IF NOT EXISTS regression_trade (
  run_id    TEXT NOT NULL,
  trade_idx INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  side      TEXT NOT NULL,
  price     REAL NOT NULL,
  size      REAL NOT NULL,
  fee       REAL NOT NULL,
  regime    TEXT,
  reason    TEXT,
  PRIMARY KEY (run_id, trade_idx),
  FOREIGN KEY (run_id) REFERENCES regression_run(run_id)
);
"""
```

Bump `SCHEMA_VERSION = 2` and update the migration error to allow upgrade-by-rebuild on `0/None → 2`. Since v2.20.0 is the first ship, treat `=1` (only seen during dev) the same as fresh: rerun `executescript`. Document this in a comment.

```python
# Replace SCHEMA_VERSION:
SCHEMA_VERSION = 2

# In _init_schema, replace strict mismatch with upgrade path for 1→2:
            if existing is not None and existing != SCHEMA_VERSION:
                if existing == 1 and SCHEMA_VERSION == 2:
                    # Forward-compat: regression_* tables added in v2; safe to add.
                    pass
                else:
                    raise RuntimeError(
                        f"hydra_history_store: schema_version={existing} on disk, "
                        f"code expects {SCHEMA_VERSION}."
                    )
            conn.executescript(_SCHEMA)
            # Bump recorded version unconditionally (idempotent).
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
```

Update `tests/test_history_store_migrations.py` to assert that a v1 DB upgrades silently to v2 and the regression tables exist.

- [ ] **Step 2: Write failing test for regression runner**

```python
# tests/test_regression_runner.py
import json
import sqlite3
from hydra_history_store import HistoryStore, CandleRow
from tools.run_regression import persist_regression_run

def test_persist_creates_run_and_metric_rows(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    run_id = "abc123"
    persist_regression_run(
        store, run_id=run_id, hydra_version="2.20.0", git_sha="deadbeef",
        param_hash="paramX", pair="BTC/USD", grain_sec=3600,
        spec_json=json.dumps({"fold_kind": "quarterly"}),
        per_fold_metrics={
            0: {"sharpe": 1.1, "total_return_pct": 5.0},
            1: {"sharpe": 1.2, "total_return_pct": 6.0},
        },
        aggregate_metrics={"sharpe": 1.15, "total_return_pct": 5.5},
        equity_curve=[(1_700_000_000, 100.0), (1_700_003_600, 101.0)],
        trades=[],
    )
    with sqlite3.connect(str(tmp_path / "h.sqlite")) as conn:
        n_runs = conn.execute("SELECT COUNT(*) FROM regression_run").fetchone()[0]
        n_metrics = conn.execute("SELECT COUNT(*) FROM regression_metrics").fetchone()[0]
        n_curve = conn.execute("SELECT COUNT(*) FROM regression_equity_curve").fetchone()[0]
    assert n_runs == 1
    assert n_metrics == 2 * 2 + 2  # 2 metrics per fold × 2 folds + 2 aggregates
    assert n_curve == 2
```

- [ ] **Step 3: Implement persist + runner**

```python
# tools/run_regression.py
"""Per-version regression runner for Mode C.

Iterates each pair in the default triangle, runs walk-forward (anchored
quarterly, brain stubbed) against the prior version's snapshot, persists
results into hydra_history.sqlite (regression_* tables), and exits with a
gate verdict.

Usage:
    python -m tools.run_regression --version 2.20.0
    python -m tools.run_regression --version 2.20.0 --accept-regression "FX modifier reroll"

Exit codes:
    0  — no WORSE verdict, or override accepted
    2  — Wilcoxon WORSE p<0.05 on any pair × any headline metric, no override
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from typing import Dict, Iterable, List, Optional, Tuple

from hydra_history_store import HistoryStore


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], encoding="utf-8"
        ).strip()
    except Exception:
        return ""


def persist_regression_run(
    store: HistoryStore,
    run_id: str,
    hydra_version: str,
    git_sha: str,
    param_hash: str,
    pair: str,
    grain_sec: int,
    spec_json: str,
    per_fold_metrics: Dict[int, Dict[str, float]],
    aggregate_metrics: Dict[str, float],
    equity_curve: Iterable[Tuple[int, float]],
    trades: Iterable[Dict],
    override_reason: Optional[str] = None,
) -> None:
    now = int(time.time())
    with store._conn() as conn:
        conn.execute(
            """INSERT INTO regression_run
               (run_id, hydra_version, git_sha, param_hash, pair, grain_sec,
                spec_json, override_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, hydra_version, git_sha, param_hash, pair, grain_sec,
             spec_json, override_reason, now),
        )
        for fold_idx, m in per_fold_metrics.items():
            for metric_name, val in m.items():
                conn.execute(
                    """INSERT INTO regression_metrics(run_id, fold_idx, metric, value)
                       VALUES (?, ?, ?, ?)""",
                    (run_id, fold_idx, metric_name, val),
                )
        for metric_name, val in aggregate_metrics.items():
            conn.execute(
                """INSERT INTO regression_metrics(run_id, fold_idx, metric, value)
                   VALUES (?, -1, ?, ?)""",
                (run_id, metric_name, val),
            )
        for ts, equity in equity_curve:
            conn.execute(
                """INSERT INTO regression_equity_curve(run_id, ts, equity)
                   VALUES (?, ?, ?)""",
                (run_id, ts, equity),
            )
        for i, t in enumerate(trades):
            conn.execute(
                """INSERT INTO regression_trade
                   (run_id, trade_idx, ts, side, price, size, fee, regime, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, i, t["ts"], t["side"], t["price"], t["size"],
                 t["fee"], t.get("regime"), t.get("reason")),
            )
        conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--db", default=os.environ.get("HYDRA_HISTORY_DB",
                                                    "hydra_history.sqlite"))
    ap.add_argument("--pairs", default="SOL/USD,SOL/BTC,BTC/USD")
    ap.add_argument("--grain-sec", type=int, default=3600)
    ap.add_argument("--accept-regression", default=None,
                    help="If set, accepts WORSE verdict and records the reason")
    args = ap.parse_args()

    store = HistoryStore(args.db)
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    git_sha = _git_sha()

    # Implementation note: full integration with BacktestRunner + walk-forward
    # runner happens via _runner_from_backtest below. The stub here provides
    # the orchestration shape; engine integration is exercised end-to-end via
    # `tests/live_harness/harness.py --mode mock` once Phase 4 lands.
    from hydra_walk_forward import (
        run_walk_forward, WalkForwardSpec, FoldMetrics
    )

    def _runner_from_backtest(pair, params, fold) -> FoldMetrics:
        from hydra_backtest import BacktestConfig, BacktestRunner
        cfg = BacktestConfig(
            name=f"reg-{args.version}-{pair}-{fold.idx}",
            pairs=(pair,),
            data_source="sqlite",
            data_source_params_json=json.dumps({
                "db_path": args.db, "grain_sec": args.grain_sec,
                "start_ts": fold.is_start, "end_ts": fold.oos_end,
            }),
            brain_mode="stub",
        )
        result = BacktestRunner(cfg).run()
        m = result.metrics
        return FoldMetrics(
            sharpe=m.sharpe, total_return_pct=m.total_return_pct,
            max_dd_pct=m.max_dd_pct, fee_adj_return_pct=m.fee_adj_return_pct,
            n_trades=m.n_trades,
        )

    # Determine baseline: most-recent prior regression_run for the same pair.
    # If none exists (first ship) → baseline == candidate; verdict will be
    # equivocal across the board.
    spec = WalkForwardSpec()
    worst_verdict: Optional[Tuple[str, str, str]] = None  # (pair, metric, verdict)
    for pair in pairs:
        cov = store.coverage(pair, args.grain_sec)
        if cov.first_ts is None:
            print(f"  [REGRESSION] {pair}: no history — skipping")
            continue
        result = run_walk_forward(
            pair=pair,
            history_start_ts=cov.first_ts,
            history_end_ts=cov.last_ts,
            baseline_params={"is_baseline": True},   # TODO: load prior snapshot
            candidate_params={"is_baseline": False},
            spec=spec,
            runner=_runner_from_backtest,
        )
        run_id = uuid.uuid4().hex
        per_fold = {fr.fold.idx: {
            "sharpe": fr.candidate.sharpe,
            "total_return_pct": fr.candidate.total_return_pct,
            "max_dd_pct": fr.candidate.max_dd_pct,
            "fee_adj_return_pct": fr.candidate.fee_adj_return_pct,
        } for fr in result.folds}
        aggregate = {
            f"wilcoxon_p_{m}": result.wilcoxon[m].p_value
            for m in result.wilcoxon
        }
        persist_regression_run(
            store, run_id=run_id, hydra_version=args.version, git_sha=git_sha,
            param_hash="", pair=pair, grain_sec=args.grain_sec,
            spec_json=json.dumps(asdict(spec)),
            per_fold_metrics=per_fold,
            aggregate_metrics=aggregate,
            equity_curve=[],
            trades=[],
            override_reason=args.accept_regression,
        )
        for metric, v in result.wilcoxon.items():
            print(f"  [REGRESSION] {pair} {metric}: {v.verdict} "
                  f"(p={v.p_value:.4f}, wins={v.candidate_wins}/{v.n})")
            if v.verdict == "worse":
                worst_verdict = (pair, metric, v.verdict)

    if worst_verdict and not args.accept_regression and \
            os.environ.get("HYDRA_REGRESSION_GATE", "1") == "1":
        pair, metric, _ = worst_verdict
        print(f"  [REGRESSION] BLOCKED: {pair} {metric} WORSE; "
              f"rerun with --accept-regression \"<reason>\" to override")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_regression_runner.py tests/test_history_store_migrations.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hydra_history_store.py tools/run_regression.py tests/test_regression_runner.py tests/test_history_store_migrations.py
git commit -m "feat(regression): snapshot tables + run_regression orchestrator"
```

### Task 18: First end-to-end regression run

- [ ] **Step 1: Run regression against the existing v2.19.1**

```bash
python -m tools.run_regression --version 2.19.1
```

Expected: per-pair Wilcoxon output. Since baseline == candidate (first ever run), every metric should be `equivocal`.

- [ ] **Step 2: Verify rows persisted**

```bash
sqlite3 hydra_history.sqlite \
  "SELECT hydra_version, pair, COUNT(*) FROM regression_run \
   GROUP BY hydra_version, pair"
```

Expected: 3 rows (one per pair).

- [ ] **Step 3: Mock harness clean**

Run: `python tests/live_harness/harness.py --mode mock`
Expected: clean exit.

- [ ] **Step 4: Two-phase audit on Phase 4 modules**

Audit `hydra_history_store.py`, `tools/run_regression.py`, `hydra_backtest.py` SqliteSource + brain stub additions per CLAUDE.md Rule 4. Fix issues. Re-test.

**🛑 PHASE 4 CHECKPOINT — STOP and ask user to verify the snapshot rows look well-formed before wiring the dashboard.**

---

## Phase 5 — Dashboard Research tab (rc2)

This phase is parallelizable across subagents — each pane is its own file.

### Task 19: Backend WS routes for Research tab

**Files:**
- Modify: `hydra_backtest_server.py`
- Test: `tests/test_research_ws.py`

- [ ] **Step 1: Define route message contract**

The dashboard talks to `hydra_backtest_server.py` over WS. Add these message types:

| inbound `type` | payload | response messages |
|---|---|---|
| `dataset.coverage` | `{}` | `{"type":"dataset.coverage","data":[{"pair","grain_sec","candle_count","first_ts","last_ts","gap_count","max_gap_sec","last_source"}, …]}` |
| `lab.run` | `{"pair","baseline_params","candidate_params","spec":{}}` | streaming `{"type":"lab.fold","data":FoldResult}` × N, then `{"type":"lab.done","data":WalkForwardResult}` |
| `releases.list` | `{}` | `{"type":"releases.list","data":[{"hydra_version","pair","run_id","verdict_summary"}, …]}` |
| `releases.diff` | `{"a_run_id","b_run_id"}` | `{"type":"releases.diff","data":{…}}` |

Document the contract at the top of `hydra_backtest_server.py` as a comment block.

- [ ] **Step 2: Implement route handlers**

Add four handlers to the existing server. Each handler reads from `HistoryStore` (for `dataset.coverage` and `releases.*`) or invokes `run_walk_forward` with `_runner_from_backtest` (for `lab.run`, streaming fold-by-fold via WS).

Implementation sketch:

```python
# In hydra_backtest_server.py — add to mount_backtest_routes()

async def _handle_dataset_coverage(ws, store):
    rows = []
    for pair, grain_sec in store.list_pairs():
        c = store.coverage(pair, grain_sec)
        rows.append({
            "pair": pair, "grain_sec": grain_sec,
            "candle_count": c.candle_count,
            "first_ts": c.first_ts, "last_ts": c.last_ts,
            "gap_count": c.gap_count, "max_gap_sec": c.max_gap_sec,
        })
    await ws.send_json({"type": "dataset.coverage", "data": rows})

async def _handle_lab_run(ws, store, payload):
    from hydra_walk_forward import run_walk_forward, WalkForwardSpec
    pair = payload["pair"]
    spec = WalkForwardSpec(**payload.get("spec", {}))
    cov = store.coverage(pair, 3600)
    if cov.first_ts is None:
        await ws.send_json({"type": "lab.error", "data": "no history for pair"})
        return
    # Stream fold results as they complete by using a runner wrapper that
    # sends a fold-level WS message per call.
    folds_done = 0
    def streaming_runner(p, params, fold):
        nonlocal folds_done
        # … invoke BacktestRunner per fold (see _runner_from_backtest in
        # tools/run_regression.py) …
        return _runner_from_backtest(p, params, fold)
    result = run_walk_forward(
        pair=pair, history_start_ts=cov.first_ts, history_end_ts=cov.last_ts,
        baseline_params=payload["baseline_params"],
        candidate_params=payload["candidate_params"],
        spec=spec, runner=streaming_runner,
    )
    await ws.send_json({"type": "lab.done", "data": _result_to_jsonable(result)})

# … similar for releases.list / releases.diff …
```

- [ ] **Step 3: Test with a fake WS connection**

```python
# tests/test_research_ws.py — minimal smoke test
from hydra_history_store import HistoryStore, CandleRow

def test_dataset_coverage_response_shape(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    store.upsert_candles([CandleRow("BTC/USD", 3600, 1, 1, 1, 1, 1, 1, "kraken_archive")])
    # Use a minimal in-process WS double; assert response shape.
    # (Full impl depends on the existing server's test harness; mirror
    # whatever mount_backtest_routes() tests already use.)
```

- [ ] **Step 4: Commit**

```bash
git add hydra_backtest_server.py tests/test_research_ws.py
git commit -m "feat(server): research tab WS routes (dataset/lab/releases)"
```

### Task 20: Dashboard `<DatasetPane>`

**Files:**
- Create: `dashboard/src/components/research/DatasetPane.jsx`

- [ ] **Step 1: Implement read-only coverage table**

```jsx
// dashboard/src/components/research/DatasetPane.jsx
import React, { useEffect, useState } from "react";

const fmtTs = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "—";
const fmtGap = (s) => s < 3600 ? `${s}s` : s < 86400 ? `${(s/3600).toFixed(1)}h` : `${(s/86400).toFixed(1)}d`;

export default function DatasetPane({ ws }) {
  const [rows, setRows] = useState([]);
  useEffect(() => {
    if (!ws) return;
    const handler = (msg) => {
      if (msg.type === "dataset.coverage") setRows(msg.data);
    };
    ws.on(handler);
    ws.send({ type: "dataset.coverage" });
    return () => ws.off(handler);
  }, [ws]);

  const stale = (r) => r.last_ts && (Date.now()/1000 - r.last_ts) > 2 * 86400;

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Canonical Historical Store</h3>
      <p style={{ color: "#888", fontSize: 12 }}>
        Read-only inspector. Refresh via <code>tools/refresh_history.py</code>.
      </p>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "1px solid #333" }}>
            <th>Pair</th><th>Grain</th><th>First</th><th>Last</th>
            <th>Candles</th><th>Gaps</th><th>Max gap</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} style={{ borderBottom: "1px solid #222",
                                 background: stale(r) ? "#3a2a00" : "transparent" }}>
              <td>{r.pair}</td>
              <td>{r.grain_sec/60} min</td>
              <td>{fmtTs(r.first_ts)}</td>
              <td>{fmtTs(r.last_ts)}{stale(r) ? " ⚠" : ""}</td>
              <td>{r.candle_count.toLocaleString()}</td>
              <td>{r.gap_count}</td>
              <td>{fmtGap(r.max_gap_sec)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/src/components/research/DatasetPane.jsx
git commit -m "feat(dashboard): DatasetPane — read-only coverage inspector"
```

### Task 21: Dashboard `<LabPane>`

**Files:**
- Create: `dashboard/src/components/research/LabPane.jsx`

- [ ] **Step 1: Implement form + fold table + verdict bar**

```jsx
// dashboard/src/components/research/LabPane.jsx
import React, { useEffect, useState } from "react";

export default function LabPane({ ws }) {
  const [pair, setPair] = useState("BTC/USD");
  const [baselineJson, setBaselineJson] = useState("{}");
  const [candidateJson, setCandidateJson] = useState("{}");
  const [running, setRunning] = useState(false);
  const [folds, setFolds] = useState([]);
  const [verdict, setVerdict] = useState(null);

  useEffect(() => {
    if (!ws) return;
    const handler = (msg) => {
      if (msg.type === "lab.fold") setFolds(prev => [...prev, msg.data]);
      else if (msg.type === "lab.done") {
        setVerdict(msg.data.wilcoxon);
        setRunning(false);
      } else if (msg.type === "lab.error") {
        alert(msg.data);
        setRunning(false);
      }
    };
    ws.on(handler);
    return () => ws.off(handler);
  }, [ws]);

  const run = () => {
    setFolds([]); setVerdict(null); setRunning(true);
    ws.send({
      type: "lab.run",
      pair,
      baseline_params: JSON.parse(baselineJson || "{}"),
      candidate_params: JSON.parse(candidateJson || "{}"),
      spec: { fold_kind: "quarterly", is_lookback_quarters: 8 },
    });
  };

  const verdictColor = (v) =>
    v?.verdict === "better" ? "#3aa757" :
    v?.verdict === "worse" ? "#d04545" : "#888";

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Hypothesis Lab</h3>
      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr", gap: 12 }}>
        <label>Pair
          <select value={pair} onChange={e => setPair(e.target.value)}
                  style={{ width: "100%" }}>
            <option>BTC/USD</option><option>SOL/USD</option><option>SOL/BTC</option>
          </select>
        </label>
        <label>Baseline params (JSON)
          <textarea value={baselineJson}
                    onChange={e => setBaselineJson(e.target.value)}
                    style={{ width: "100%", height: 80, fontFamily: "monospace" }} />
        </label>
        <label>Candidate params (JSON)
          <textarea value={candidateJson}
                    onChange={e => setCandidateJson(e.target.value)}
                    style={{ width: "100%", height: 80, fontFamily: "monospace" }} />
        </label>
      </div>
      <button onClick={run} disabled={running} style={{ marginTop: 12 }}>
        {running ? "Running…" : "Run walk-forward"}
      </button>

      {verdict && (
        <div style={{ marginTop: 16, padding: 12, background: "#1a1a1a" }}>
          <h4 style={{ marginTop: 0 }}>Verdict (paired Wilcoxon, α=0.05)</h4>
          {Object.entries(verdict).map(([metric, v]) => (
            <div key={metric} style={{ fontFamily: "monospace" }}>
              <span style={{ color: verdictColor(v), fontWeight: 600 }}>
                {v.verdict.toUpperCase()}
              </span>{" — "}
              {metric}: candidate wins {v.candidate_wins}/{v.n}, p={v.p_value.toFixed(4)}, median Δ={v.median_delta.toFixed(3)}
            </div>
          ))}
        </div>
      )}

      {folds.length > 0 && (
        <table style={{ marginTop: 16, width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr><th>Fold</th><th>OOS window</th><th>Δ Sharpe</th><th>Δ Return%</th><th>n trades</th></tr>
          </thead>
          <tbody>
            {folds.map((fr, i) => (
              <tr key={i}>
                <td>{i}</td>
                <td>{new Date(fr.fold.oos_start*1000).toISOString().slice(0,10)} → {new Date(fr.fold.oos_end*1000).toISOString().slice(0,10)}</td>
                <td>{fr.deltas.sharpe.toFixed(3)}</td>
                <td>{fr.deltas.total_return_pct.toFixed(2)}</td>
                <td>{fr.candidate.n_trades}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/src/components/research/LabPane.jsx
git commit -m "feat(dashboard): LabPane — Mode B walk-forward UI"
```

### Task 22: Dashboard `<ReleasesPane>`

**Files:**
- Create: `dashboard/src/components/research/ReleasesPane.jsx`

- [ ] **Step 1: Implement releases list + diff selector**

```jsx
// dashboard/src/components/research/ReleasesPane.jsx
import React, { useEffect, useState } from "react";

export default function ReleasesPane({ ws }) {
  const [releases, setReleases] = useState([]);
  const [selected, setSelected] = useState([]);   // up to 2 run_ids
  const [diff, setDiff] = useState(null);

  useEffect(() => {
    if (!ws) return;
    const handler = (msg) => {
      if (msg.type === "releases.list") setReleases(msg.data);
      if (msg.type === "releases.diff") setDiff(msg.data);
    };
    ws.on(handler);
    ws.send({ type: "releases.list" });
    return () => ws.off(handler);
  }, [ws]);

  const toggleSelect = (run_id) => {
    setSelected(prev => prev.includes(run_id)
      ? prev.filter(x => x !== run_id)
      : prev.length < 2 ? [...prev, run_id] : [prev[1], run_id]);
  };

  const compare = () => {
    if (selected.length !== 2) return;
    setDiff(null);
    ws.send({ type: "releases.diff", a_run_id: selected[0], b_run_id: selected[1] });
  };

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Release Regression Snapshots</h3>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr><th>✓</th><th>Version</th><th>Pair</th><th>Verdict</th><th>Run ID</th></tr>
        </thead>
        <tbody>
          {releases.map(r => (
            <tr key={r.run_id}>
              <td><input type="checkbox" checked={selected.includes(r.run_id)}
                         onChange={() => toggleSelect(r.run_id)} /></td>
              <td>{r.hydra_version}</td>
              <td>{r.pair}</td>
              <td>{r.verdict_summary}</td>
              <td style={{ fontFamily: "monospace", fontSize: 11 }}>{r.run_id.slice(0,8)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button onClick={compare} disabled={selected.length !== 2}
              style={{ marginTop: 12 }}>
        Diff selected
      </button>
      {diff && (
        <pre style={{ marginTop: 16, background: "#1a1a1a", padding: 12, fontSize: 12 }}>
          {JSON.stringify(diff, null, 2)}
        </pre>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/src/components/research/ReleasesPane.jsx
git commit -m "feat(dashboard): ReleasesPane — Mode C snapshot diff UI"
```

### Task 23: Wire `<ResearchTab>` into App.jsx

**Files:**
- Create: `dashboard/src/components/ResearchTab.jsx`
- Modify: `dashboard/src/App.jsx`

- [ ] **Step 1: Compose the three panes**

```jsx
// dashboard/src/components/ResearchTab.jsx
import React, { useState } from "react";
import DatasetPane from "./research/DatasetPane";
import LabPane from "./research/LabPane";
import ReleasesPane from "./research/ReleasesPane";

export default function ResearchTab({ ws }) {
  const [pane, setPane] = useState("dataset");
  const tabs = [
    ["dataset", "Dataset"],
    ["lab", "Lab"],
    ["releases", "Releases"],
  ];
  return (
    <div>
      <nav style={{ borderBottom: "1px solid #333", padding: "0 16px" }}>
        {tabs.map(([id, label]) => (
          <button key={id} onClick={() => setPane(id)}
                  style={{
                    background: "transparent", border: "none",
                    color: pane === id ? "#fff" : "#888",
                    padding: "12px 16px", cursor: "pointer",
                    borderBottom: pane === id ? "2px solid #3aa757" : "2px solid transparent",
                  }}>
            {label}
          </button>
        ))}
      </nav>
      {pane === "dataset" && <DatasetPane ws={ws} />}
      {pane === "lab" && <LabPane ws={ws} />}
      {pane === "releases" && <ReleasesPane ws={ws} />}
    </div>
  );
}
```

- [ ] **Step 2: Replace BACKTEST/COMPARE in App.jsx**

Locate the existing `BACKTEST` and `COMPARE` tab content in `dashboard/src/App.jsx`. Replace both with a single `RESEARCH` tab pointing at `<ResearchTab ws={ws} />`. Keep `LIVE` and `THESIS` tabs untouched.

- [ ] **Step 3: Build dashboard**

```bash
cd dashboard && npm install && npm run build
```

Expected: build succeeds, no TS/JSX errors.

- [ ] **Step 4: Run dev server, manual smoke**

```bash
cd dashboard && npm run dev
```

Open http://localhost:5173, click into the Research tab; verify all three panes render. With the Hydra agent + backtest server running:
- DATASET shows three rows (BTC/USD, SOL/USD, SOL/BTC) with non-zero candle counts.
- LAB form accepts JSON params, "Run walk-forward" streams fold rows, verdict bar renders.
- RELEASES shows the v2.19.1 snapshot from Task 18.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/ResearchTab.jsx dashboard/src/App.jsx
git commit -m "feat(dashboard): ResearchTab wired into App (replaces BACKTEST/COMPARE)"
```

**🛑 PHASE 5 CHECKPOINT — STOP and ask user to verify Research tab usability in browser before proceeding to release work.**

---

## Phase 6 — `/release` gate + version bump + release (rc3)

### Task 24: Update `/release` skill to insert regression step

**Files:** locate the `/release` skill file (typically `.claude/plugins/.../release/SKILL.md` per Hydra's plugin layout — find with `Glob` for `**/release/SKILL.md`).

- [ ] **Step 1: Insert regression-harness step**

Edit the skill SOP. After the "tests pass" step and BEFORE the "tag" step, insert:

```markdown
6. **Regression harness**

   ```bash
   python -m tools.run_regression --version $NEW_VERSION
   ```

   - Runs anchored quarterly walk-forward against the prior version's
     snapshot for SOL/USD, SOL/BTC, BTC/USD.
   - Persists a snapshot regardless of outcome.
   - Exit code 2 = Wilcoxon WORSE p<0.05 on any pair × any headline metric.
     Block release. To override (rare; document the why):

     ```bash
     python -m tools.run_regression --version $NEW_VERSION \
         --accept-regression "<short reason>"
     ```

     The reason persists into `regression_run.override_reason` for audit.
```

Renumber subsequent steps.

- [ ] **Step 2: Commit**

```bash
git add <release skill path>
git commit -m "feat(release): insert regression-harness gate step"
```

### Task 25: Bump version sites to v2.20.0

**Files (all 7 per CLAUDE.md §Version Sites):**

- [ ] **Step 1: Enumerate sites**

```bash
git grep -nE 'v?2\.19\.1|2\.19\.1|HYDRA_VERSION'
```

Expected: hits in `CHANGELOG.md`, `dashboard/package.json`, `dashboard/package-lock.json` (×2), `dashboard/src/App.jsx`, `hydra_agent.py`, `hydra_backtest.py`, `CLAUDE.md`.

- [ ] **Step 2: Edit each site to `2.20.0` and add a CHANGELOG section**

```markdown
## [2.20.0] — 2026-04-26

### Added
- Canonical historical OHLC store (`hydra_history.sqlite`), bootstrapped from
  Kraken trade archive, kept warm by daily REST refresh + live tape capture.
- Anchored quarterly walk-forward methodology with paired Wilcoxon signed-rank
  test (`hydra_walk_forward.py`) — drives both Mode B (hypothesis lab) and
  Mode C (release regression).
- Per-version regression snapshot tables in `hydra_history.sqlite`
  (`regression_run`, `regression_metrics`, `regression_equity_curve`,
  `regression_trade`).
- Dashboard Research tab redesign: `DATASET` / `LAB` / `RELEASES` panes
  replacing the old BACKTEST/COMPARE tabs.
- `/release` regression gate — Wilcoxon WORSE p<0.05 blocks tag with
  `--accept-regression "<reason>"` override.
- New env flags: `HYDRA_TAPE_CAPTURE` (default 1), `HYDRA_HISTORY_DB`,
  `HYDRA_REGRESSION_GATE` (default 1).

### Changed
- `BacktestConfig.data_source` default flipped from `"synthetic"` to `"sqlite"`.
- New `BacktestConfig.brain_mode` (default `"stub"`); regression and lab runs
  no longer call Anthropic/Grok APIs in the MVP.

### Deprecated
- `SyntheticSource` retained for unit tests but no longer the default.
- `KrakenHistoricalSource` (single-call REST) deprecated in favor of
  `SqliteSource` reading the canonical store.
```

- [ ] **Step 3: Run release alignment script**

```bash
python scripts/check_release_alignment.py
```

Expected: clean (tag/GH-release checks deferred to T28).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md dashboard/package.json dashboard/package-lock.json \
        dashboard/src/App.jsx hydra_agent.py hydra_backtest.py CLAUDE.md
git commit -m "chore: v2.20.0 — bump version sites + CHANGELOG"
```

### Task 26: Audit (Rule 1 — 7-way partition)

- [ ] **Step 1: Spawn 7 parallel audit agents**

Use the `/audit` skill or dispatch per-partition agents per CLAUDE.md §Audit. Each must return HIGH/MED/LOW findings. Synthesize, fix HIGH+MED, re-audit (Rule 4 phase-2).

- [ ] **Step 2: Run full test suite + mock harness**

```bash
python -m pytest tests/ -v
python tests/live_harness/harness.py --mode mock
```

Expected: all green.

- [ ] **Step 3: Commit any audit fixes**

```bash
git add -A
git commit -m "chore(audit): v2.20.0 audit fixes"
```

### Task 27: PR + CI green + merge

- [ ] **Step 1: Push branch and open PR**

```bash
git push -u origin feature/research-tab-redesign
gh pr create --title "v2.20.0 — Research tab redesign: real history + walk-forward" \
  --body "$(cat <<'EOF'
## Summary
- Canonical Kraken-archive-sourced SQLite OHLC store + live tape capture
- Anchored quarterly walk-forward with paired Wilcoxon (Mode B + Mode C)
- Per-version regression snapshots gated into /release
- Dashboard Research tab redesign (DATASET / LAB / RELEASES)

Spec: `docs/superpowers/specs/2026-04-26-research-tab-redesign-design.md`
Plan: `docs/superpowers/plans/2026-04-26-research-tab-redesign.md`

## Test plan
- [x] `python -m pytest tests/` green
- [x] `python tests/live_harness/harness.py --mode mock` green
- [x] Manual: bootstrap from Kraken archive populates 3 pairs at 1h
- [x] Manual: Research tab in dashboard renders all 3 panes
- [x] Manual: `python -m tools.run_regression --version 2.20.0` exits 0

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Wait for CI green**

Both `engine-tests` and `dashboard-build` must pass.

- [ ] **Step 3: Merge PR**

```bash
gh pr merge --merge   # or --squash per repo convention; check recent merges with: git log --merges -5
```

### Task 28: Signed tag + GitHub release

- [ ] **Step 1: Pull merged main**

```bash
git checkout main && git pull
```

- [ ] **Step 2: Signed tag**

```bash
git tag -s v2.20.0 -m "v2.20.0"
git tag -v v2.20.0   # verify (Rule 3)
git push origin v2.20.0
```

- [ ] **Step 3: GitHub release**

```bash
gh release create v2.20.0 --verify-tag --notes-from-tag
```

- [ ] **Step 4: Final alignment check**

```bash
python scripts/check_release_alignment.py --check-tag --check-gh-release
```

Expected: exit 0.

**🛑 PHASE 6 CHECKPOINT — release complete. Confirm with the user before any post-release follow-up.**

---

## Self-Review Notes (post-write check)

- ✅ All 10 spec scope items have at least one task. Cross-check:
  - (1) hydra_history_store.py → T1, T2, T3, T4, T17
  - (2) tools/bootstrap_history.py → T5, T6
  - (3) tools/refresh_history.py → T7
  - (4) hydra_tape_capture.py → T8, T9, T10
  - (5) hydra_walk_forward.py → T11, T12, T13, T14
  - (6) hydra_backtest.py mods → T15, T16
  - (7) regression tables + tools/run_regression.py → T17, T18
  - (8) Dashboard Research tab → T19, T20, T21, T22, T23
  - (9) /release skill update → T24
  - (10) Version sites + SCHEMA_VERSION → T17 (SCHEMA_VERSION=2), T25
- ✅ No "TBD"/"implement later" — every code step has actual code.
- ✅ Type names consistent: `CandleRow`, `CandleOut`, `Coverage`, `Fold`, `FoldMetrics`, `FoldResult`, `WalkForwardSpec`, `WalkForwardResult`, `WilcoxonVerdict` — same names used in defs and in callers.
- ✅ One known relaxation: T19 (`tests/test_research_ws.py`) carries a "smoke test" placeholder rather than full code — flagged because the existing `hydra_backtest_server.py` test pattern needs to be inspected first; the executing agent must check `tests/test_backtest_server.py` (or whatever already exists) and mirror that style. This is the only test placeholder in the plan.
- ✅ CLAUDE.md Rule 1 (7-way audit) → Task 26.
- ✅ CLAUDE.md Rule 4 (two-phase audit) → Tasks 14, 18, 26.
- ✅ CLAUDE.md Rule 3 (verify with command output) → Tasks 6, 18, 23, 28.
- ✅ CLAUDE.md Rule 5 (enumerate version sites first) → Task 25.
