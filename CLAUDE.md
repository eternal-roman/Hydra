# CLAUDE.md — Agent Instructions for HYDRA

> **AGENT HARD REQUIREMENT — CLAUDE.md MAINTENANCE**
>
> This file is load-bearing. Any agent (Claude Code, Cursor, etc.) that
> modifies the codebase MUST update this file in the same change when:
> - A module is added, removed, renamed, or split (Repository Structure)
> - A launcher script is added or removed (Build & Run)
> - A version-bump site is added/removed (Version Management)
> - A new env var, kill switch, or opt-in flag is introduced (Companion
>   Subsystem / Common Pitfalls)
> - A subsystem changes ownership of state files (Verification Discipline)
> - A safety invariant or hard rule changes (Common Pitfalls / Audit Workflow)
> - The CI gate changes (Testing / Release & PR Workflow)
>
> If you cannot update this file in the same commit, leave a `TODO(claude-md):`
> marker in the relevant code change AND a matching `<!-- TODO(claude-md): -->`
> comment in this file. Stale CLAUDE.md is treated as a CI failure waiting
> to happen — do not let drift accumulate.
>
> Companion specs (`docs/COMPANION_SPEC.md`, `HYDRA_MEMORY.md`,
> `docs/BACKTEST_SPEC.md`) are the authoritative deep references. CLAUDE.md
> is the agent-facing index — it must point at them, not duplicate them.

This file provides context for Claude Code and other AI agents working on this repository.

## Project Overview

HYDRA is a regime-adaptive crypto trading agent for Kraken. It detects market conditions (trending, ranging, volatile) and switches between four strategies (Momentum, Mean Reversion, Grid, Defensive) to execute limit post-only orders on SOL/USDC, SOL/BTC, and BTC/USDC.

## Verification Discipline

- Always stop running agents/processes before editing journals, snapshots, or state files (they will be overwritten)
- After any fix, verify with actual commands (git tag -v, test runs) rather than claiming success
- Run the full test suite AND typecheck/lint before declaring work complete
- When a fix touches multi-file state (journal + snapshot + config), explicitly enumerate all files to update

**Hydra-specific:**
- Stop `hydra_agent.py` and the `start_hydra.bat` watchdog before editing
  `hydra_session_snapshot.json`, `hydra_order_journal.json`,
  `hydra_params_*.json`, or `hydra_errors.log` — the live agent rewrites them.
  See §Operating Rules → Rule 2 for the binding form.
- Verification commands: the CI invocation pattern in `.github/workflows/ci.yml`
  (individual `python tests/test_*.py` runs), `python tests/live_harness/harness.py --mode mock`,
  and `python hydra_engine.py` (synthetic demo). See §Operating Rules → Rule 3.
- Multi-file state for a typical engine change: snapshot + journal +
  `hydra_params_<pair>.json` (one per traded pair) + `hydra_errors.log`.
- The `.claude/hooks/post-edit.sh` hook runs a path-scoped verification step
  after every Edit/Write tool use. Set `HYDRA_POSTEDIT_HOOK_DISABLED=1` to
  silence it during heavy refactors. Hook failures are advisory only.

## Operating Rules

These rules are binding on any agent operating in this repo. Each was
earned through a documented past failure and is non-negotiable.

### Rule 1 — Parallel Task agents for any audit > 20 files

Past failure: single-pass review missed 7+ bugs that parallel-agent audits caught (10-agent and 6-agent sessions both surfaced findings the orchestrator alone missed).

> Use N parallel Task agents to audit this codebase. Split by directory or
> module group (see §Audit Workflow for Hydra's 7-way partition). Each agent
> returns HIGH/MED/LOW findings. Then synthesize.

Default: 7 agents on the partitions in §Audit Workflow. Scale up to 10+ if file count justifies.

### Rule 2 — Stop processes before editing their state

Past failure: in a journal-reconciliation session, the agent edited
`hydra_order_journal.json` multiple times while `hydra_agent.py` was running.
Each edit was overwritten on the next tick.

> Before editing any state file (journal, snapshot, db), check if a process
> is actively writing to it. If yes: stop the process first, make the edit,
> verify it persisted, then restart. Always clean the snapshot AND the
> journal together — they must stay in sync.

Hydra state-file owners: `hydra_agent.py` owns
`hydra_session_snapshot.json`, `hydra_order_journal.json`,
`hydra_params_*.json`, `hydra_errors.log`, `hydra_thesis.json`,
`hydra_thesis_documents/`, `hydra_thesis_processed/`,
`hydra_thesis_pending/`, `hydra_thesis_evidence_archive/`. The CBP sidecar
(`cbp-runner/state/`) is owned by `cbp-runner/supervisor.py` — see
[HYDRA_MEMORY.md](HYDRA_MEMORY.md) for kill switches.

### Rule 3 — Verify claims with actual commands

Past failure: agent claimed a git tag was verified without running
`git tag -v`; another session claimed a fix worked without re-running
the failing test.

> When you claim something is 'verified', 'passing', or 'fixed', you
> must run the actual verification command (pytest, git tag -v, curl,
> etc.) in the same turn and paste the output. No claims without evidence.

### Rule 4 — Two-phase self-audit on new code

Past failure: a single-pass review of new code missed bugs that a
self-audit caught. The journal-maintenance-tool session had the agent
find 7 bugs in its own code across two self-audit rounds.

> After you finish writing this, do a self-audit pass looking for: unused
> imports, dead code, unhandled exceptions, null/empty crashes, deprecated
> API usage, misleading error messages, false-positive checks. Fix
> everything found, then do a second self-audit pass. Only then declare done.

### Rule 5 — Enumerate all version-bump locations upfront

Past failure: v2.6.0 release bumped version in some files but missed
others, requiring a follow-up correction commit.

> Before bumping version to X.Y.Z, run:
> `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'`
> and list every location. Update all of them in one commit.

Hydra's canonical 7-site list is in §Version Management. The grep is the
safety net for sites added since that list was last updated.

## Repository Structure

```
hydra_engine.py            — Pure Python trading engine (indicators, regime detection, signals, position sizing)
hydra_agent.py             — Live agent (Kraken CLI via WSL, WebSocket broadcast, trade execution,
                             order reconciler, session snapshot + --resume)
hydra_brain.py             — AI reasoning: Claude Analyst + Risk Manager + Grok Strategist
hydra_tuner.py             — Self-tuning parameters via exponential smoothing of regime/signal thresholds
hydra_companions/          — Companion subsystem package (chat, proposals, nudges, ladder watcher,
                             live executor, CBP memory client, soul JSONs under souls/).
                             Module count not pinned — see docs/COMPANION_SPEC.md.
hydra_backtest.py          — Core replay engine (see Backtesting & Experimentation section)
hydra_backtest_metrics.py  — Bootstrap CI, walk-forward, Monte Carlo, regime-conditioned P&L
hydra_backtest_server.py   — BacktestWorkerPool + WS message handlers
hydra_backtest_tool.py     — Anthropic tool-use schemas + dispatcher + quota tracker
hydra_experiments.py       — Experiment dataclass + ExperimentStore + presets + sweep/compare
hydra_reviewer.py          — AI Reviewer (7 code-enforced rigor gates, PR-draft only)
hydra_shadow_validator.py  — Single-slot FIFO live-parallel validation before param writes
hydra_thesis.py            — Thesis layer (v2.13.0+): ThesisTracker, Ladder, IntentPrompt,
                             Evidence dataclasses. Golden Unicorn initiative — slow-moving
                             persistent worldview + user-authored intent. See §Thesis Layer.
journal_maintenance.py     — Order journal compaction / rotation
hydra_journal_migrator.py  — One-shot legacy hydra_trades_live.json → hydra_order_journal.json migration
dashboard/src/App.jsx      — React dashboard (single-file, all inline styles)
SKILL.md                   — Full trading specification (agent-readable)
AUDIT.md                   — Technical audit with test results
CHANGELOG.md               — Version history
HYDRA_MEMORY.md            — Memory wiring spec (CBP sidecar topology)
SECURITY.md                — Security policy
docs/BACKTEST.md           — User runbook for the backtesting platform
docs/BACKTEST_SPEC.md      — Authoritative backtest design spec
docs/COMPANION_SPEC.md     — Authoritative companion subsystem spec
```

Agent tooling (Claude Code project-scoped):

- `.claude/skills/release/SKILL.md` — release workflow skill (invoke via `/release`)
- `.claude/skills/audit/SKILL.md` — audit workflow skill (invoke via `/audit`)
- `.claude/settings.json` + `.claude/hooks/post-edit.sh` — path-scoped post-edit verification hook (set `HYDRA_POSTEDIT_HOOK_DISABLED=1` to silence)

Per-user `.claude/settings.local.json` and runtime `.claude/scheduled_tasks.lock` are gitignored; everything else under `.claude/` is committed (Claude Code's documented split between team-wide and per-user config). `.gitattributes` pins `*.sh text eol=lf` so Windows clones with `core.autocrlf=true` don't silently rewrite the hook's shebang to CRLF and break it on Git Bash / WSL.

## Memory & CBP Sidecar

- Hydra auto-launches the sibling `cbp-runner/` checkout from `start_hydra.bat` / `start_all.bat` via `python "%CBP_RUNNER_DIR%\supervisor.py" --detach`.
- `CBP_RUNNER_DIR` defaults to `C:\Users\elamj\Dev\cbp-runner`; override via env.
- `hydra_companions.cbp_client.CbpClient` reads `state/ready.json` on every call (tokens rotate).
- Authoritative wiring spec: [HYDRA_MEMORY.md](HYDRA_MEMORY.md). Sidecar invariants live in `cbp-runner/CLAUDE.md`.
- Kill switches: `CBP_SIDECAR_ENABLED=0` env or `state/_disabled` flag file. Hydra's memory path falls through to JSONL-only with no interruption — never block on the sidecar.

## Thesis Layer (v2.13.0+, Golden Unicorn)

Slow-moving persistent worldview + user-authored intent that sits *above*
the per-tick engine and the stateless 3-agent brain. Phase A (this
release) ships the foundational surface; Phases B–E extend brain context,
add Grok 4 reasoning document processing, the Ladder primitive, and
opt-in posture enforcement. Module: `hydra_thesis.py`. Plan file:
`~/.claude/plans/athena-shared-some-interesting-sleepy-seal.md`.

Design stance — **Hydra is the flywheel, not the shield.** The thesis
layer augments brain reasoning and surfaces user intent. It does not
throttle trading. `BLOCK` is reserved for the small set of hard rules:
ledger shield (0.20 BTC, user's long-term hold), tax friction floor
($50 realized gain), no-altcoin gate. Everything else is advisory context
that makes the brain smarter, not more restrictive.

- State file: `hydra_thesis.json` (atomic `.tmp` → `os.replace()` writes
  mirroring `_save_snapshot`). Gitignored. Subdirs
  `hydra_thesis_documents/` / `hydra_thesis_processed/` /
  `hydra_thesis_pending/` / `hydra_thesis_evidence_archive/` are lazy,
  also gitignored.
- Schema version: `THESIS_SCHEMA_VERSION = "1.0.0"` in `hydra_thesis.py`;
  bump independently when `ThesisState` JSON schema changes.
- Brain wiring (v2.13.1, Phase B): `HydraAgent._apply_brain` injects
  `state["thesis_context"]` → `HydraBrain._format_thesis_context` prepends
  a THESIS CONTEXT block to `ANALYST_PROMPT`. Active intent prompts are
  surfaced priority-ranked and scoped by pair. `BrainDecision.thesis_alignment`
  carries `{in_thesis, intent_prompts_consulted, evidence_delta,
  posterior_shift_request}` back to the agent; stamped onto journal entries
  as `decision.thesis_alignment` alongside `decision.thesis_posture` and
  `decision.thesis_intents_active`.
- Size multiplier (v2.13.1): `thesis.size_hint_for(pair, signal)` returns
  1.0 under default advisory enforcement — the brain's `size_multiplier`
  flows to Kelly unchanged. Only `posture_enforcement == "binding"`
  (Phase E, opt-in) derives a non-unity hint from `knobs.size_hint_range`
  × posture. Final product is clamped `[0.0, 1.5]` in `_apply_brain`.
- Intent prompts (v2.13.1): `ThesisTracker.add_intent / remove_intent /
  update_intent / list_intents`. `intent_prompt_max_active` (default 5)
  enforced via FIFO eviction. `on_tick` sweeps expired prompts once per
  tick. Three WS routes wired: `thesis_create_intent`, `thesis_delete_intent`,
  `thesis_update_intent`.
- Snapshot integration: `_save_snapshot` writes `thesis_state`;
  `_load_snapshot` calls `thesis.restore(...)`. Missing key is fail-soft.
- WS routes: `thesis_get_state`, `thesis_update_knobs`,
  `thesis_update_posture`, `thesis_update_hard_rules`. All handlers
  broadcast the new `thesis_state` message so every client stays in sync.
- Dashboard: new **THESIS** tab sibling to LIVE / BACKTEST / COMPARE.
  Phase A is functional for posture + knobs + hard rules + deadline;
  Phase B–E sub-panels are scaffolded placeholders.
- Kill switch: `HYDRA_THESIS_DISABLED=1` (see §Env flags).
- Hard-rule floor enforcement: `ledger_shield_btc` cannot be lowered
  below 0.20 BTC via the API — a dashboard typo or malicious WS payload
  cannot reduce the protected BTC. Test in `test_thesis_tracker.py`.
- Drift invariant: `tests/test_thesis_drift.py` enforces that
  `context_for` returns `None` and `size_hint_for` returns `1.0` in both
  disabled and default-enabled modes in Phase A. Any future phase that
  begins influencing the tick MUST preserve this for the disabled case.

Authoritative design spec: `docs/THESIS_SPEC.md` (arrives with Phase B
when brain integration lands). User runbook: `docs/THESIS.md`
(same timeline).

## Companion Subsystem

- Default-on. Chat, proposals, and proactive nudges are active without env vars. The orb in the dashboard IS the activation.
- Authoritative spec: [docs/COMPANION_SPEC.md](docs/COMPANION_SPEC.md).
- Package: `hydra_companions/` + soul JSONs at `hydra_companions/souls/` (`apex.soul.json`, `athena.soul.json`, `broski.soul.json`).
- Test launcher: `start_hydra_companion.bat` (paper mode, no real money).
- Env contract (opt-out only):
  - `HYDRA_COMPANION_DISABLED=1` — kill switch (no orb)
  - `HYDRA_COMPANION_PROPOSALS_ENABLED=0` — no trade cards
  - `HYDRA_COMPANION_NUDGES=0` — no proactive messages
  - `HYDRA_COMPANION_LIVE_EXECUTION=1` — opt-in real-order execution (default OFF for money safety)

## Key Technical Decisions

- **Pure Python, zero dependencies** — `hydra_engine.py` uses only stdlib. No numpy/pandas. Do not add external dependencies to the engine.
- **Limit post-only orders** — All trades use `--type limit --oflags post`. Never use market orders.
- **Kraken CLI via WSL** — Commands run through `wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken ..."`. The CLI is installed in WSL Ubuntu, not Windows.
- **Single-file dashboard** — All React components are in `App.jsx` with inline styles. No component library, no CSS modules. Keep it this way.
- **One engine per pair** — Each trading pair has its own independent `HydraEngine` instance. They do not share state.

## Build & Run

```bash
# Dashboard
cd dashboard && npm install && npm run dev

# Agent — conservative (default, 15-min candles, runs forever)
python hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC --balance 100

# Agent — competition mode (half-Kelly, lower threshold)
python hydra_agent.py --mode competition

# Agent — 5-min candles (faster ticks, noisier signals)
python hydra_agent.py --candle-interval 5

# Agent — paper trading (no API keys needed)
python hydra_agent.py --mode competition --paper

# Agent — resume previous session (restores engines + coordinator state)
python hydra_agent.py --mode competition --resume

# Engine synthetic demo (no API keys needed)
python hydra_engine.py
```

Windows launchers:

- `start_hydra.bat` — agent watchdog (production: `--mode competition --resume`)
- `start_all.bat` — full stack (CBP sidecar + agent + dashboard)
- `start_dashboard.bat` — dashboard only
- `start_hydra_companion.bat` — paper-mode companion testing harness (no real money)

## Working with the Code

### Indicators (hydra_engine.py)
- RSI uses Wilder's exponential smoothing — do not simplify to SMA
- ATR uses Wilder's exponential smoothing (same as RSI) — do not simplify to simple average
- MACD builds a full historical series then applies 9-EMA — do not simplify to single-point calculation
- Bollinger Bands use population variance (divide by N, not N-1)
- All indicators are stateless static methods — they recompute from the full price array each tick

### Regime Detection
- Priority: VOLATILE > TREND_UP > TREND_DOWN > RANGING
- Volatile check must come first — it overrides trend signals
- Warmup requires 50 candles before regime detection activates
- **Adaptive volatility threshold**: VOLATILE triggers when current ATR% exceeds `volatile_atr_mult` (default 1.8) times the asset's own median ATR% over the candle history. Same logic for BB width. This means SOL (naturally high ATR) and BTC (naturally low ATR) are evaluated against their own baselines, not a fixed absolute number. The tuner learns the optimal multiplier per pair. Floor values (1.5% ATR, 0.03 BB width) prevent degenerate behavior in dead markets.

### Trading
- Confidence threshold: 0.65 both modes. Applied to both BUY and SELL signals — SELL is gated by the same min_confidence check as BUY. Signals below 0.65 (< 15% Kelly edge) are filtered as negative-EV after costs.
- Position sizing: quarter-Kelly conservative, half-Kelly competition (`(confidence*2 - 1) * multiplier * balance`)
- Order minimums: pair-aware — Kraken `ordermin` per base asset (0.02 SOL, 0.00005 BTC), `costmin` per quote (0.5 USDC, 0.00002 BTC). Enforced on both buy and sell paths. Partial sells below ordermin force full position close to prevent dust.
- Price precision: `KrakenCLI._format_price(pair, price)` rounds to the pair's native decimals before the `.8f` format. Any code that computes a derived price MUST use this — raw `f"{price:.8f}"` will be rejected by Kraken on low-precision pairs (SOL/USDC=2, BTC/USDC=2, SOL/BTC=7). Hardcoded `PRICE_DECIMALS` remain as fallbacks; at startup `KrakenCLI.load_pair_constants()` dynamically loads the true values from `kraken pairs` and patches them via `apply_pair_constants()`.
- Dynamic pair constants: at startup (live mode), the agent calls `kraken pairs` to load `pair_decimals`, `ordermin`, and `costmin` for each traded pair. These override the hardcoded `PRICE_DECIMALS`, `MIN_ORDER_SIZE`, and `MIN_COST` class-level dicts. If the API call fails, hardcoded fallbacks are used — no degradation in behavior.
- System status gate: each tick (live mode) checks `kraken status` before doing any work. If Kraken reports `"maintenance"` or `"cancel_only"`, the tick is skipped with a log message. `"post_only"` is treated as normal (we only place post-only orders). API errors degrade gracefully to `"online"`. Status transitions are logged once per change, not every tick.
- Circuit breaker: 15% max drawdown halts the engine permanently for the session. Both `tick()` and `_maybe_execute` check the halt flag.
- Rate limiting: 2-second minimum between every Kraken API call — do not remove or reduce
- Order journal persistence: `order_journal` is snapshotted immediately after any tick that appends (not just on the periodic N-tick cadence), so a subsequent crash cannot lose entries since the last successful tick. The rolling file `hydra_order_journal.json` is merged on startup so restarts preserve full history.
- Execution stream: lifecycle finalization flows from `kraken ws executions` via the `ExecutionStream` class — push-based, not polling. Placement stays REST (`KrakenCLI.order_buy/sell` with `--userref` for correlation); WS events drive entries from `PLACED` to `FILLED` / `PARTIALLY_FILLED` / `CANCELLED_UNFILLED` / `REJECTED` and handle engine rollback on non-fills. All fill-detection uses the shared `_is_fully_filled()` helper with 1% tolerance.
- Execution stream restart-gap reconciliation: when the stream auto-restarts, `reconcile_restart_gap()` queries `kraken query-orders` for all in-flight orders to detect fills/cancels that occurred while the stream was down. Terminal events are injected into `drain_events()` so the agent processes them in the same tick the stream recovers. Orders still open on the exchange remain in `_known_orders` for the new stream to finalize normally.
- Resume reconciliation: on `--resume`, `_reconcile_stale_placed()` scans the journal for PLACED entries from the previous session and queries the exchange. Terminal orders (closed/canceled/expired) have their journal lifecycle updated directly. Still-open orders are re-registered with the ExecutionStream so WS events finalize them. Engine rollback is not possible for previous-session entries (no `pre_trade_snapshot` persisted) — a warning is logged if an unfilled order is found.
- BaseStream superclass: `ExecutionStream`, `CandleStream`, `TickerStream`, `BalanceStream`, and `BookStream` all inherit from `BaseStream` which provides subprocess spawn/stop, reader/stderr threads, heartbeat-based health checks, and auto-restart with cooldown. Subclasses override `_build_cmd()`, `_on_message(msg)`, and `_stream_label()`.
- Push-based market data: `CandleStream` (ws ohlc) and `TickerStream` (ws ticker) each subscribe to ALL traded pairs in one WS connection. `_fetch_and_tick()` uses the candle stream (zero REST calls, zero rate-limit sleep). Both streams are auto-restarted on failure via `ensure_healthy()` each tick. If a WS stream is unhealthy, the agent skips that data source until auto-restart recovers it. Order placement is blocked when TickerStream is unavailable.
- Push-based balances: `BalanceStream` (ws balances) receives real-time balance updates. `_build_dashboard_state()` uses WS data when healthy. If the stream is unhealthy, the agent skips balance updates until auto-restart recovers it. Asset names are normalized (XXBT→BTC, XBT→BTC) and equities/ETFs are filtered out.
- Push-based order book: `BookStream` (ws book) subscribes to all pairs with depth 10. Order book intelligence uses WS data when healthy. If the stream is unhealthy, the agent skips order book data until auto-restart recovers it. WS format `{price, qty}` dicts are converted to REST format `[price, qty, ts]` arrays so `OrderBookAnalyzer` works unchanged.
- Execution stream health: `ExecutionStream.health_status()` returns `(healthy, reason)` so the tick warning identifies *which* check failed (subprocess exited / reader thread crashed / heartbeat stale). `ensure_healthy()` auto-restarts the subprocess on failure with a `RESTART_COOLDOWN_S=30s` cooldown so we don't thrash. Heartbeat threshold is 30s — kraken cold-start over WSL can take 5–10s before the first heartbeat. A separate stderr-drain thread prevents the OS pipe buffer from filling and silently freezing the subprocess. The tick warning is rate-limited to *transitions* (one print per distinct reason; one "stream healthy again" print on recovery).
- Tick body is wrapped in try/except — any exception is logged to `hydra_errors.log` with full traceback and the tick loop continues to the next iteration instead of dying (which would trigger `start_hydra.bat` restart)
- FOREX session weighting: applies a confidence modifier based on UTC hour — London/NY overlap (12-16 UTC) +0.04, London (07-12) +0.02, NY (16-21) +0.02, Asian (00-07) -0.03, dead zone (21-00) -0.05. Subject to the same +0.15 total modifier cap as order book and cross-pair modifiers.

### Dashboard
- Connects to agent via WebSocket on port 8765
- All data comes from `state.pairs[pair]` — no direct API calls from the frontend
- Price formatting: use `fmtPrice()` for prices, `fmtInd()` for indicator values
- Charts use responsive SVG with `width="100%" viewBox`

## AI Brain (hydra_brain.py)

3-agent reasoning pipeline using Claude + Grok:
- **Market Analyst** (Claude Sonnet) — evaluates engine signals, produces thesis + conviction
- **Risk Manager** (Claude Sonnet) — approves/adjusts/overrides trades, manages risk exposure via `size_multiplier` (0.0-1.5). Brain does NOT modify engine confidence — Kelly sizing uses engine confidence directly, brain controls position size via size_multiplier only.
- **Strategic Advisor** (Grok 4 Reasoning) — called only on genuine disagreements: Risk Manager OVERRIDE, or analyst explicitly disagrees with engine at low conviction (< 0.50). Grok arbitrates the contested action only; conviction stays from analyst, sizing from risk manager.
- Only fires on BUY/SELL signals with fresh candle data (HOLD is free, no API call — skip logic lives in the agent's `_apply_brain`). Per-pair candle-freshness gating ensures brain evaluates each pair exactly once per new candle, preventing duplicate evaluation on forming-candle updates.
- Falls back to engine-only on API failure, budget exceeded, or missing key. Fallback does NOT mark the candle as evaluated, so the next tick retries.
- Enable by setting `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and/or `XAI_API_KEY` in `.env`
- Cost: ~$1-2/day with narrow Grok escalation on ~10-15% of signals
- Do not change the JSON response format in system prompts — the parser depends on it
- Strategist always uses `self.strategist_client` (xAI) — do not route it through primary client

## Backtesting & Experimentation

Strictly-additive platform layered on top of the live agent. Default behavior with no opt-in flag is identical to v2.9.x. Full user runbook: `docs/BACKTEST.md`. Authoritative design spec: `docs/BACKTEST_SPEC.md`.

### Module map
- `hydra_backtest.py` — core replay engine (`BacktestConfig`, `BacktestRunner`, `CandleSource` hierarchy, `SimulatedFiller`). Reuses `HydraEngine` verbatim — zero logic drift guaranteed by `tests/test_backtest_drift.py` (invariant I7).
- `hydra_backtest_metrics.py` — bootstrap CI, walk-forward, Monte Carlo, regime-conditioned P&L, parameter sensitivity.
- `hydra_experiments.py` — `Experiment` dataclass, `ExperimentStore` (threading.RLock — NOT Lock; delete → audit_log re-enters), 8 presets in `hydra_backtest_presets.json`, `sweep_experiment`, `compare`.
- `hydra_backtest_tool.py` — 8 Anthropic tool-use schemas (`BACKTEST_TOOLS`) + `BacktestToolDispatcher` + `QuotaTracker` (per_caller_daily=10, concurrent=3, global_daily=50; UTC midnight reset).
- `hydra_backtest_server.py` — `BacktestWorkerPool` (max_workers=2, daemon, queue=20) + WS message handlers mounted via `mount_backtest_routes`.
- `hydra_reviewer.py` — AI Reviewer with **7 code-enforced rigor gates** (not prompt). Tunable thresholds in `hydra_reviewer_config.json`.
- `hydra_shadow_validator.py` — single-slot FIFO live-parallel validation before param writes.
- `hydra_tuner.py` — added `apply_external_param_update()` + `rollback_to_previous()` (depth=1 history deque) alongside existing observation-driven update loop.

### Safety invariants (I1–I12)
1. Live tick cadence unaffected (measured pre/post deploy).
2. Backtest workers construct own engine instances — never hold refs to live.
3. Separate storage (`.hydra-experiments/`) — zero writes to live state files.
4. All workers are daemon threads.
5. Every worker entry wrapped in try/except; live loop isolated.
6. `HYDRA_BACKTEST_DISABLED=1` → v2.9.x behavior exactly.
7. Drift regression test on every commit.
8. Reviewer NEVER auto-applies code — PR drafts only.
9. Param changes require shadow validation + explicit human approval before live write.
10. Kraken candle fetches respect 2s rate limit; disk cache prevents redundancy.
11. Worker pool bounded — `MAX_WORKERS_HARD_CAP=4` clamped in `BacktestWorkerPool.__init__` (silently clamps + logs; configured values above 4 don't crash); queue depth 20; 50 experiments/day; 200k candles/experiment cap.
12. Every result stamped with git SHA, param hash, data hash, seed, hydra_version.

### Rigor gates (enforced in code, not prompt)
Before any `PARAM_TWEAK` is auto-apply eligible, all 7 must pass: `min_trades_50`, `mc_ci_lower_positive`, `wf_majority_improved`, `oos_gap_acceptable`, `improvement_above_2se`, `cross_pair_majority`, `regime_not_concentrated`. Regime-only failure downgrades verdict to a scoped `CODE_REVIEW` via **set-equality** check on the failed-gate list (order-independent). See `_assemble_decision` in `hydra_reviewer.py`.

### Reviewer tool-use (read_source_file)
Reviewer runs an Anthropic tool-use loop (`REVIEWER_TOOLS`) so `CODE_REVIEW` verdicts are grounded in real source. Allow-list: `hydra_*.py` at repo root + `tests/**/*.py`. Deny-list: path substrings `.env`, `config.json`, `credentials`, `secret`, `token`. Per-review: 6 reads, 16 KB per file, 6 loop iterations. Paths resolve against `ResultReviewer.source_root` and reject absolute paths, `..`, and symlinks escaping the repo. The read list lands on `ReviewDecision.source_files_read`.

### Reviewer PR drafts (I8)
Every `CODE_REVIEW` verdict emits `.hydra-experiments/pr_drafts/{exp_id}_{timestamp}.md` via `write_pr_draft()`. Includes verdict, proposed_changes table, rigor-gate results, evidence snapshot, risk_flags, consulted source files. Never touches source files. Advisory only — open a real PR from the draft.

### Retrospective accuracy + confidence decay
`ResultReviewer.self_retrospective(lookback_days=30)` joins `review_history.jsonl` (reviewer output) against `shadow_outcomes.jsonl` (shadow validator terminal records) by `experiment_id` and computes `reviewer_accuracy_score = approved / evaluated`. `_recent_accuracy()` caches this for 5 min. If recent accuracy drops below `0.5` with ≥5 evaluated samples, new `HIGH`-confidence verdicts are decayed to `MEDIUM` and a `confidence_decayed:...` risk_flag is appended.

### Cost disclosure ($10/day threshold)
Brain and reviewer both implement a one-shot per-UTC-day disclosure: when cumulative daily cost crosses `COST_ALERT_USD=10.0`, a log line prints and a `cost_alert` WS message broadcasts (`{component, daily_cost_usd, threshold_usd, day_key, enforce_budget}`). **Independent of `enforce_budget`** — a reviewer with `enforce_budget=False` (backtest mode) still alerts. Dashboard renders as a banner.

### Budget policy: live vs backtest
`HydraBrain` and `ResultReviewer` both take an `enforce_budget=True` kwarg. Live call sites keep default. Backtest-triggered instances pass `enforce_budget=False` so experiments don't stall behind the live `max_daily_cost` cap. Disclosure ($10/day) still fires regardless.

### Env flags
- `HYDRA_BACKTEST_DISABLED=1` — kill switch. Disables worker pool, WS handlers reject backtest messages.
- `HYDRA_BRAIN_TOOLS_ENABLED=1` — enables Anthropic tool-use for Analyst + Risk Manager (Grok stays text-only). Off by default; when on, per-agent quotas apply.
- `HYDRA_THESIS_DISABLED=1` — kill switch for the thesis layer (§Thesis Layer). Tracker returns inert defaults and `save()` is a no-op; `tests/test_thesis_drift.py` enforces v2.12.5 bit-identical behavior on every commit.

### Dashboard
`dashboard/src/App.jsx` gained tab switcher (LIVE / BACKTEST / COMPARE), `BacktestControlPanel`, `ObserverModal` (dual-state), `ExperimentLibrary`, `CompareResults`, and `ReviewPanel`. Shared primitives `RegimeBadge` and `SignalChip` prevent drift between LIVE and observer regime/signal styling. Equity history capped at `MAX_EQUITY_HISTORY_EXPERIMENTS=10` (LRU-ish) to prevent long-session memory growth. Typed-message fallback to `applyLiveState` is gated on absence of a `type` field AND presence of a `LIVE_STATE_KEYS` member — a malformed typed message can't corrupt LIVE. `compareInFlight` + `viewInFlight` states debounce repeat clicks. `DashboardBroadcaster` in `hydra_agent.py` refactored with `compat_mode=True` dual-emit (raw state + `{type, data}` wrapper) for one-release backward compatibility.

### Brain tool-use
`HydraBrain.__init__` gained `tool_dispatcher`, `enable_tool_use`, `enforce_budget`, `broadcaster`, and `tool_iterations_cap` kwargs. `_call_llm_with_tools()` implements the Anthropic stop_reason loop with the **injectable iteration cap** (default 4) and an 8 KB result cap that truncates via a structured JSON envelope (not a naive byte-slice) so the LLM sees a `truncated:true` signal instead of malformed JSON. `max_tokens` stop with pending `tool_use` blocks is logged rather than silently dropped. Analyst + Risk Manager branch on `_tool_use_enabled`; `_call_llm` unchanged for fallback and Grok path.

### Tests
The full suite lives under `tests/` and `tests/live_harness/`. The CI gate
is `.github/workflows/ci.yml` (`engine-tests` + `dashboard-build` jobs);
that workflow is the authoritative list of what must pass on every PR.
Run the backtest-platform subset locally as:

```bash
python -m pytest tests/test_backtest_engine.py tests/test_backtest_drift.py
python -m pytest tests/test_backtest_metrics.py tests/test_experiments.py
python -m pytest tests/test_backtest_tool.py tests/test_brain_tool_use.py
python -m pytest tests/test_backtest_server.py tests/test_reviewer.py
python -m pytest tests/test_shadow_validator.py
python tests/live_harness/harness.py --mode smoke   # kill-switch verified
```

### Gotchas
- `HYDRA_VERSION` in `hydra_backtest.py` stamps every `BacktestResult` — see §Version Management entry 6.
- `ExperimentStore` uses `threading.RLock()` — switching to `Lock` deadlocks `delete() → audit_log()` re-entry.
- `sanitize_json` replaces non-finite floats with None pre-serialize (stdlib `json.dump` emits `Infinity`). Applied on both main persistence AND `audit_log`/`log_review` jsonl writes.
- `sweep_experiment` clears `param_hash` + `created_at` before `replace()` on the frozen dataclass so `finalize_stamps` recomputes.
- `ResultReviewer._cost_lock` (threading.Lock) guards `_daily_tokens_in/_out/_daily_cost/_day_key/_cost_alert_fired_day`. Multi-worker concurrent reviews would otherwise race the counters.
- `.hydra-experiments/presets.json` (not `hydra_backtest_presets.json`) is the on-disk preset library — bootstrapped from `PRESET_LIBRARY` on first `load_presets()` call. `.hydra-experiments/reviewer_config.json` is bootstrapped by the reviewer on first init. Delete either to regenerate.
- `shadow_outcomes.jsonl` in the store root is append-only; written by `ShadowValidator._log_outcome()` on every `_finalize()`. Consumed by `ResultReviewer.self_retrospective()` for the accuracy score that drives confidence decay.

## Testing

The full suite lives under `tests/` and `tests/live_harness/`. The CI gate
is `.github/workflows/ci.yml` (`engine-tests` + `dashboard-build` jobs);
that workflow is the authoritative list of what must pass on every PR.
Run the suite locally as individual files (CI invocation pattern) or via
`python -m pytest tests/`.

Always run `python tests/live_harness/harness.py --mode mock` for any
change to the execution path (`HydraAgent._place_order`, `ExecutionStream`,
snapshot/restore, `PositionSizer`, or any order-journal write site).

### Live-execution test harness

`tests/live_harness/` drives `HydraAgent._place_order` across 33+ scenarios
(happy paths, failure modes, rollback completeness, schema validation,
historical regressions, WS execution stream lifecycle transitions, and real
Kraken). It is the canonical validation tool for any change to `_place_order`,
`ExecutionStream`, `snapshot_position`/`restore_position`, `PositionSizer`, or
any order-journal write site. Snapshot fields include `gross_profit` and
`gross_loss` for per-engine P&L tracking across restarts. A `FakeExecutionStream` test double lets mock
scenarios drive lifecycle transitions via `inject_event(...)` without spawning
the real `kraken ws executions` subprocess.

```bash
python tests/live_harness/harness.py --mode smoke    # import + agent construction
python tests/live_harness/harness.py --mode mock     # full mock-mode scenario run
python tests/live_harness/harness.py --mode validate # real Kraken read-only + --validate
python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders
```

`smoke` and `mock` run in CI on every PR. `mock` is the required gate for any
PR touching the execution path. `validate` and `live` are manual for high-risk
changes. See `tests/live_harness/README.md` for the scenario catalog, findings
tracker (HF-### IDs), authoring guide, and the field-sync checklist that must
be consulted before modifying `HydraEngine` snapshot fields.

See AUDIT.md for the full verification checklist.

## Release & PR Workflow

- For every feature/fix: create branch → tests pass → PR → verify CI green → merge → tag version
- Bump version in ALL locations (check package.json, __init__.py, README, docs, etc. — grep for current version)
- Use signed tags for releases
- Never merge with red or pending CI

**Hydra-specific:**
- "Tests pass" means both CI jobs green per `.github/workflows/ci.yml`:
  `engine-tests` (all individual `python tests/test_*.py` invocations
  + live harness `--mode smoke` + `--mode mock` + module import smoke)
  and `dashboard-build` (`npm run build`). The `mock` harness is the
  mandatory gate for any PR touching the execution path.
- "ALL locations" is the 7-site list in §Version Management below. Always
  run `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` BEFORE bumping, per
  §Operating Rules → Rule 5.
- Signed tag command: `git tag -s vX.Y.Z -m "vX.Y.Z"`. Verify with
  `git tag -v vX.Y.Z` per §Operating Rules → Rule 3.
- Use the `/release` skill (`.claude/skills/release/SKILL.md`) to drive
  the full cycle — it codifies steps 1–7 with the Hydra-specific
  expansions inline.

## Version Management

When bumping the version, **all seven locations must be updated in lockstep**:

1. `CHANGELOG.md` — new `## [X.Y.Z]` section header
2. `dashboard/package.json` — `"version"` field
3. `dashboard/package-lock.json` — both `"version"` fields (root + `""` package)
4. `dashboard/src/App.jsx` — footer string `HYDRA vX.Y.Z`
5. `hydra_agent.py` — `_export_competition_results()` → `"version"` field
6. `hydra_backtest.py` — `HYDRA_VERSION = "X.Y.Z"` (stamps every `BacktestResult`)
7. Git tag — `git tag vX.Y.Z` after merge to main

Only bump the **minor** version (e.g. 2.8 → 2.9) for material upgrades (new features, architectural changes). Bug fixes and doc tweaks use **patch** increments (e.g. 2.8.0 → 2.8.1).

## Windows/WSL Gotchas

- Use UTF-8 explicitly; cp1252 will crash on Unicode (emoji, special chars)
- time.time() has low resolution on Windows — use time.perf_counter() for timing
- Escape parentheses in .bat files, especially inside if-blocks
- Watch for Vite/dev-server silently switching ports; verify the actual bound port

**Hydra-specific:**
- The dashboard renders regime emoji (📈 ⚠️ etc.) and the console writes
  the portfolio block under the same theme — both will crash on cp1252.
- `time.time()` has ~15 ms Windows resolution; using it in `BaseStream`
  heartbeat checks or `RESTART_COOLDOWN_S=30s` accounting silently
  miscounts. Use `time.perf_counter()`.
- `start_hydra.bat` and `start_all.bat` use nested `if`-blocks around
  `--resume` and the CBP sidecar launch — escape parens or the cmd
  parser drops branches silently.
- WSL: kraken-cli runs via
  `wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken ..."`.
  If the distro is named `Ubuntu-22.04` instead of `Ubuntu`, the
  invocation silently routes nowhere — verify with `wsl -l -v`.
- Vite dev server in `dashboard/` falls off `:5173` to the next free
  port if it's taken; the dashboard WS proxy assumes `:5173`. Verify
  the bound port in Vite's startup log before assuming hot-reload works.

## Common Pitfalls

- Don't add `import numpy` or `import pandas` to the engine — it's intentionally pure Python
- Don't change orders to market type — limit post-only is a deliberate design choice
- Don't reduce rate limiting below 2s — Kraken will throttle or ban
- Don't merge engine instances across pairs — they must remain independent
- The `.env` file contains Kraken API keys — never commit it
- `hydra_order_journal.json` is the rolling order journal — it's gitignored. Legacy `hydra_trades_live.json` is auto-migrated on first startup and preserved as `hydra_trades_live.json.migrated`.
- `hydra_params_*.json` files are learned tuning parameters — they're gitignored
- `hydra_session_snapshot.json` is the session snapshot for `--resume` — it's gitignored
- On shutdown, the agent cancels all resting limit orders and flushes a snapshot — do not bypass this
- `start_hydra.bat` uses `--mode competition --resume` for production — do not remove these flags
- **Feature gap:** CrossPairCoordinator Rule 2 (BTC recovery BUY boost) and Rule 3 (coordinated swap SELL) can theoretically conflict if BTC is TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP simultaneously — Rule 3 overwrites Rule 2. Current behavior favors the safer SELL. Future work: add explicit priority or merge logic.
- Companion live execution requires explicit opt-in: `HYDRA_COMPANION_LIVE_EXECUTION=1`. Without it, companion proposals are paper/advisory only — confirm this env var is unset before any live debugging.
- CBP sidecar failures are silent by design (Hydra falls through to JSONL). If memory writes seem to vanish, check `cbp-runner/state/ready.json` exists and `cbp-runner/state/_disabled` does NOT exist.
- `kraken-cli` is an external dep installed in WSL Ubuntu (`source ~/.cargo/env && kraken`). The dashboard footer (`dashboard/src/App.jsx`) pins the current expected version. Check there before debugging schema errors from `--validate`.

## Audit Workflow

- For codebase audits, spawn parallel Task agents across file groups (pattern used successfully with 6-10 agents)
- Categorize findings as HIGH/MED/LOW and fix in that order
- Re-audit your own fixes before declaring done (self-audit has caught 7+ bugs in past sessions)

**Hydra-specific:**
- Natural file-group partitions for parallel agents (see §Operating Rules → Rule 1):
  1. Engine + tuner: `hydra_engine.py`, `hydra_tuner.py`
  2. Agent + streams: `hydra_agent.py` (contains `BaseStream` and its
     `ExecutionStream` / `CandleStream` / `TickerStream` / `BalanceStream` /
     `BookStream` subclasses)
  3. AI layer: `hydra_brain.py`, `hydra_reviewer.py`, `hydra_shadow_validator.py`
  4. Backtest platform: `hydra_backtest*.py`, `hydra_experiments.py`
  5. Companion subsystem: `hydra_companions/` package
  6. Dashboard: `dashboard/src/App.jsx`
  7. Tests: `tests/` + `tests/live_harness/`
- HIGH severity: any violation of safety invariants I1–I12
  (see §Backtesting & Experimentation), the limit-post-only rule,
  the 2 s rate-limit floor, the 15 % circuit breaker, the Wilder-EMA
  RSI/ATR specification, or the companion `HYDRA_COMPANION_LIVE_EXECUTION`
  default-off contract.
- Two-phase self-audit is mandatory per §Operating Rules → Rule 4: after
  fixing HIGH/MED items, re-run the partition sweep against your own diff,
  then re-run the full §Testing block AND
  `python tests/live_harness/harness.py --mode mock`. Only declare done
  when phase 2 is clean.
- Use the `/audit` skill (`.claude/skills/audit/SKILL.md`) to drive
  the full cycle.
