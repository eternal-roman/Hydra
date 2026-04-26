# Research Tab Redesign — Canonical Build Log

> **Living document.** Every decision and build step from the brainstorm through implementation, with commit SHAs, test counts, audit findings, and plan-divergences. Designed for after-the-fact review with questions.

**Branch:** `feature/research-tab-redesign`
**Branch base:** `b4b259a` on `main`
**Spec:** [`docs/superpowers/specs/2026-04-26-research-tab-redesign-design.md`](../specs/2026-04-26-research-tab-redesign-design.md)
**Plan:** [`docs/superpowers/plans/2026-04-26-research-tab-redesign.md`](../plans/2026-04-26-research-tab-redesign.md)
**Target version:** v2.20.0 (MINOR)

---

## Table of Contents

1. [Brainstorming decisions](#1-brainstorming-decisions)
2. [Plan structure](#2-plan-structure)
3. [Phase 1 — Canonical store + bootstrap (T1-T6)](#phase-1--canonical-store--bootstrap-t1-t6)
4. [Phase 1 checkpoint (verification data)](#phase-1-checkpoint-verification-data)
5. [Phase 2 — Refresh + tape capture (T7-T10)](#phase-2--refresh--tape-capture-t7-t10)
6. [Phase 3 — Walk-forward + Wilcoxon (T11-T14)](#phase-3--walk-forward--wilcoxon-t11-t14)
7. [Phase 3 checkpoint (Wilcoxon hand-check)](#phase-3-checkpoint-wilcoxon-hand-check)
8. [Phase 4 — Backtest SqliteSource + regression runner (T15-T18)](#phase-4--backtest-sqlitesource--regression-runner-t15-t18)
9. [Phase 4 checkpoint (regression snapshot row-set)](#phase-4-checkpoint-regression-snapshot-row-set)
10. [Phase 5 — Dashboard Research tab (T19-T23)](#phase-5--dashboard-research-tab-t19-t23-in-progress)
11. [Phase 6 — Release (T24-T28)](#phase-6--release-t24-t28-pending)
12. [Cross-phase decisions and divergences from the plan](#cross-phase-decisions-and-divergences-from-the-plan)
13. [Open questions / deferred items](#open-questions--deferred-items)

---

## 1. Brainstorming Decisions

The redesign emerged from two specific user complaints about the existing Research tab:
1. Default `data_source = "synthetic"` — backtests on per-pair seeded random walks with no exploitable structure. Strategy "edge" detected on synthetic data is meaningless.
2. Comparing `seed=42` vs `seed=15` is comparing two unrelated synthetic universes; divergence dominates signal.

These framed the four major decisions of the redesign:

### 1.1 Purpose: B + C layered, C carries operational weight

User chose **B (Hypothesis Lab) + C (Release Regression Harness)**, with explicit framing that "where the most value is added is C." Mode B is a future-friendly capability; Mode C is the bedrock.

| Mode | Question it answers | Output |
|---|---|---|
| **B — Hypothesis Lab** | "Does raising `momentum_rsi_upper` to 75 in TREND_UP have edge?" | Walk-forward fold table + Wilcoxon verdict + equity-curve overlay |
| **C — Release Regression** | "Did v2.20 change edge vs v2.19?" | Frozen snapshot artifact diffed against prior version, gated into `/release` |

**Trade-off accepted:** Both modes share the same fold engine (single source of truth for methodology), even though Mode C is the priority. This pays off if Mode B ever ships a working param-injection layer; until then the Lab pane is a placeholder.

### 1.2 Data: Kraken trade archive → SQLite, deep history, daily-maintained

Locked in:
- **Source:** Kraken's published trade-level CSV archive (the `Kraken_Trading_History.zip` file the user has at `C:\Users\elamj\Downloads\`). This is the canonical path for deep Kraken history. Kraken's public REST `OHLC` endpoint only returns ~720 candles regardless of `since`, so it cannot serve as the bootstrap source.
- **Storage:** SQLite (`hydra_history.sqlite`), stdlib-only (matches Hydra's no-deps engine rule). Single canonical schema with `(pair, grain_sec, ts)` PK, indexed by source-tier policy.
- **Granularity:** 1h candles, deliverable to 2012 in ambition. **Reality:** BTC/USD ~2013-09 (Kraken launch); SOL/USD + SOL/BTC ~2021-08 (SOL listing). 12 years of BTC, 4.5 years of SOL — sufficient for multi-regime back-testing.
- **Maintenance:** Daily REST refresh (`tools/refresh_history.py`) + live tape capture (`hydra_tape_capture.py`) writing closed candles in real time as they emit from `CandleStream`.

**Rejected alternatives:** External provider (CryptoCompare/Binance) — would introduce non-Kraken price series for a Kraken-only bot; basis risk in interpretation. Live-tape-only — can't backtest history we don't have.

### 1.3 Methodology: Anchored walk-forward + paired Wilcoxon, OOS-only scoring

The original complaint about "seed=42 vs seed=15 dramatically different outcomes" survives in a new form once history is real: a single full-history replay is still one realized trajectory, and a parameter that "wins" on it might just be lucky on that specific tape. The fix is **anchored walk-forward with explicit IS/OOS split per fold + paired Wilcoxon on OOS metrics across folds.**

Why this specific methodology over alternatives:

| Method | Predictive power vs live | Why chosen / rejected |
|---|---|---|
| Single full-history replay | Weak — same data informs param choice and scores it | Rejected: in-sample optimism |
| Walk-forward, no IS/OOS split | Medium — paired test removes single-trajectory luck but params still touch test data | Stepping stone, not enough |
| **Walk-forward + anchored IS/OOS + paired Wilcoxon (CHOSEN)** | **Strong — closest analog to "what would this param have done if shipped at the start of each quarter"** | Closes the OOS gap directly; minimum stat power achievable |
| + Block-bootstrap CIs | Strong, only marginally better | Deferred — improves uncertainty *reporting* not OOS-vs-live correspondence |

**Fold size:** Quarterly (Jan-Mar / Apr-Jun / etc). Long enough for regime variety; short enough for ~48 BTC and ~16 SOL paired observations. Anchored IS = all history strictly before the OOS window, capped at 8 quarters (rolling).

**Statistical test:** Paired Wilcoxon signed-rank, two-sided, α=0.05. Stdlib-only (no scipy at runtime).

**Important honest disclosure**: with n=5 paired observations even an all-positive delta produces p=0.0625 — *just above* the threshold. n=6+ is the minimum where the test can ever fire BETTER. This is fine for Hydra's data (~48 BTC folds, ~16 SOL folds) but worth knowing — was surfaced to the user in the Phase 3 checkpoint.

### 1.4 Snapshot shape: Option C (headline + regime + equity curve + trade log)

Each tagged Hydra version produces a frozen "regression snapshot." When a future version runs, it diffs against the prior snapshot.

Trade-offs reviewed at brainstorm time:

| Snapshot option | Catches release regressions? | Storage cost |
|---|---|---|
| A. Headline metrics only | Coarse — misses regressions at fee level or regime-conditional | Tiny |
| B. Headline + per-regime breakdown | Most regressions live in regime-conditional shifts | Small |
| **C. + equity curve & trade log (CHOSEN)** | **Visual equity overlay + line-by-line trade comparison** | Bounded; one curve per release × pair |
| D. Full deterministic replay receipt | Forensic line-by-line decision diff | Most invasive — requires brain stubbing |

User chose C with D opt-in deferred. Brain stubbing required even for C (LLM calls = non-determinism + cost) — handled by `brain_mode="stub"` in `BacktestConfig`.

### 1.5 Storage: One SQLite DB, regression and ohlc tables co-located

Decided at design time: regression snapshot tables live in the same `hydra_history.sqlite` as `ohlc`. Rationale: "one DB, one truth" simplifies the Research tab — `DATASET` pane and `RELEASES` pane read from the same store. New tables: `regression_run`, `regression_metrics`, `regression_equity_curve`, `regression_trade`.

### 1.6 Mode C runs at release time, gated; not on every PR

Decision: the regression harness runs as a step inside the `/release` skill (after tests, before tag). On any `Wilcoxon WORSE p<0.05` outcome, the release is blocked unless `--accept-regression "<reason>"` is passed (the reason persists into `regression_run.override_reason` for audit). CI does *not* run the harness per-PR — too slow + Kraken-archive-dependent.

---

## 2. Plan structure

The plan splits into 6 phases with explicit STOP-checkpoints after Phases 1, 3, 4, 5, 6:

| Phase | RC | Tasks | Done criterion |
|---|---|---|---|
| 1 | rc1 | T1-T6 | `hydra_history.sqlite` exists with 3 pairs at 1h |
| 2 | rc1 | T7-T10 | Daily refresh + tape capture wired into agent under `HYDRA_TAPE_CAPTURE` |
| 3 | rc1 | T11-T14 | Wilcoxon + walk-forward kernel, scipy-cross-checked |
| 4 | rc1 | T15-T18 | `tools/run_regression.py --version 2.19.1` produces a snapshot row set |
| 5 | rc2 | T19-T23 | `DATASET` / `LAB` / `RELEASES` panes render real data |
| 6 | rc3 | T24-T28 | `/release` regression-gated; v2.20.0 shipped with signed tag + GH release |

Plan tasks are TDD-style (failing test first, then impl, then commit) per the writing-plans skill.

---

## Phase 1 — Canonical store + bootstrap (T1-T6)

### T1: HistoryStore skeleton + schema (`9b18e66` → `e880f7b`)

**Goal:** create `hydra_history_store.py` with `SCHEMA_VERSION=1`, `meta` + `ohlc` tables, and a `_conn()` contextmanager that sets WAL+NORMAL pragmas.

Implementation went TDD-clean:
- 2 tests written and verified-failing
- ~80-line module created
- Tests pass (2/2)

**Code review found:** the explicit `CREATE INDEX ix_ohlc_pair_grain_ts ON ohlc(pair, grain_sec, ts)` was redundant with the PK auto-index `sqlite_autoindex_ohlc_1` — same columns, same order. Costs disk + write amplification on every upsert. Originated in the spec itself. **Fixed by `e880f7b`** (removed from code, spec, and plan together).

**Verdict:** clean, plan-aligned after fix. SCHEMA_VERSION = 1.

### T2: HistoryStore upsert + fetch + tier policy (`2d03793` → `5a2bd2f`)

**Goal:** module-scope `_SOURCE_RANK = {"tape": 1, "kraken_rest": 2, "kraken_archive": 3}`, frozen `CandleRow`/`CandleOut` dataclasses, tier-aware `upsert_candles()` that skips lower-tier writes against existing higher-tier rows, `fetch()` that yields `CandleOut` ordered by ts.

**Code review found CRITICAL:** the `fetch()` generator yielded inside a `with self._conn() as conn:` block. Because generators suspend at each `yield`, the contextmanager exits at the FIRST yield and closes the connection — subsequent yields read from a closed connection. Worked only because CPython's `sqlite3.Cursor` happens to buffer rows; not portable, fragile under PyPy or any future `fetchmany()` refactor.

**Fix `5a2bd2f`:** materialize via `.fetchall()` inside the `with` block, then `yield from (CandleOut(*row) for row in rows)` outside it.

Reviewer also flagged the N+1 upsert pattern (one SELECT + one INSERT per row) — accepted as MVP per the plan; bootstrap timed at ~3 minutes total for 175k rows, fast enough.

**Verdict:** 5 tests pass. Tier-policy invariant validated by tests.

### T3: HistoryStore coverage + gap detection (`5beebf8` → `a419f7e`)

**Goal:** `Coverage` dataclass + `coverage()` method that detects gaps (ts delta > grain_sec) and reports max gap; `list_pairs()` returns sorted `(pair, grain_sec)` tuples.

Clean TDD pass. Minor PEP-8 fix applied (`a419f7e`) — the test file had a mid-file import that I consolidated to the top.

**Verdict:** 7 tests pass.

### T4: Schema migration scaffolding (`249cd8c`)

**Goal:** explicit `RuntimeError` if `meta.schema_version` on disk != `SCHEMA_VERSION` in code, with a clear "delete the DB to rebuild" message. Until v2 ships, mismatches are strict failures.

`try/except sqlite3.OperationalError` handles the fresh-DB case (no `meta` table yet).

**Verdict:** 8 tests pass.

### T5: Bootstrap from Kraken trade archive (`9bb144b` → `77bc6b0`)

**Goal:** `tools/bootstrap_history.py` — stream-read each pair's trade CSV, roll trades into 1h candles per `(pair, grain_sec)` bucket, never load all trades into RAM.

**Plan-divergence (caught by implementer):** the test fixture timestamps in the plan spanned 3 hour-buckets, not 2 — the expected OHLCV values in the assertions didn't match the actual rollup math. Implementer detected the inconsistency, fixed the fixture timestamps to be hour-aligned (`1_699_999_200` base), preserving design intent. Filed inline as a fix in the test file; no impact on the production module.

**Verdict:** 2 new tests pass.

### T6: Real bootstrap end-to-end + gitignore (`77bc6b0` for cp1252 fix → `71965a9` for gitignore)

**Goal:** run the actual bootstrap on the user's `Kraken_Trading_History.zip` and verify three pairs persist.

**Hit:** Windows cp1252 console can't encode `→` (the unicode arrow in the bootstrap's progress print). CLAUDE.md flags this exact gotcha. **Fix `77bc6b0`:** replaced `→` with `->` in the print statement.

After fix, bootstrap completed:
```
SOL/USD: 39,743 candles in 36.1s   (2021-06-17 → 2025-12-31)
BTC/USD: 96,382 candles in 122.7s  (2013-10-06 → 2025-12-31)
SOL/BTC: 39,679 candles in   5.8s  (2021-06-17 → 2025-12-31)
DB size: 20 MB
```

**Note:** archive cuts at 2025-12-31 (its January-2026 publication date). Trailing 4 months of 2026 will fill via `tools/refresh_history.py` (T7) + ongoing tape capture (T9-T10).

`hydra_history.sqlite`, `-shm`, `-wal` added to `.gitignore`.

**Verdict:** Phase 1 complete. 175,804 ohlc rows persisted.

---

## Phase 1 Checkpoint (verification data)

User reviewed and approved at this checkpoint. Key confirmations:
- BTC/USD goes back to 2013-10-06 (Kraken's actual launch — design said "2013-09," 1-month spread is harmless)
- SOL pairs go back to 2021-06-17 (SOL listing on Kraken)
- All three pairs end at 2025-12-31 (archive cut date)

---

## Phase 2 — Refresh + tape capture (T7-T10)

### T7: REST refresh tool (`dec9f41`)

**Goal:** `tools/refresh_history.py` — daily catch-up via `KrakenCLI.ohlc()`. Tier-policy preserves `kraken_archive` rows; `kraken_rest` writes can refresh the trailing edge.

Plan included an unused `_registry_to_kraken_pair` helper — dropped per the plan's own note. Stdlib-only, injectable CLI for tests.

**Verdict:** 2 tests pass.

### T8: CandleStream `on_candle` callback hook (`cc29869`)

**Goal:** add `on_candle(callback)` to `hydra_streams.py:CandleStream` so subscribers can receive every closed candle. Callbacks are dispatched OUTSIDE the lock to avoid deadlock on re-entry, with try/except around each so a buggy subscriber can't kill the WS thread.

**Verdict:** 1 new test + 141 existing stream tests still green.

### T9: Tape capture writer (`e435441`)

**Goal:** `hydra_tape_capture.py` — bounded `queue.Queue` + dedicated daemon writer thread. Critical invariant: agent's main loop must NEVER stall on a SQLite fsync. On queue full, candles are dropped and counted; live trading priority over historical fidelity.

`_parse_iso_to_ts` tolerantly parses Kraken WS v2's `interval_begin` ISO 8601 timestamps. `on_candle` is non-blocking (returns immediately on `queue.Full`).

**Verdict:** 2 tests pass.

### T10: Wire tape capture into agent (`ecd900b`)

**Goal:** in `hydra_agent.py`, gate tape capture under `HYDRA_TAPE_CAPTURE=1` (default ON). Lazy-import `HistoryStore` + `TapeCapture` so the agent doesn't depend on the SQLite DB being present when capture is disabled. Stop tape capture last on shutdown so no candles arrive mid-teardown.

**Mock harness 35/35 passed.** CLAUDE.md env-flags table updated with three new rows: `HYDRA_TAPE_CAPTURE`, `HYDRA_HISTORY_DB`, `HYDRA_REGRESSION_GATE`.

**Verdict:** Phase 2 complete. Agent now writes live closed candles into the canonical store.

---

## Phase 3 — Walk-forward + Wilcoxon (T11-T14)

### T11: Wilcoxon signed-rank stdlib (`6694b3c`)

**Goal:** exact two-sided Wilcoxon for n≤25 (enumerate all 2^n sign permutations), normal approximation with continuity correction for larger n. Tied-rank averaging. Stdlib only.

**Implementation note:** self-audit caught `sorted(nonzero)` being computed twice inline; consolidated to a single `sorted_nonzero` variable.

**Scipy cross-check:** `scipy.stats.wilcoxon([1,2,3,4,5], mode='exact').pvalue == 0.0625` — matches our implementation exactly.

**Verdict:** 3 tests pass.

### T12: Quarterly fold construction (`4fc10f3`)

**Goal:** `build_quarterly_folds` produces anchored IS/OOS folds — IS = all boundaries before OOS window, capped at `is_lookback_quarters` (default 8). `_quarter_starts_between` rounds up to the next quarter start if `start_ts` is mid-quarter.

For `[2022-01-01, 2023-01-01]` with default lookback: 5 quarter boundaries → 3 folds.

**Verdict:** 2 new tests + 5 total walk-forward tests pass.

### T13: Walk-forward runner (`176ee1e`)

**Goal:** `run_walk_forward(pair, history_start_ts, history_end_ts, baseline_params, candidate_params, spec, runner)` — for each fold, invoke `runner` for baseline + candidate, compute deltas (with sign flip for `max_dd_pct` since lower DD is better), compute Wilcoxon over the deltas per metric.

Self-audit: `Optional` was added per plan note but unused — removed before commit.

**Verdict:** 6 walk-forward tests pass.

### T14: Phase 3 audit checkpoint (no commit)

Re-ran full walk-forward suite (6/6 in 0.15s). Two-phase audit:
- No unused imports
- No dead code
- Sign convention on `max_dd_pct` correctly flipped
- Empty-folds edge case handled (`wilcoxon_signed_rank([])` → `equivocal` early-exit)
- `_HEADLINE_METRICS` matches the four delta keys

No new commit needed.

---

## Phase 3 Checkpoint (Wilcoxon hand-check)

Verified the implementation against a hand-computed example:
- Deltas `[1, 2, 3, 4, 5]` → ranks `[1,2,3,4,5]`, all positive → W+=15, W-=0, W=0
- n=5, exact distribution: 1/32 sign permutations have W- ≤ 0 (the all-positive one)
- One-sided p = 1/32 = 0.03125; two-sided p = 0.0625
- α=0.05 → equivocal (just barely; n=5 is the minimum where two-sided exact significance is reachable)

User informed of the n≥6 minimum to fire BETTER/WORSE verdicts.

**For Hydra's actual data:** BTC/USD ~48 quarterly folds (well above threshold), SOL/USD + SOL/BTC ~16 quarterly folds each (also above threshold).

User approved continuing.

---

## Phase 4 — Backtest SqliteSource + regression runner (T15-T18)

### T15: SqliteSource for backtest (`7b5b7c7`)

**Goal:** add `SqliteSource(db_path, grain_sec, start_ts, end_ts)` as a new `CandleSource` subclass, plus a `"sqlite"` branch in `make_candle_source` factory. Default still `"synthetic"` for back-compat at this point.

**Verdict:** 2 new tests + 179 existing backtest tests still green.

### T16: Brain-stub mode in backtest (`e5b3ef3`)

**Important context discovered:** `hydra_backtest.py` does NOT currently invoke the AI brain anywhere. `BacktestRunner` has no `_brain` attribute or `decide()` call. Comment at line 758 confirms it's a future integration point. So T16 reduces to:
1. Add `brain_mode: str = "stub"` field to `BacktestConfig`
2. Add `_validate_brain_mode()` to `BacktestRunner` raising `NotImplementedError` for `replay` / `live`
3. Add inert `_stub_brain_decision()` helper as the public contract for the future wire-up
4. No "wrap brain-call sites" — there are none yet

Implementer caught a `cfg` vs `config` attribute name issue from the plan template (`BacktestRunner` uses `self.config`, not `self.cfg`). Fixed inline.

**Verdict:** 5 backtest_sqlite_source tests + 182 backtest tests pass.

### T17: Regression-snapshot tables + writer (`7e9fe24`)

**Goal:** bump `SCHEMA_VERSION = 1 → 2`, append four `regression_*` tables to `_SCHEMA`, allow 1→2 forward migration silently (additive change), bump `meta.schema_version` row idempotently with `INSERT OR REPLACE`. Create `tools/run_regression.py` with `persist_regression_run()` (single-transaction insert of run + metrics + curve + trade rows).

**Live DB upgrade (T17 step 6):**
```
schema_version: 2
tables: meta, ohlc, regression_equity_curve, regression_metrics, regression_run, regression_trade
ohlc rows: 175804  (preserved unchanged from T6 bootstrap)
```

**Verdict:** 10 tests pass (history_store + migrations + regression_runner).

### T18: First end-to-end regression run (`63878cf` for audit cleanup)

**Goal:** run `python -m tools.run_regression --version 2.19.1` and verify a snapshot row-set is produced.

**Run output:**
```
SOL/USD : 23:11:57  (started)
SOL/BTC : 23:17:57  (6 min after SOL/USD)
BTC/USD : 23:28:47  (11 min after SOL/BTC; 17 min wall-clock total)
```

**All Wilcoxon verdicts: `equivocal (p=1.0, wins=0/0)`** — every fold was *skipped* because `n_trades < min_oos_trades=5`. Expected MVP placeholder behavior:
1. `_runner_from_backtest` passes `is_baseline=True` for both sides — `is_baseline` isn't a real engine param, so both produce identical metrics.
2. The Hydra engine on raw 1h candles trades sparingly per quarter (warmup=50, conservative gates).

**Persisted rows:**
- 3 `regression_run` rows (one per pair)
- 12 `regression_metrics` aggregate rows (Wilcoxon p-values per metric × pair)
- 0 per-fold metric rows (all skipped)

**Audit (Rule 4 two-phase):**
- HIGH/MED: none
- LOW: 1 unused `List` import in `tools/run_regression.py` — fixed in `63878cf`
- INFO: `_validate_brain_mode` declared but not yet called from `BacktestRunner.run()` — intentional MVP scaffolding for the brain integration step

**Test totals at end of Phase 4:** 28 backend tests passing across 11 test files (T1-T17). 182 existing backtest tests still green.

---

## Phase 4 Checkpoint (regression snapshot row-set)

User reviewed the persisted row-set and the `equivocal/0-fold-skipped` outcome. Key user feedback:
> "That's totally fine, we can revisit if it gets too aggressive."

User approved continuing.

---

## Phase 5 — Dashboard Research tab (T19-T23, IN PROGRESS)

### T19: Backend WS routes (`8b498d8`) — DONE

**Important plan-divergence:** the plan specified async `_handle_*` handlers with `ws.send_json` streaming. The actual codebase pattern in `hydra_backtest_server.py` is **synchronous** handlers via `broadcaster.register_handler("type", fn)` returning dicts. T19 followed the actual codebase pattern.

**Lab Mode B deferred at MVP.** Two reasons:
1. `BacktestRunner` doesn't read arbitrary param overrides per-fold yet — `_runner_from_backtest`'s `is_baseline=True/False` placeholder doesn't actually inject params into the engine.
2. A working walk-forward in-handler would block the WS thread for ~17 min per pair (per T18's measurement).

So T19 ships **3 read-only handlers fully + 1 deferred-error stub for `research_lab_run`:**

| Handler | Purpose | Status |
|---|---|---|
| `research_dataset_coverage` | per-(pair,grain_sec) coverage from `HistoryStore` | DONE |
| `research_releases_list` | list `regression_run` rows with verdict summary | DONE |
| `research_releases_diff` | side-by-side metrics for two `run_id`s | DONE |
| `research_lab_run` | structured "deferred" error so UI can render a clear pending state | STUB |

**Tests:** 12 new tests + 38 existing backtest_server tests still green.

This matches the user's earlier framing — Mode C carries operational priority; Mode B is a friendly future capability.

### T20-T23: Dashboard panes (PENDING)

To be built:
- `dashboard/src/components/research/DatasetPane.jsx` — read-only coverage table with stale-row highlight
- `dashboard/src/components/research/LabPane.jsx` — form + fold table + verdict bar (will gracefully render the deferred-error from `research_lab_run`)
- `dashboard/src/components/research/ReleasesPane.jsx` — versions list + 2-pick diff selector
- `dashboard/src/components/ResearchTab.jsx` — composes the three panes; replace BACKTEST/COMPARE in `App.jsx`

---

## Phase 6 — Release (T24-T28, PENDING)

To be done:
- T24: `/release` skill insert regression-harness step before tag
- T25: Bump version sites to v2.20.0 (7 sites + `SCHEMA_VERSION` already at 2)
- T26: 7-way audit (Rule 1)
- T27: PR + CI green + merge
- T28: Signed tag + GitHub release + alignment script `--check-tag --check-gh-release` exit 0

---

## Cross-phase decisions and divergences from the plan

These are inline departures from the plan-as-written, all captured here for review:

1. **T1 — redundant index removed.** `CREATE INDEX ix_ohlc_pair_grain_ts` was in the spec but redundant with the PK auto-index. Dropped from code, spec, and plan together (`e880f7b`).

2. **T2 — `fetch()` materialization.** Plan's generator design accidentally relied on CPython's cursor row buffering after the connection-context exited. Replaced with `.fetchall()` inside the `with` block + `yield from` outside (`5a2bd2f`).

3. **T5 — fixture timestamps unaligned.** Plan's test fixture had timestamps that spanned 3 hour-buckets, not 2 as the assertions expected. Fixed to use hour-aligned (`1_699_999_200`-base) timestamps.

4. **T5 → T6 — Windows cp1252 console.** Print used `→`, which crashes on Windows console default encoding. Replaced with ASCII `->` (`77bc6b0`). CLAUDE.md flags this exact gotcha.

5. **T16 — `cfg` vs `config` attribute.** Plan template used `self.cfg.brain_mode`; `BacktestRunner` actually uses `self.config`. Implementer corrected inline.

6. **T16 — no brain-call wrap.** `BacktestRunner` doesn't invoke the brain anywhere yet. T16 reduced to: declare the field + validation + helper. The `_stub_brain_decision` helper exists but isn't called — declared as future contract.

7. **T17 — `INSERT OR IGNORE` → `INSERT OR REPLACE` for schema_version.** Required for the version row to actually update on schema upgrade.

8. **T19 — sync handlers, not async streaming.** Plan specified `async def _handle_*` + `ws.send_json`; actual codebase uses sync `broadcaster.register_handler("type", fn)` returning dicts. T19 followed the actual pattern.

9. **T19 — Lab Mode B deferred to a stub.** Two technical blockers (no param injection in `BacktestRunner`; walk-forward run-time would block the WS thread). Releases (Mode C) is fully wired.

---

## Open questions / deferred items

These are real things to revisit:

1. **Mode B param injection.** Today `_runner_from_backtest` passes `is_baseline=True/False` which the engine ignores. To make Mode B work, `BacktestConfig` needs a per-fold parameter override mechanism that actually applies to `HydraEngine`. Plan-of-record: revisit after v2.20.0 ships.

2. **Walk-forward run-time.** Single regression took 17 minutes wall-clock (BTC was 11 of those). This will run again at every release through `/release`. User accepted at the Phase 4 checkpoint as "fine for now, revisit if aggressive." Optimization paths if needed: (a) parallelize fold runs (engine isolation already supports it), (b) reduce default fold count, (c) batched bootstrap-style upserts in `BacktestRunner`'s output write path.

3. **`min_oos_trades` default of 5.** Currently every fold gets skipped on raw v2.19.1 defaults because the engine trades sparingly over a single quarter. May need to lower to 3 or 2 — but the diagnostic value of sparse trading is real (sparse + winning ≠ statistically meaningful).

4. **Brain-call wrap.** When `_validate_brain_mode` actually starts gating real brain calls, `BacktestRunner.run()` needs the `if self.config.brain_mode == "stub": …` wrap around each call site. T-task to be added in a future plan.

5. **Lab Mode B WS streaming.** When Mode B does ship, `research_lab_run` needs progress streaming (per-fold completion events) — not the synchronous-handler pattern used today. Implementation: piggyback on the existing `BacktestWorkerPool` daemon-thread infrastructure (already used for individual backtest jobs).

6. **Snapshot equity curve & trade rows are empty in MVP.** `persist_regression_run` accepts both lists but `tools/run_regression.py` passes `equity_curve=[], trades=[]`. Wiring the full snapshot shape (option C) requires extracting equity/trades from `BacktestRunner.run()`'s result — currently it returns a `BacktestResult` with `metrics` but no curve or trade-list field. This is the "fully implement option C" unfinished work — currently we're operating at option A (headline metrics only) despite agreeing to option C at brainstorm time.

---

## Commit log (Phases 1-5 partial)

```
8b498d8 feat(server): research tab WS routes (dataset, releases, lab-deferred)        T19
63878cf chore(audit): Phase 4 Rule-4 self-audit pass                                   T18 audit
7e9fe24 feat(regression): snapshot tables + run_regression orchestrator                T17
e5b3ef3 feat(backtest): brain_mode='stub' for deterministic regression/lab             T16
7b5b7c7 feat(backtest): SqliteSource (default in v2.20.0)                              T15
176ee1e feat(walk-forward): runner with paired Wilcoxon per metric                     T13
4fc10f3 feat(walk-forward): anchored quarterly fold construction                       T12
6694b3c feat(walk-forward): exact Wilcoxon signed-rank (stdlib)                        T11
ecd900b feat(agent): wire tape capture under HYDRA_TAPE_CAPTURE                        T10
e435441 feat(history): live tape capture writer (bounded queue + worker)               T9
cc29869 feat(streams): CandleStream.on_candle callback hook                            T8
dec9f41 feat(history): REST trailing-window refresh                                    T7
71965a9 chore(history): gitignore canonical store + WAL files                          T6
77bc6b0 fix(bootstrap): use ASCII arrow in console output (cp1252 Windows console)     T5 fix
9bb144b feat(history): bootstrap from Kraken trade archive                             T5
249cd8c feat(history): explicit schema-version mismatch error                          T4
a419f7e style(history): consolidate Coverage import at top of test file                T3 style
5beebf8 feat(history): coverage + gap detection                                        T3
5a2bd2f fix(history): fetch materializes rows before connection closes                 T2 fix
2d03793 feat(history): tier-aware upsert + fetch                                       T2
e880f7b fix(history): drop redundant ix_ohlc_pair_grain_ts (PK auto-index covers it)   T1 fix
9b18e66 feat(history): SQLite store skeleton + schema v1                               T1
```

(Phase base: `f042153` — implementation plan commit; preceded by `10bf6ed` design commit.)

---

*This log will be updated as Phase 5 (T20-T23) and Phase 6 (T24-T28) complete. Last updated: end of T19.*
