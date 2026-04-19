# CLAUDE.md — Agent Instructions for HYDRA

> **HARD REQUIREMENT.** Update this file in the same change as: module
> add/remove/rename/split, launcher add/remove, version-bump site change,
> new env flag or kill switch, state-file ownership change, safety
> invariant change, CI gate change. If not possible in the same commit,
> leave `TODO(claude-md):` in code AND a matching `<!-- TODO(claude-md): -->`
> here. Stale CLAUDE.md = CI failure waiting to happen.
>
> **Cold subsystem detail lives in CBP, not here.** When you touch a
> subsystem, load its node first:
> `python C:/Users/elamj/Dev/cbp-runner/bin/memory-read.py --label <slug>`.
> This file is the hot index — pointers, rules, and cross-cutting
> invariants only. Point, don't duplicate.

## Operating Rules (binding, non-negotiable)

Each was earned through a documented past failure. Violating one is a
regression bug, not a style issue.

1. **Parallel Task agents for any audit > 20 files.** Use N parallel
   agents on `audit.partition` (default 7-way). Each returns HIGH/MED/LOW;
   then synthesize. Scale to 10+ if file count justifies.
2. **Stop processes before editing their state.** A live writer overwrites
   your edit on its next tick. Check ownership in `state_files`; stop
   owner, edit, verify persisted, restart. Snapshot + journal must stay
   in sync — clean both together.
3. **Verify claims with actual commands.** "Verified", "passing", "fixed"
   require running the verification (`pytest`, `git tag -v`, etc.) in the
   same turn and pasting the output. No claims without evidence.
4. **Two-phase self-audit on new code.** After writing, audit for unused
   imports, dead code, unhandled exceptions, null/empty crashes,
   deprecated APIs, misleading errors, false-positive checks. Fix all,
   then a second pass. Only then declare done.
5. **Enumerate all version-bump locations upfront.** Before bumping to
   X.Y.Z, run `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` and confirm every
   site in `version_sites`. Update all in one commit.

## Project

- **HYDRA** — regime-adaptive crypto trading agent for Kraken. Detects
  regime (trending/ranging/volatile), switches between 4 strategies
  (Momentum, MeanReversion, Grid, Defensive), executes limit post-only.
- **Pairs:** SOL/USDC, SOL/BTC, BTC/USDC
- **Version pin:** v2.13.7

## Defaults (inherited)

- Engine: Python stdlib only (no numpy/pandas in engine)
- Orders: limit post-only (`--type limit --oflags post`). Never market.
- Engine isolation: one HydraEngine per pair, no shared state
- Kraken CLI: `wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken ..."`
  (verify distro is exactly `Ubuntu` via `wsl -l -v`)
- Kraken REST min interval: **2s** between calls
- min_confidence: 0.65 (both modes); warmup_candles: 50
- Circuit breaker: **15% drawdown halts engine for session** (permanent)
- WS dashboard port: 8765; Vite dev: 5173 (assumed)
- CI authority: `.github/workflows/ci.yml` (jobs: `engine-tests`,
  `dashboard-build`)

## Cross-cutting invariants (HIGH severity if violated)

- **Limit post-only, never market** — deliberate design choice
- **2s REST floor** — Kraken throttles or bans below this
- **15% drawdown kills engine for session** — both `tick()` and `_maybe_execute` check
- **RSI/ATR = Wilder exponential smoothing, NOT SMA** (Bollinger = population variance)
- **Ledger shield floor = 0.20 BTC** — cannot be lowered via API, ever
- **SKIP ≠ BLOCK** — posture restriction SKIPs, BLOCK reserved for hard rules
- **`HYDRA_COMPANION_LIVE_EXECUTION` default OFF** — proposals are paper until opted in

Subsystem detail (indicators, regime, Kelly sizing, price precision,
execution stream lifecycle, resume reconciliation, forex modifier,
shutdown) → `cbp --label hydra.engine_invariants` + `hydra.trading_invariants`.

## Modules (thin index — details in deep specs / CBP)

| id | file | role |
|---|---|---|
| engine | `hydra_engine.py` | indicators, regime detection, signals, position sizing |
| agent | `hydra_agent.py` | live agent: Kraken CLI via WSL, WS broadcast, execution, reconciler, snapshot + `--resume` |
| brain | `hydra_brain.py` | 3-agent AI: Claude Analyst + Risk Manager + Grok Strategist |
| tuner | `hydra_tuner.py` | self-tuning params; `apply_external_param_update` + `rollback_to_previous` (depth=1 deque) |
| companions | `hydra_companions/` | chat/proposals/nudges/ladder/live executor/CBP client/souls |
| backtest | `hydra_backtest.py` | replay engine; reuses HydraEngine verbatim; `HYDRA_VERSION` lives here |
| backtest_metrics | `hydra_backtest_metrics.py` | bootstrap CI, walk-forward, Monte Carlo, regime P&L, sensitivity |
| backtest_server | `hydra_backtest_server.py` | `BacktestWorkerPool` (max=2 daemon, queue=20) + WS via `mount_backtest_routes` |
| backtest_tool | `hydra_backtest_tool.py` | 8 Anthropic tool schemas + dispatcher + `QuotaTracker` (10/d caller, 3 concurrent, 50/d global) |
| experiments | `hydra_experiments.py` | `Experiment` + `ExperimentStore` (RLock); 8 presets; sweep/compare |
| reviewer | `hydra_reviewer.py` | AI Reviewer; 7 code-enforced rigor gates; PR-draft only |
| shadow_validator | `hydra_shadow_validator.py` | single-slot FIFO live-parallel validation before param writes |
| thesis | `hydra_thesis.py` | `ThesisTracker`, `Ladder`, `IntentPrompt`, `Evidence` |
| thesis_processor | `hydra_thesis_processor.py` | daemon: research → `ProposedThesisUpdate` awaiting human approval (Grok 4) |
| journal_maintenance | `journal_maintenance.py` | order journal compaction/rotation |
| journal_migrator | `hydra_journal_migrator.py` | one-shot legacy journal migration (auto on first start) |
| dashboard | `dashboard/src/App.jsx` | single-file React, inline styles; tabs LIVE/BACKTEST/COMPARE/THESIS |

## Deep specs

- `SKILL.md` — full trading specification (agent-readable)
- `AUDIT.md` — technical audit + verification checklist
- `CHANGELOG.md` — version history
- `HYDRA_MEMORY.md` — memory wiring + CBP sidecar topology
- `SECURITY.md` — security policy
- `docs/BACKTEST.md` / `docs/BACKTEST_SPEC.md` — runbook + authoritative design
- `docs/COMPANION_SPEC.md` — companion spec (authoritative)
- `docs/THESIS_SPEC.md` / `docs/THESIS.md` — design spec + runbook

## CBP pointers (load on demand, one node per subsystem)

Relational graph (edges: `causes` / `requires` / `contradicts` /
`qualifies`) lives in CBP — 272+ nodes tracked there, not duplicated
here. Session-start header surfaces top weighted nodes.

```
cbp --label hydra.engine_invariants     # indicators + regime + adaptive volatility
cbp --label hydra.trading_invariants    # sizing, minimums, precision, exec, resume, forex
cbp --label hydra.ai_brain              # Analyst/RM/Strategist + tool-use loop
cbp --label hydra.streams               # BaseStream + 5 instances
cbp --label hydra.thesis_layer          # posture/ladder/intent/doc-processor
cbp --label hydra.backtest_platform     # I1–I12, rigor gates, reviewer, dashboard
cbp --label hydra.companion_subsystem   # orb default ON, live-exec opt-in
cbp --label hydra.tests_live_harness    # 33+ scenarios, smoke/mock/validate/live modes
```

Discovery: `python C:/Users/elamj/Dev/cbp-runner/bin/memory-read.py --tag group:hydra_spec`
for the whole spec set, or `--label hydra.<slug>` for one node.
Persist new learnings: `python C:/Users/elamj/Dev/cbp-runner/bin/memory-write.py --label <slug> --summary <text> --tag ...`

## Claude Code tooling

- **Skills:** `/release` (release SOP), `/audit` (zero-skip review), `/review`, `/security-review`
- **Post-edit hook:** `.claude/hooks/post-edit.sh` — path-scoped verification; advisory; silence with `HYDRA_POSTEDIT_HOOK_DISABLED=1`
- **Settings split:** per-user `.claude/settings.local.json` + runtime `.claude/scheduled_tasks.lock` gitignored; everything else under `.claude/` committed
- **gitattributes pin:** `*.sh text eol=lf` — prevents Windows core.autocrlf CRLF-ing hook shebang

## State files

| id | path | ownership / notes |
|---|---|---|
| snapshot | `hydra_session_snapshot.json` | atomic `.tmp → os.replace`; `--resume` target; embeds `thesis_state` |
| order_journal | `hydra_order_journal.json` | snapshots immediately on any tick that appends (crash cannot lose since last successful tick); gitignored |
| params | `hydra_params_<pair>.json` | per-pair learned tuning params; gitignored |
| errors_log | `hydra_errors.log` | tick try/except writes here with full traceback; loop continues |
| thesis_state | `hydra_thesis.json` | atomic `.tmp → os.replace`; `THESIS_SCHEMA_VERSION` bumps independently; lazy subdirs `hydra_thesis_{documents,processed,pending,evidence_archive}/`; gitignored |
| cbp_sidecar_state | `cbp-runner/state/` | owner `cbp-runner/supervisor.py`; kill via `CBP_SIDECAR_ENABLED=0` or `state/_disabled` flag; Hydra falls through to JSONL — never blocks |
| experiments_store | `.hydra-experiments/` | owner `experiments`; `presets.json` + `reviewer_config.json` bootstrap from code on first init (delete to regenerate); `shadow_outcomes.jsonl` append-only |

CBP sidecar: auto-launched by `start_hydra.bat` / `start_all.bat` via
`python %CBP_RUNNER_DIR%\supervisor.py --detach` (default
`C:\Users\elamj\Dev\cbp-runner`; override `CBP_RUNNER_DIR`). Client:
`hydra_companions.cbp_client.CbpClient` reads `state/ready.json` on
every call (tokens rotate).

## Env flags (kill switches + opt-ins)

| flag | scope | effect |
|---|---|---|
| `HYDRA_THESIS_DISABLED` | thesis | full kill; tracker returns inert; `save()` no-op; drift test enforces v2.12.5 bit-identical |
| `HYDRA_THESIS_PROCESSOR_DISABLED` | thesis | Grok 4 doc processor off; uploads persist but no proposal |
| `HYDRA_THESIS_LADDERS` | thesis | opt in to Ladder primitive (match_rung is no-op without it) |
| `HYDRA_BACKTEST_DISABLED` | backtest | kill; worker pool off, WS rejects backtest msgs; v2.9.x exact |
| `HYDRA_BRAIN_TOOLS_ENABLED` | brain | enables Anthropic tool-use for Analyst+RM (Grok stays text-only) |
| `HYDRA_COMPANION_DISABLED` | companion | kill (no orb) |
| `HYDRA_COMPANION_PROPOSALS_ENABLED` | companion | default on; `=0` for no trade cards |
| `HYDRA_COMPANION_NUDGES` | companion | default on; `=0` for no proactive messages |
| `HYDRA_COMPANION_LIVE_EXECUTION` | companion | **opt-in** real-order execution; **default OFF for money safety** |
| `CBP_SIDECAR_ENABLED` | memory | default on; `=0` falls through to JSONL; also `state/_disabled` flag |
| `CBP_RUNNER_DIR` | memory | override sibling `cbp-runner` checkout location |
| `HYDRA_POSTEDIT_HOOK_DISABLED` | tooling | silence hook during heavy refactors |

## Build / run

- Dashboard dev: `cd dashboard && npm install && npm run dev`
- Agent default: `python hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC --balance 100`
- Agent competition: `python hydra_agent.py --mode competition`
- Agent paper: `python hydra_agent.py --mode competition --paper`
- Agent resume: `python hydra_agent.py --mode competition --resume`
- Engine demo (no keys): `python hydra_engine.py`

**Launchers:**
- `start_hydra.bat` — production watchdog (`--mode competition --resume` — **do not remove these flags**)
- `start_all.bat` — full stack: CBP sidecar + agent + dashboard
- `start_dashboard.bat` — dashboard only
- `start_hydra_companion.bat` — paper-mode companion testing (no real money)

## Version sites (Rule 5: update ALL in one commit)

1. `CHANGELOG.md` — new `## [X.Y.Z]` section header
2. `dashboard/package.json` — `"version"` field
3. `dashboard/package-lock.json` — **both** `"version"` fields (root + `""` package)
4. `dashboard/src/App.jsx` — footer string `HYDRA vX.Y.Z`
5. `hydra_agent.py` — `_export_competition_results()` → `"version"` field
6. `hydra_backtest.py` — `HYDRA_VERSION = "X.Y.Z"` (stamps every `BacktestResult`)
7. Git tag — `git tag -s vX.Y.Z -m "vX.Y.Z"` after merge; verify `git tag -v vX.Y.Z` (Rule 3)

**Policy:** MINOR only for material upgrades; bug fixes / doc tweaks = PATCH.

## Release PR workflow

- **Cycle:** branch → tests pass → PR → CI green → merge → signed tag
- **Tests pass:** both CI jobs green (`engine-tests` + `dashboard-build`). Mock harness (`tests/live_harness/harness.py --mode mock`) **MANDATORY** for any PR touching execution path.
- **Enumerate first:** `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` before bumping (Rule 5)
- **Tag:** signed; verify (Rule 3)
- **Automation:** `/release` skill codifies the cycle. Never merge with red or pending CI.

Tests: `python -m pytest tests/` or individual `python tests/test_*.py`
(CI pattern). Live harness detail → `cbp --label hydra.tests_live_harness`.

## Audit

**7-way partition** for Rule 1:

| id | scope |
|---|---|
| p1_engine_tuner | engine, tuner |
| p2_agent_streams | agent, streams |
| p3_ai_layer | brain, reviewer, shadow_validator |
| p4_backtest | backtest, backtest_metrics, backtest_server, backtest_tool, experiments |
| p5_companion | companions |
| p6_dashboard | dashboard |
| p7_tests | `tests/`, `tests/live_harness/` |

**HIGH severity:** violations of backtest I1–I12, limit-post-only, 2s
rate-limit floor, 15% circuit breaker, Wilder-EMA RSI/ATR spec, or
`HYDRA_COMPANION_LIVE_EXECUTION` default-off.

**Two-phase protocol (Rule 4):** after fixing HIGH/MED, re-run partition
sweep against your diff, then full tests + `harness.py --mode mock`;
declare done only when phase 2 is clean. Drive full cycle via `/audit`.

## Windows / WSL gotchas

- Use UTF-8 explicitly; cp1252 crashes on Unicode (dashboard regime emoji + console portfolio block share the theme — both crash on cp1252)
- `time.time()` has ~15ms Windows resolution; in BaseStream heartbeat or `RESTART_COOLDOWN_S=30s` it silently miscounts — use `time.perf_counter()`
- Escape parentheses in `.bat` files inside if-blocks; `start_hydra.bat` + `start_all.bat` use nested if around `--resume` and CBP sidecar launch — cmd parser drops branches silently
- WSL: if distro is `Ubuntu-22.04` instead of `Ubuntu`, `kraken` invocation silently routes nowhere — verify `wsl -l -v`
- Vite dev server falls off :5173 to next free port if taken; dashboard WS proxy assumes :5173 — verify bound port in Vite startup log

## Common pitfalls

- Don't add `import numpy` or `import pandas` to the engine — intentionally pure Python
- Don't change orders to market type — limit post-only is deliberate
- Don't reduce rate limiting below 2s — Kraken throttles/bans
- Don't merge engine instances across pairs — they must remain independent
- `.env` contains Kraken API keys — never commit
- On shutdown agent cancels all resting limit orders and flushes snapshot — do not bypass
- `start_hydra.bat` uses `--mode competition --resume` for production — do not remove
- **FEATURE GAP:** `CrossPairCoordinator` Rule 2 (BTC recovery BUY boost) + Rule 3 (coordinated swap SELL) can conflict when BTC TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP — Rule 3 overwrites Rule 2 (favors safer SELL); future: explicit priority or merge logic
- Companion live execution opt-in: `HYDRA_COMPANION_LIVE_EXECUTION=1`; confirm unset before live debugging
- CBP sidecar failures silent by design (falls through to JSONL); if memory writes vanish, check `cbp-runner/state/ready.json` exists and `state/_disabled` does NOT
- `kraken-cli` is an external WSL Ubuntu dep (`source ~/.cargo/env && kraken`); check dashboard footer pinned version before debugging `--validate` schema errors
