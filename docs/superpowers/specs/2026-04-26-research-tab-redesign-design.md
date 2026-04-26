# Research Tab Redesign — Design Spec

**Date:** 2026-04-26
**Author:** Claude (Opus 4.7) + eternal-roman
**Status:** Draft, pending user review
**Target Hydra version:** v2.20.0 (MINOR — material upgrade)

## 1. Problem

The current Research tab (backtest + experiments + compare, surfaced through `hydra_backtest_server.py` + dashboard `BACKTEST` / `COMPARE` / `THESIS` tabs) is not fit for purpose:

1. **Default `data_source = "synthetic"`.** Most runs replay a per-pair seeded GBM/OU random walk, not real Kraken history. There is no exploitable structure in synthetic data — strategy "edge" detected on it is meaningless.
2. **Even when `data_source = "kraken"` is selected, history is shallow.** `KrakenHistoricalSource.iter_candles` (`hydra_backtest.py:396`) makes one `kraken ohlc` REST call, which returns ~720 candles regardless of `since` (~7.5 days at 15m, ~30 days at 1h). Multi-regime back-testing is impossible.
3. **Seed-driven divergence dominates.** Comparing seed=42 vs seed=15 yields two unrelated synthetic universes. Different seeds *should* diverge, but on synthetic data the divergence is the entire signal — there is no underlying "truth" they're sampling from.
4. **No release-over-release regression.** A v2.X.Y release ships with no historical-replay artifact. There is no answer to "did this version change edge?" beyond CI test pass/fail.
5. **Dashboard UX is opaque** ("opaque chat window with unclear seeds and synthetic candles") — driven by `BacktestConfig` field surface area rather than by the questions a user actually has.

## 2. Goals

The redesign must support two layered modes:

- **Mode B — Hypothesis Lab.** "Does raising `momentum_rsi_upper` to 75 in TREND_UP have edge?" Output: a paired statistical verdict + equity-curve overlay + per-regime breakdown.
- **Mode C — Release Regression Harness.** Each tagged Hydra version produces a frozen snapshot artifact. Future versions are diffed against it. Output: "v2.20 vs v2.19 on canonical history — better / worse / equivocal per pair × per metric."

Predictive power criterion: **a backtest verdict that says "edge" must correlate with live-Hydra edge.** This pins the methodology (anchored walk-forward, OOS-only scoring) over flashier alternatives (block-bootstrap CIs, in-sample optimism).

Out of scope for v2.20.0:
- Mode D ("deep replay receipt" — every signal/decision logged) — opt-in later.
- Block-bootstrap confidence intervals — defer; B+ already addresses predictiveness.
- "With-AI-brain" expensive mode in the harness — defer (cost + non-determinism); brain is stubbed.
- Sub-hour grains (15m, 5m, 1m). Schema supports them; bootstrap doesn't yet.

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Canonical Historical Store (NEW)                               │
│  hydra_history.sqlite                                           │
│    ohlc(pair, grain_sec, ts, open, high, low, close, volume,    │
│         source, ingested_at)                                    │
│    PRIMARY KEY (pair, grain_sec, ts)                            │
│                                                                 │
│  Populated by:                                                  │
│   1. Bootstrap: Kraken_Trading_History.zip → roll to 1h         │
│   2. Daily refresh: kraken ohlc REST (last ~30 days)            │
│   3. Live tape capture: hydra_ohlc_stream → INSERT OR IGNORE    │
└─────────────────────────────────────────────────────────────────┘
              │
              ├──► Mode B: Hypothesis Lab
              │      hydra_backtest.SqliteSource(pair, grain, start, end)
              │      → walk-forward folds (anchored IS / quarterly OOS)
              │      → paired Wilcoxon on OOS metric deltas
              │
              └──► Mode C: Release Regression Harness
                     same fold engine, baseline=prior version snapshot
                     → INSERT into regression_run / regression_metrics /
                       regression_equity_curve / regression_trade
```

## 4. Components

### 4.1 New: `hydra_history_store.py`

**Purpose:** single source of truth for historical OHLC. Stdlib-only (`sqlite3`).

**Public API:**
```python
class HistoryStore:
    def __init__(self, path: str = "hydra_history.sqlite")
    def upsert_candles(self, rows: Iterable[CandleRow]) -> int
    def fetch(self, pair: str, grain_sec: int,
              start_ts: int, end_ts: int) -> Iterator[Candle]
    def coverage(self, pair: str, grain_sec: int) -> Coverage
    # Coverage = (first_ts, last_ts, candle_count, gap_count, max_gap_sec)
    def list_pairs(self) -> List[Tuple[str, int]]   # (pair, grain_sec)
```

**Schema (canonical, in perpetuity):**
```sql
CREATE TABLE IF NOT EXISTS ohlc (
  pair         TEXT    NOT NULL,    -- registry-canonical, e.g. "BTC/USD"
  grain_sec    INTEGER NOT NULL,    -- 60, 300, 900, 3600, 86400
  ts           INTEGER NOT NULL,    -- unix seconds, candle OPEN, UTC
  open         REAL    NOT NULL,
  high         REAL    NOT NULL,
  low          REAL    NOT NULL,
  close        REAL    NOT NULL,
  volume       REAL    NOT NULL,
  source       TEXT    NOT NULL,    -- 'kraken_archive'|'kraken_rest'|'tape'
  ingested_at  INTEGER NOT NULL,
  PRIMARY KEY (pair, grain_sec, ts)
);
CREATE INDEX IF NOT EXISTS ix_ohlc_pair_grain_ts
  ON ohlc(pair, grain_sec, ts);
```

**Pair canonicalization:** uses `hydra_pair_registry.PairRegistry`. Storage form is always `BASE/QUOTE` with registry-canonical assets (BTC, USD — never XBT/ZUSD).

**Conflict policy on upsert:** `INSERT OR REPLACE` keyed by `(pair, grain_sec, ts)`. The `source` column tracks provenance; `kraken_archive` writes win on bootstrap, then `kraken_rest` and `tape` refresh the trailing edge.

**Source tier:**
1. `kraken_archive` — gold (deepest, immutable)
2. `kraken_rest` — refreshes the last 30d daily
3. `tape` — live capture, fills gaps inside the 30d window in real time

### 4.2 New: `tools/bootstrap_history.py`

**Purpose:** one-time roll of `Kraken_Trading_History.zip` → SQLite.

**Inputs:**
- `--zip` path (default: `~/Downloads/Kraken_Trading_History.zip`)
- `--pairs SOLUSD,XBTUSD,SOLXBT` (Kraken file names; mapped to canonical via registry)
- `--grain 3600` (default 1h)
- `--out hydra_history.sqlite`

**Algorithm:**
1. Open the zip; for each requested pair file, stream-read the CSV (`unixtime,price,volume` per Kraken trade-archive format) — never load into RAM.
2. Per (pair, grain) bucket: maintain `(open, high, low, close, volume)` accumulator. When a trade ts crosses the next grain boundary, emit the closed candle and reset.
3. Buffer 10k candles, then bulk `INSERT OR REPLACE`.
4. Print coverage report at end: `BTC/USD 2013-09-06 → 2026-01-24 (109k candles)`.

**Cost:** XBTUSD is 2.7 GB — read once, drop trades, keep candles. Total runtime ~3-5 min on the user's box. Disk: SQLite ~50 MB total for all three pairs at 1h.

### 4.3 New: `hydra_tape_capture.py`

**Purpose:** keep canonical store warm with live closes. Subscribes to existing `hydra_ohlc_stream` (no new network surface).

**Behavior:**
- On every closed-candle event from any pair the agent is running, `INSERT OR IGNORE` into `ohlc` with `source='tape'`.
- `INSERT OR IGNORE` (not `REPLACE`) — `kraken_archive` and `kraken_rest` writes are authoritative on the same ts.
- Runs in-process inside `hydra_agent.py`; gated by env `HYDRA_TAPE_CAPTURE=1` (default ON for production launchers).

### 4.4 New: `tools/refresh_history.py`

**Purpose:** daily catch-up via REST OHLC.

- Iterates each `(pair, grain_sec)` in `coverage()`.
- Calls `kraken ohlc <pair> --interval <grain_min>` (existing CLI, 2s rate-limit floor enforced).
- `INSERT OR REPLACE` for any candle whose `source` is not `kraken_archive`.
- Idempotent. Safe to run hourly via Windows Task Scheduler or invoked at agent startup.

### 4.5 Modified: `hydra_backtest.py`

Replace `SyntheticSource` as default; keep `KrakenHistoricalSource` for backward-compat but mark deprecated.

**New default:** `SqliteSource(pair, grain_sec, start_ts, end_ts)` reads from `hydra_history.sqlite` via `HistoryStore.fetch`.

**`BacktestConfig` changes:**
- `data_source: str = "sqlite"` (was `"synthetic"`).
- New: `walk_forward: WalkForwardSpec | None`. When set, `BacktestRunner.run()` returns a `WalkForwardResult` (per-fold metrics + paired Wilcoxon verdict) instead of a single `BacktestResult`.
- `random_seed` becomes vestigial for sqlite-source runs (no randomness in price path); retained internally for stub-AI tie-breaking determinism but removed from the LAB pane UI.

**Brain stubbing:** new `BacktestConfig.brain_mode: str = "stub"` (alternatives: `"stub"|"replay"|"live"`). `"stub"` short-circuits the AI brain to a deterministic "approve if quant-rules approve, else hold" decision. `"live"` calls Anthropic/Grok APIs (cost!); `"replay"` reads cached responses from a future cache layer (Mode D infrastructure, deferred). MVP only ships `"stub"`.

### 4.6 New: `hydra_walk_forward.py`

**Purpose:** the methodology kernel. Stdlib only.

**Public API:**
```python
@dataclass(frozen=True)
class WalkForwardSpec:
    fold_kind: str = "quarterly"   # MVP: only quarterly
    is_lookback_quarters: int = 8  # rolling IS cap
    min_oos_trades: int = 5        # discard folds too sparse to score
    metrics: Tuple[str, ...] = ("sharpe", "total_return_pct",
                                 "max_dd_pct", "fee_adj_return_pct")

@dataclass
class FoldResult:
    is_start: int; is_end: int; oos_start: int; oos_end: int
    baseline: BacktestMetrics
    candidate: BacktestMetrics
    deltas: Dict[str, float]   # candidate - baseline

@dataclass
class WalkForwardResult:
    folds: List[FoldResult]
    wilcoxon: Dict[str, WilcoxonVerdict]   # one per metric
    # WilcoxonVerdict = (n_folds, w_stat, p_value, candidate_wins,
    #                    median_delta, verdict: "better"|"worse"|"equivocal")

def run_walk_forward(
    history: HistoryStore,
    pair: str,
    baseline_cfg: BacktestConfig,
    candidate_cfg: BacktestConfig,
    spec: WalkForwardSpec,
) -> WalkForwardResult: ...
```

**Wilcoxon signed-rank** implemented inline (stdlib only). Two-sided test, p<0.05 verdict threshold; medians used for direction reporting (paired data — means are misleading on Sharpe-like metrics).

**OOS isolation invariant:** the candidate engine is constructed fresh per fold and seeded with state derived **only from the IS window** of that fold. No state leaks across folds. No state leaks from OOS back to IS. This is the core anti-look-ahead property.

### 4.7 New: regression-snapshot tables

In `hydra_history.sqlite` (same DB — one source of truth for the research stack):

```sql
CREATE TABLE regression_run (
  run_id        TEXT PRIMARY KEY,        -- uuid4 hex
  hydra_version TEXT NOT NULL,
  git_sha       TEXT NOT NULL,
  param_hash    TEXT NOT NULL,
  pair          TEXT NOT NULL,
  grain_sec     INTEGER NOT NULL,
  spec_json     TEXT NOT NULL,           -- WalkForwardSpec as JSON
  override_reason TEXT,                  -- non-null iff release was forced past WORSE verdict
  created_at    INTEGER NOT NULL
);

CREATE TABLE regression_metrics (
  run_id     TEXT NOT NULL,
  fold_idx   INTEGER NOT NULL,           -- -1 = aggregate over all folds
  metric     TEXT NOT NULL,              -- 'sharpe', 'total_return_pct', ...
  value      REAL NOT NULL,
  PRIMARY KEY (run_id, fold_idx, metric),
  FOREIGN KEY (run_id) REFERENCES regression_run(run_id)
);

CREATE TABLE regression_equity_curve (
  run_id  TEXT NOT NULL,
  ts      INTEGER NOT NULL,
  equity  REAL NOT NULL,
  PRIMARY KEY (run_id, ts),
  FOREIGN KEY (run_id) REFERENCES regression_run(run_id)
);

CREATE TABLE regression_trade (
  run_id    TEXT NOT NULL,
  trade_idx INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  side      TEXT NOT NULL,               -- BUY|SELL
  price     REAL NOT NULL,
  size      REAL NOT NULL,
  fee       REAL NOT NULL,
  regime    TEXT,
  reason    TEXT,
  PRIMARY KEY (run_id, trade_idx),
  FOREIGN KEY (run_id) REFERENCES regression_run(run_id)
);
```

A "snapshot" = the row in `regression_run` plus its dependent rows. To diff two versions, you join on `pair`+`grain_sec`+`spec_json` and compare metrics/curves/trades.

### 4.8 Modified: `hydra_backtest_server.py` + dashboard Research tab

The existing free-form `BACKTEST` / `COMPARE` / `THESIS` panels are replaced with three structured panes:

**`DATASET` pane.**
Read-only inspector for `hydra_history.sqlite`.
Per pair × grain row: first_ts, last_ts, candle_count, gap_count, max_gap, last_refresh source. Banner if any pair shows a gap > 1 day in the last 90 days. No write controls (refresh is via cron / scripts).

**`LAB` pane (Mode B).**
Form-driven, not config-blob-driven:
- *Pair* (single picker — start with one pair, defer multi-pair to phase 2).
- *Baseline params* (default: current live params from tuner files).
- *Candidate params* (diff editor: only fields that differ from baseline are shown / persisted).
- *Walk-forward spec* (dropdown of presets — "default quarterly", "monthly aggressive", "annual conservative" — quarterly the only one shipped at MVP).
- *Run.* Async; streams fold-by-fold progress over WS.
Output:
- Fold table: `IS window | OOS window | baseline Sharpe | candidate Sharpe | Δ | … (other metrics) | n_trades`.
- Verdict bar: per metric `Wilcoxon n=14, candidate wins 11/14, p=0.012 → BETTER (Sharpe)`.
- Equity-curve overlay (concatenated OOS windows).
- Per-regime breakdown table.

Mode B is single-pair-at-a-time at MVP. Multi-pair lab runs are deferred (the methodology is unchanged; it's a UI iteration).

**`RELEASES` pane (Mode C).**
- List of every `regression_run` grouped by `hydra_version` (latest first).
- Click any version → snapshot detail: per-pair fold table, equity curve, headline metrics.
- Click two versions → diff view: same fold structure, side-by-side, with the same Wilcoxon verdict applied.
- "Run regression for current branch" button → triggers a run, persists with `hydra_version` taken from `HYDRA_VERSION` constant. The regression iterates **internally** over every pair in the default triangle (`SOL/USD`, `SOL/BTC`, `BTC/USD`) and emits one `regression_run` row per pair.

The existing `THESIS` tab is **not changed by this redesign** — thesis layer is orthogonal.

### 4.9 `/release` skill integration

Update `release` skill to insert a step **after tests pass, before tag**:

```
6. Regression harness
   - python tools/run_regression.py --version $NEW_VERSION
   - Reads hydra_history.sqlite, runs walk-forward against the prior
     version's snapshot for each pair in the default triangle.
   - Persists the new snapshot regardless of outcome.
   - Verdict gate:
     * If any pair × any headline metric shows Wilcoxon WORSE p<0.05
       → block release. Require explicit `--accept-regression "<reason>"`
         to proceed (writes the reason into regression_run.spec_json).
     * BETTER or EQUIVOCAL → proceed.
```

CI workflow does NOT run the harness on every PR (slow, depends on local SQLite). Release-time only.

## 5. Data Flow

### 5.1 Initial bootstrap (one-time, manual)

```
Kraken_Trading_History.zip
   │
   └──► tools/bootstrap_history.py
          │
          └──► hydra_history.sqlite (ohlc table, source='kraken_archive')
                 BTC/USD: ~2013-09 → ~2026-01  (~108k candles @ 1h)
                 SOL/USD: ~2021-08 → ~2026-01  (~38k candles @ 1h)
                 SOL/BTC: ~2021-08 → ~2026-01  (~38k candles @ 1h)
```

### 5.2 Daily incremental

```
Windows Task Scheduler (or agent startup hook)
   │
   └──► tools/refresh_history.py
          │
          └──► kraken ohlc {pair} --interval 60   (last ~720 candles per pair)
                 │
                 └──► INSERT OR REPLACE into ohlc (source='kraken_rest')
                       (only overwrites non-archive rows)
```

### 5.3 Live tape

```
hydra_ohlc_stream  ──► hydra_tape_capture.on_candle_close(pair, candle)
                          │
                          └──► INSERT OR IGNORE into ohlc (source='tape')
                                 (preserves archive/rest authoritative writes)
```

### 5.4 Mode B run

```
LAB pane → BacktestConfig (baseline) + BacktestConfig (candidate)
   │
   └──► run_walk_forward(history, pair, baseline, candidate, spec)
          │
          ├── Per fold: HistoryStore.fetch(pair, grain, oos_start, oos_end)
          │             → BacktestRunner with brain_mode="stub"
          │             → BacktestMetrics
          │
          └── Aggregate → WalkForwardResult → WS push to dashboard
```

### 5.5 Mode C run

```
/release skill (or RELEASES → "Run regression")
   │
   └──► run_walk_forward(history, pair, baseline=prior_snapshot,
                          candidate=current_branch_params, spec)
          │
          └──► persist into regression_run / regression_metrics /
                regression_equity_curve / regression_trade
                (run_id = uuid4, hydra_version = HYDRA_VERSION)
```

## 6. Error Handling

- **Missing pair coverage.** `HistoryStore.fetch` returning empty for a fold → fold skipped, logged, counted in `WalkForwardResult.skipped_folds`. If >50% of folds skipped → run fails with explicit "insufficient history for `<pair>` in window" error.
- **Gap inside a fold.** Gaps >1 candle inside an OOS window → fold marked `degraded`, included but flagged. Gaps >24h → fold skipped.
- **Bootstrap zip missing or corrupt.** `bootstrap_history.py` fails fast with the path it tried; never partial-writes (uses a temp DB and renames at the end).
- **REST refresh fails.** `refresh_history.py` exits non-zero, leaves the store untouched. `coverage()` last-refresh banner in the DATASET pane goes amber after >2 days stale.
- **Concurrent writers.** SQLite WAL mode + retry-on-busy. Bootstrap and refresh are mutually exclusive (a `_bootstrap.lock` flag in the DB dir); tape capture coexists with refresh because it uses `INSERT OR IGNORE`.
- **Schema migrations.** A `schema_version` row in a `meta` table; migrations are explicit numbered scripts. No silent ALTERs.

## 7. Testing

- `tests/test_history_store.py` — schema, upsert, fetch, coverage, gap detection.
- `tests/test_bootstrap_history.py` — synthetic mini-zip fixture; verify rollup math vs hand-computed candles for 100 trades.
- `tests/test_walk_forward.py` — synthetic deterministic engine; verify fold structure, IS/OOS isolation invariant, Wilcoxon math against scipy reference values (scipy in dev-deps only, not engine).
- `tests/test_tape_capture.py` — fakes a stream of candle-close events; verifies INSERT OR IGNORE preserves archive/rest rows.
- Mock harness (`tests/live_harness/harness.py --mode mock`) extended to assert the agent boots cleanly with `HYDRA_TAPE_CAPTURE=1`.
- Backwards-compat: existing synthetic-source backtest tests retained (synthetic source not deleted, only demoted from default).

## 8. Migration / Rollout

1. **v2.20.0-rc1:** ship `hydra_history_store.py`, `bootstrap_history.py`, `refresh_history.py`, `hydra_tape_capture.py` (default OFF), `hydra_walk_forward.py`. No dashboard changes yet. User runs bootstrap manually once.
2. **v2.20.0-rc2:** dashboard Research tab swap (`DATASET` / `LAB` / `RELEASES` panes). Tape capture default ON.
3. **v2.20.0:** `/release` skill update, regression-gate enforced.
4. **Backward compat:** synthetic source remains available via `data_source="synthetic"` for unit tests and demo, but is no longer the default in dashboard or CLI.

## 9. Version Sites Touched (Rule 5)

This is a v2.20.0 cycle. Sites:
1. `CHANGELOG.md` — new `## [2.20.0]` section.
2. `dashboard/package.json` — version field.
3. `dashboard/package-lock.json` — both version fields.
4. `dashboard/src/App.jsx` — `HYDRA v2.20.0` footer.
5. `hydra_agent.py` — `_export_competition_results()` version field.
6. `hydra_backtest.py` — `HYDRA_VERSION = "2.20.0"`.
7. Git tag — signed `v2.20.0`.
8. GitHub Release — `gh release create v2.20.0`.
9. **NEW: `hydra_history_store.py`** — `SCHEMA_VERSION = 1`. Independent of HYDRA_VERSION; bumps only on schema migration.

## 10. Env Flags Added

| flag | default | effect |
|---|---|---|
| `HYDRA_TAPE_CAPTURE` | `1` | live tape writes to `hydra_history.sqlite`; `=0` disables (e.g. for paper tests on a separate machine sharing the DB). |
| `HYDRA_HISTORY_DB` | `hydra_history.sqlite` | path override for the canonical store. |
| `HYDRA_REGRESSION_GATE` | `1` | when `1`, Wilcoxon WORSE p<0.05 in `tools/run_regression.py` blocks the release step. Set to `0` to disable the gate (e.g. when intentionally accepting regression — must still record `--accept-regression "<reason>"` to populate `regression_run.override_reason`). Read only by `tools/run_regression.py`; live agent ignores it. |

## 11. Cross-Cutting Invariants Added

- **History store is append-only by source-tier.** `kraken_archive` writes are immutable; `kraken_rest` and `tape` may overwrite each other on the trailing edge but never the archive. Enforced in `upsert_candles`.
- **Walk-forward OOS isolation.** Engine state at fold N's OOS start is derived only from history strictly before OOS start. No fold-N OOS data feeds back into fold N+1's IS warmup beyond what real time would have allowed. (Anchored walk-forward, not "leaky" walk-forward.)
- **Regression harness is brain-stubbed.** `brain_mode="stub"` is enforced in `tools/run_regression.py`; LLM calls are unreachable from the regression code path. (Removes Anthropic/Grok API drift as a confound.)
- **One DB, one truth.** `ohlc` and `regression_*` tables share `hydra_history.sqlite`. The `.hydra-experiments/` JSON store is retained for the existing presets/experiments concepts but is no longer the home of regression artifacts.

## 12. Open Questions / Risks

- **Trade-archive recency.** The zip is from 2026-01-24. Refresh closes the gap to "now" via REST + tape. First boot post-bootstrap, the trailing ~3 months are filled by REST (capable: ~30 days at 1h × 4 calls back = ~120 days). Confirm REST `since` actually paginates that far for hourly — if not, accept a small gap and let tape capture fill forward.
- **Wilcoxon power on 4.5 years of SOL.** ~16 quarterly folds. Borderline for detecting small effect sizes. Acceptable for MVP — call out in `RELEASES` pane verdict ("low statistical power on SOL/USD: n=16 folds").
- **Dashboard CSS drift.** `dashboard/src/App.jsx` is a single-file React component with inline styles; three new panes is non-trivial. Plan factors out a `<ResearchTab>` sub-component but does not refactor the rest of `App.jsx`.
- **Live execution interlock.** Tape capture writes from inside `hydra_agent.py`. If the SQLite write blocks (rare but possible on Windows fsync), the agent's main loop must not stall. Tape writes go through a bounded queue + dedicated writer thread; on queue full, candles are dropped and logged (live trading priority over historical fidelity).

---

**End of design.** Pending user review → writing-plans skill for implementation plan.
