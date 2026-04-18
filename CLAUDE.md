# CLAUDE.md — Agent Instructions for HYDRA

> **AGENT HARD REQUIREMENT — CLAUDE.md MAINTENANCE**
>
> Load-bearing. Any agent modifying the codebase MUST update this file in the same change when:
> - A module is added/removed/renamed/split (Repository Structure)
> - A launcher script is added/removed (Build & Run)
> - A version-bump site is added/removed (Version Management)
> - A new env var, kill switch, or opt-in flag is introduced (Companion / Common Pitfalls)
> - A subsystem changes state-file ownership (Verification Discipline)
> - A safety invariant or hard rule changes (Common Pitfalls / Audit Workflow)
> - The CI gate changes (Testing / Release & PR Workflow)
>
> If not updatable in the same commit, leave a `TODO(claude-md):` marker in code AND a matching `<!-- TODO(claude-md): -->` here. Stale CLAUDE.md is treated as a CI failure waiting to happen.
>
> Deep references live in `docs/COMPANION_SPEC.md`, `HYDRA_MEMORY.md`, `docs/BACKTEST_SPEC.md`. CLAUDE.md is the agent-facing index — point, don't duplicate.

## Project Overview

HYDRA is a regime-adaptive crypto trading agent for Kraken. It detects market conditions (trending, ranging, volatile) and switches between four strategies (Momentum, Mean Reversion, Grid, Defensive) to execute limit post-only orders on SOL/USDC, SOL/BTC, and BTC/USDC.

## Verification Discipline

- Always stop running agents/processes before editing journals, snapshots, or state files (they will be overwritten)
- After any fix, verify with actual commands (git tag -v, test runs) rather than claiming success
- Run the full test suite AND typecheck/lint before declaring work complete
- When a fix touches multi-file state (journal + snapshot + config), explicitly enumerate all files to update

**Hydra-specific:**
- Stop `hydra_agent.py` + `start_hydra.bat` watchdog before editing `hydra_session_snapshot.json`, `hydra_order_journal.json`, `hydra_params_*.json`, or `hydra_errors.log` (live agent rewrites). See Rule 2 for binding form.
- Verification commands: CI invocation in `.github/workflows/ci.yml` (individual `python tests/test_*.py`), `python tests/live_harness/harness.py --mode mock`, `python hydra_engine.py` (synthetic demo). See Rule 3.
- Typical engine-change state surface: snapshot + journal + `hydra_params_<pair>.json` (per pair) + `hydra_errors.log`.
- `.claude/hooks/post-edit.sh` runs path-scoped verification after every Edit/Write. Silence with `HYDRA_POSTEDIT_HOOK_DISABLED=1` during heavy refactors. Hook failures advisory only.

## Operating Rules

These rules are binding on any agent operating in this repo. Each was
earned through a documented past failure and is non-negotiable.

### Rule 1 — Parallel Task agents for any audit > 20 files

Past failure: single-pass review missed 7+ bugs that parallel-agent audits caught.

> Use N parallel Task agents to audit this codebase. Split by directory or
> module group (see §Audit Workflow for Hydra's 7-way partition). Each agent
> returns HIGH/MED/LOW findings. Then synthesize.

Default: 7 agents on §Audit Workflow partitions. Scale to 10+ if file count justifies.

### Rule 2 — Stop processes before editing their state

Past failure: agent edited `hydra_order_journal.json` repeatedly while `hydra_agent.py` was running; each edit was overwritten on next tick.

> Before editing any state file (journal, snapshot, db), check if a process
> is actively writing to it. If yes: stop the process first, make the edit,
> verify it persisted, then restart. Always clean the snapshot AND the
> journal together — they must stay in sync.

State-file owners: `hydra_agent.py` owns `hydra_session_snapshot.json`, `hydra_order_journal.json`, `hydra_params_*.json`, `hydra_errors.log`, `hydra_thesis.json`, `hydra_thesis_{documents,processed,pending,evidence_archive}/`. CBP sidecar (`cbp-runner/state/`) is owned by `cbp-runner/supervisor.py` — kill switches in [HYDRA_MEMORY.md](HYDRA_MEMORY.md).

### Rule 3 — Verify claims with actual commands

Past failure: agent claimed a git tag was verified without running `git tag -v`; another claimed a fix worked without re-running the failing test.

> When you claim something is 'verified', 'passing', or 'fixed', you
> must run the actual verification command (pytest, git tag -v, curl,
> etc.) in the same turn and paste the output. No claims without evidence.

### Rule 4 — Two-phase self-audit on new code

Past failure: single-pass review missed 7+ bugs across two self-audit rounds in the journal-maintenance-tool session.

> After you finish writing this, do a self-audit pass looking for: unused
> imports, dead code, unhandled exceptions, null/empty crashes, deprecated
> API usage, misleading error messages, false-positive checks. Fix
> everything found, then do a second self-audit pass. Only then declare done.

### Rule 5 — Enumerate all version-bump locations upfront

Past failure: v2.6.0 bumped version in some files but missed others, requiring a follow-up commit.

> Before bumping version to X.Y.Z, run:
> `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'`
> and list every location. Update all of them in one commit.

Canonical 7-site list in §Version Management; the grep is the safety net for sites added since.

## Repository Structure

```
hydra_engine.py            — Pure Python trading engine (indicators, regime detection, signals, position sizing)
hydra_agent.py             — Live agent (Kraken CLI via WSL, WebSocket broadcast, trade execution,
                             order reconciler, session snapshot + --resume)
hydra_brain.py             — AI reasoning: Claude Analyst + Risk Manager + Grok Strategist
hydra_tuner.py             — Self-tuning parameters via exponential smoothing of regime/signal thresholds
hydra_companions/          — Companion subsystem (chat, proposals, nudges, ladder watcher, live
                             executor, CBP client, souls/). See docs/COMPANION_SPEC.md.
hydra_backtest.py          — Core replay engine (see Backtesting & Experimentation section)
hydra_backtest_metrics.py  — Bootstrap CI, walk-forward, Monte Carlo, regime-conditioned P&L
hydra_backtest_server.py   — BacktestWorkerPool + WS message handlers
hydra_backtest_tool.py     — Anthropic tool-use schemas + dispatcher + quota tracker
hydra_experiments.py       — Experiment dataclass + ExperimentStore + presets + sweep/compare
hydra_reviewer.py          — AI Reviewer (7 code-enforced rigor gates, PR-draft only)
hydra_shadow_validator.py  — Single-slot FIFO live-parallel validation before param writes
hydra_thesis.py            — Thesis layer (v2.13.0+): ThesisTracker, Ladder, IntentPrompt, Evidence.
                             Golden Unicorn — persistent worldview + user intent. See §Thesis Layer.
hydra_thesis_processor.py  — Grok 4 reasoning document processor (v2.13.2+). Daemon: ingests
                             user research → ProposedThesisUpdate JSON awaiting human approval.
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

Slow-moving persistent worldview + user-authored intent above the per-tick
engine and the stateless 3-agent brain. Shipped A→E across v2.13.0–v2.13.4.
Modules: `hydra_thesis.py` + `hydra_thesis_processor.py`. Plan file:
`~/.claude/plans/athena-shared-some-interesting-sleepy-seal.md`.
Authoritative design spec: `docs/THESIS_SPEC.md`. User runbook: `docs/THESIS.md`.

Design stance — **Hydra is the flywheel, not the shield.** The thesis
layer augments brain reasoning and surfaces user intent. It does not
throttle trading. `BLOCK` is reserved for the small set of hard rules:
ledger shield (0.20 BTC, user's long-term hold), tax friction floor
($50 realized gain), no-altcoin gate. Everything else is advisory context
that makes the brain smarter, not more restrictive.

- State: `hydra_thesis.json` (atomic `.tmp` → `os.replace()`, mirrors `_save_snapshot`). Subdirs `hydra_thesis_{documents,processed,pending,evidence_archive}/` are lazy. All gitignored. Schema version `THESIS_SCHEMA_VERSION` in `hydra_thesis.py`; bump independently of `HYDRA_VERSION` on `ThesisState` schema changes.
- Brain wiring: `HydraAgent._apply_brain` injects `state["thesis_context"]` → `HydraBrain._format_thesis_context` prepends THESIS CONTEXT to `ANALYST_PROMPT`. Active intent prompts surfaced priority-ranked, scoped by pair. `BrainDecision.thesis_alignment` (`in_thesis`, `intent_prompts_consulted`, `evidence_delta`, `posterior_shift_request`) is stamped onto journal entries alongside `decision.thesis_posture` and `decision.thesis_intents_active`.
- Size multiplier: `thesis.size_hint_for(pair, signal)` returns 1.0 under default advisory mode — brain's `size_multiplier` flows to Kelly unchanged. Only `posture_enforcement == "binding"` derives a non-unity hint from `knobs.size_hint_range` × posture. Final product clamped `[0.0, 1.5]` in `_apply_brain`.
- Intent prompts: `ThesisTracker.add_intent / remove_intent / update_intent / list_intents`. `intent_prompt_max_active` (default 5) enforced via FIFO eviction. `on_tick` sweeps expired prompts. WS routes: `thesis_{create,delete,update}_intent`.
- Posture enforcement (opt-in via `knobs.posture_enforcement="binding"`): per-posture daily entry caps via `knobs.max_daily_entries_by_posture` (defaults PRESERVATION=2, TRANSITION=4, ACCUMULATION=None). Agent calls `thesis.check_posture_restriction(pair, side)` before execute_signal; false `allow` SKIPs (not BLOCKs) the trade, logs, broadcasts `thesis_posture_restriction`, lets tick continue. No journal entry for skipped placements. SKIP ≠ BLOCK — BLOCK reserved for hard rules. Counter per-pair per-UTC-day; `record_entry` prunes yesterday each call.
- Ladder primitive (feature-flagged `HYDRA_THESIS_LADDERS=1`): `ThesisTracker.create_ladder / list_ladders / cancel_ladder / match_rung / record_rung_{placement,fill} / check_stop_loss`. Per-pair cap via `knobs.max_active_ladders_per_pair`. Rung match: 0.5% price tolerance. `_place_order` calls `_journal_ladder_stamp(pair, side, price)` → stamps `decision.{ladder_id,rung_idx,adhoc}` on every placement. Stop-loss is ADVISORY — on breach with any FILLED rung the ladder flips to STOPPED_OUT, pending rungs CANCELLED, filled positions NOT auto-sold (deliberate non-goal). Per-tick expiry sweep when flag set; `convert_to_market` variant logged + treated as cancel. WS routes: `thesis_{create,cancel}_ladder`.
- Document processor: `hydra_thesis_processor.py` + `ThesisProcessorWorker` (daemon). Bounded queue of uploaded research → **Grok 4 reasoning** (xAI) → `ProposedThesisUpdate` JSON written to `hydra_thesis_pending/`. Nothing auto-applies. Budget cap: `knobs.grok_processing_budget_usd_per_day` (default $5). Requires `XAI_API_KEY` (kill switch in Env flags below). Big-shift proposals (|posterior_shift.confidence − 0.5| > 0.30) force `requires_human=true` in code — defensive, not prompt-dependent. `_apply_proposal` drops any `hard_rules` key — hard rules NEVER mutated by a proposal.
- Snapshot integration: `_save_snapshot` writes `thesis_state`; `_load_snapshot` calls `thesis.restore(...)`. Missing key fail-soft.
- WS routes (state mutators): `thesis_get_state`, `thesis_update_{knobs,posture,hard_rules}`. All handlers broadcast updated `thesis_state` so every client stays in sync.
- Dashboard: **THESIS** tab sibling to LIVE / BACKTEST / COMPARE. Phase A panels (posture / knobs / hard rules / deadline) functional; Phase B–E sub-panels scaffolded.
- Hard-rule floor: `ledger_shield_btc` cannot be lowered below 0.20 BTC via API — dashboard typo or malicious WS payload cannot reduce protected BTC. Test in `test_thesis_tracker.py`.
- Drift invariant: `tests/test_thesis_drift.py` enforces `context_for → None` and `size_hint_for → 1.0` in both disabled and default-enabled modes. Any future phase influencing the tick MUST preserve this for the disabled case.

Env flags (thesis layer):
- `HYDRA_THESIS_DISABLED=1` — full kill switch. Tracker returns inert defaults, `save()` is a no-op; `tests/test_thesis_drift.py` enforces v2.12.5 bit-identical behavior on every commit.
- `HYDRA_THESIS_PROCESSOR_DISABLED=1` — disable Grok 4 document processor (v2.13.2+). Worker never starts; upload routes still persist documents to `hydra_thesis_documents/` but no proposal is generated.
- `HYDRA_THESIS_LADDERS=1` — opt in to Ladder primitive journal-schema fields (v2.13.3+). Without it, ladder CRUD still works but `match_rung` is a no-op and `_place_order` writes v2.13.2-shaped journal entries.

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
- Price precision & dynamic pair constants: `KrakenCLI._format_price(pair, price)` rounds to native decimals before `.8f`. Any code computing a derived price MUST use this — raw `f"{price:.8f}"` is rejected on low-precision pairs (SOL/USDC=2, BTC/USDC=2, SOL/BTC=7). At startup (live), `KrakenCLI.load_pair_constants()` queries `kraken pairs` for `pair_decimals` / `ordermin` / `costmin` per pair, then `apply_pair_constants()` patches `PRICE_DECIMALS` / `MIN_ORDER_SIZE` / `MIN_COST`. API failure falls back to hardcoded constants — no degradation.
- System status gate: each tick (live) checks `kraken status` before any work. `"maintenance"` / `"cancel_only"` → skip tick with log. `"post_only"` treated as normal (we only post-only). API errors degrade to `"online"`. Status transitions logged once per change.
- Circuit breaker: 15% max drawdown halts the engine permanently for the session. Both `tick()` and `_maybe_execute` check the halt flag.
- Rate limiting: 2-second minimum between every Kraken **REST** call (`KrakenCLI._run`) — do not remove or reduce. Steady-state market/account data flows through WS streams which bypass this; REST is now narrow (warmup OHLC, per-tick `kraken status`, order placement, `query-orders`, balance fallback, pair constants).
- Order journal persistence: `order_journal` snapshots immediately on any tick that appends (not just periodic N-tick cadence) — crash cannot lose entries since last successful tick. `hydra_order_journal.json` merged on startup so restarts preserve full history.
- Execution stream: lifecycle finalization flows from `kraken ws executions` via `ExecutionStream` — push-based. Placement stays REST (`KrakenCLI.order_buy/sell` + `--userref` correlation); WS events drive `PLACED → FILLED / PARTIALLY_FILLED / CANCELLED_UNFILLED / REJECTED` and trigger engine rollback on non-fills. Fill detection uses shared `_is_fully_filled()` (1% tolerance).
- Restart-gap reconciliation: on auto-restart, `reconcile_restart_gap()` queries `kraken query-orders` for in-flight orders to catch fills/cancels during downtime. Terminal events injected into `drain_events()` and processed same tick as recovery. Still-open orders remain in `_known_orders` for the new stream to finalize.
- Resume reconciliation: on `--resume`, `_reconcile_stale_placed()` scans journal for previous-session PLACED entries + queries exchange. Terminal orders (closed/canceled/expired) update journal lifecycle directly. Still-open orders re-register with ExecutionStream for WS finalization. Engine rollback impossible for previous-session entries (no `pre_trade_snapshot` persisted) — warns if unfilled order found.
- BaseStream superclass: `ExecutionStream / CandleStream / TickerStream / BalanceStream / BookStream` all inherit from `BaseStream` (subprocess spawn/stop, reader + stderr threads, heartbeat health, auto-restart with `RESTART_COOLDOWN_S=30s`). Subclasses override `_build_cmd()`, `_on_message(msg)`, `_stream_label()`. Heartbeat threshold 30s — accommodates kraken cold-start over WSL (5–10s to first heartbeat). Stderr-drain thread prevents pipe-buffer freeze. Per-tick `ensure_healthy()` auto-restarts; unhealthy streams cause that data source to be skipped until recovery. Tick warnings rate-limited to *transitions* (one print per distinct reason + one "healthy again" on recovery).
- Per-stream specifics: **CandleStream** (`ws ohlc`) — drives `_fetch_and_tick()` (zero REST, zero rate-limit). **TickerStream** (`ws ticker`) — order placement BLOCKED when unavailable. Both subscribe to all pairs in one WS connection. **BalanceStream** (`ws balances`) — feeds `_build_dashboard_state()`; asset names normalized (XXBT/XBT → BTC), equities/ETFs filtered. **BookStream** (`ws book`, depth 10) — feeds order book intel; WS `{price,qty}` dicts converted to REST `[price,qty,ts]` arrays so `OrderBookAnalyzer` works unchanged. **ExecutionStream** — `health_status() → (healthy, reason)` identifies subprocess exit / reader crash / heartbeat stale.
- Tick body is wrapped in try/except — any exception is logged to `hydra_errors.log` with full traceback and the tick loop continues to the next iteration instead of dying (which would trigger `start_hydra.bat` restart)
- FOREX session weighting: applies a confidence modifier based on UTC hour — London/NY overlap (12-16 UTC) +0.04, London (07-12) +0.02, NY (16-21) +0.02, Asian (00-07) -0.03, dead zone (21-00) -0.05. Subject to the same +0.15 total modifier cap as order book and cross-pair modifiers.

### Dashboard
- Connects to agent via WebSocket on port 8765
- All data comes from `state.pairs[pair]` — no direct API calls from the frontend
- Price formatting: use `fmtPrice()` for prices, `fmtInd()` for indicator values
- Charts use responsive SVG with `width="100%" viewBox`

## AI Brain (hydra_brain.py)

3-agent reasoning pipeline (Claude + Grok):
- **Market Analyst** (Claude Sonnet) — evaluates engine signals, produces thesis + conviction.
- **Risk Manager** (Claude Sonnet) — approves/adjusts/overrides via `size_multiplier` (0.0–1.5). Brain does NOT modify engine confidence — Kelly uses engine confidence directly; brain controls size only via `size_multiplier`.
- **Strategic Advisor** (Grok 4 Reasoning) — called only on genuine disagreement: Risk Manager OVERRIDE, or analyst disagrees with engine at low conviction (<0.50). Arbitrates contested action only; conviction stays from analyst, sizing from risk manager.
- Fires only on BUY/SELL with fresh candles (HOLD = no API call; skip in `_apply_brain`). Per-pair candle-freshness gating ensures one evaluation per new candle (no duplicates on forming-candle updates).
- Falls back to engine-only on API failure / budget exceeded / missing key. Fallback does NOT mark candle evaluated → next tick retries.
- Enable via `.env`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and/or `XAI_API_KEY`. Cost: ~$1–2/day with narrow Grok escalation (~10–15% of signals).
- Do not change the JSON response format in system prompts — parser depends on it.
- Strategist always uses `self.strategist_client` (xAI) — do not route through primary client.

## Backtesting & Experimentation

Strictly-additive platform layered on top of the live agent. Default behavior with no opt-in flag is identical to v2.9.x. Full user runbook: `docs/BACKTEST.md`. Authoritative design spec: `docs/BACKTEST_SPEC.md`.

### Module map
- `hydra_backtest.py` — replay engine (`BacktestConfig`, `BacktestRunner`, `CandleSource`, `SimulatedFiller`). Reuses `HydraEngine` verbatim — drift gated by `tests/test_backtest_drift.py` (I7).
- `hydra_backtest_metrics.py` — bootstrap CI, walk-forward, Monte Carlo, regime-conditioned P&L, parameter sensitivity.
- `hydra_experiments.py` — `Experiment` + `ExperimentStore` (uses `threading.RLock`; `Lock` deadlocks `delete → audit_log` re-entry), 8 presets in `hydra_backtest_presets.json`, `sweep_experiment`, `compare`.
- `hydra_backtest_tool.py` — 8 Anthropic tool schemas (`BACKTEST_TOOLS`) + `BacktestToolDispatcher` + `QuotaTracker` (per-caller=10/d, concurrent=3, global=50/d, UTC reset).
- `hydra_backtest_server.py` — `BacktestWorkerPool` (max=2, daemon, queue=20) + WS handlers via `mount_backtest_routes`.
- `hydra_reviewer.py` — Reviewer with **7 code-enforced rigor gates**. Thresholds in `hydra_reviewer_config.json`.
- `hydra_shadow_validator.py` — single-slot FIFO live-parallel validation before param writes.
- `hydra_tuner.py` — `apply_external_param_update()` + `rollback_to_previous()` (depth=1 deque) alongside the observation-driven update loop.

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
Anthropic tool-use loop (`REVIEWER_TOOLS`) grounds `CODE_REVIEW` verdicts in real source. Allow: `hydra_*.py` at repo root + `tests/**/*.py`. Deny substrings: `.env`, `config.json`, `credentials`, `secret`, `token`. Per review: 6 reads, 16 KB/file, 6 loop iterations. Paths resolve against `ResultReviewer.source_root`; absolute paths, `..`, and escaping symlinks rejected. Reads land on `ReviewDecision.source_files_read`.

### Reviewer PR drafts (I8)
`CODE_REVIEW` verdicts emit `.hydra-experiments/pr_drafts/{exp_id}_{timestamp}.md` via `write_pr_draft()` — verdict + proposed_changes table + rigor-gate results + evidence snapshot + risk_flags + consulted source files. Never touches source. Advisory only — open a real PR from the draft.

### Retrospective accuracy + confidence decay
`ResultReviewer.self_retrospective(lookback_days=30)` joins `review_history.jsonl` against `shadow_outcomes.jsonl` by `experiment_id`, computes `reviewer_accuracy_score = approved / evaluated`. `_recent_accuracy()` cached 5 min. Below 0.5 with ≥5 samples → new HIGH verdicts decayed to MEDIUM + `confidence_decayed:...` risk_flag appended.

### Cost disclosure ($10/day threshold)
Brain + reviewer one-shot per-UTC-day: cumulative cost crosses `COST_ALERT_USD=10.0` → log line + `cost_alert` WS broadcast (`{component, daily_cost_usd, threshold_usd, day_key, enforce_budget}`). **Independent of `enforce_budget`** — backtest reviewers (`enforce_budget=False`) still alert. Dashboard renders as banner.

### Budget policy: live vs backtest
`HydraBrain` + `ResultReviewer` take `enforce_budget=True` (default). Backtest-triggered instances pass `False` to avoid stalling on live `max_daily_cost` cap. $10/day disclosure fires regardless.

### Env flags
- `HYDRA_BACKTEST_DISABLED=1` — kill switch. Disables worker pool, WS handlers reject backtest messages.
- `HYDRA_BRAIN_TOOLS_ENABLED=1` — enables Anthropic tool-use for Analyst + Risk Manager (Grok stays text-only). Off by default; when on, per-agent quotas apply.
- Thesis kill switches (`HYDRA_THESIS_DISABLED`, `HYDRA_THESIS_PROCESSOR_DISABLED`, `HYDRA_THESIS_LADDERS`) — see §Thesis Layer.

### Dashboard
`dashboard/src/App.jsx`: tab switcher (LIVE/BACKTEST/COMPARE) + `BacktestControlPanel`, `ObserverModal` (dual-state), `ExperimentLibrary`, `CompareResults`, `ReviewPanel`. Shared `RegimeBadge` + `SignalChip` prevent LIVE/observer drift. Equity history capped at `MAX_EQUITY_HISTORY_EXPERIMENTS=10` (LRU). Typed-message → `applyLiveState` fallback gated on absence of `type` AND presence of `LIVE_STATE_KEYS` member (malformed typed messages can't corrupt LIVE). `compareInFlight` + `viewInFlight` debounce repeat clicks. `DashboardBroadcaster` (`hydra_agent.py`) dual-emits via `compat_mode=True` (raw state + `{type,data}` wrapper) for one-release back-compat.

### Brain tool-use
`HydraBrain.__init__` kwargs: `tool_dispatcher`, `enable_tool_use`, `enforce_budget`, `broadcaster`, `tool_iterations_cap`. `_call_llm_with_tools()` runs the Anthropic stop_reason loop with **injectable iteration cap** (default 4) + 8 KB result cap that truncates via structured JSON envelope (LLM sees `truncated:true`, not malformed JSON). `max_tokens` stop with pending `tool_use` blocks is logged, not silently dropped. Analyst + Risk Manager branch on `_tool_use_enabled`; `_call_llm` unchanged for fallback + Grok path.

### Tests
Backtest-platform subset (see §Testing for the full CI gate):

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

`tests/live_harness/` drives `HydraAgent._place_order` across 33+ scenarios (happy paths, failure modes, rollback completeness, schema validation, historical regressions, WS execution-stream lifecycle transitions, real Kraken). Canonical validation for any change to `_place_order`, `ExecutionStream`, `snapshot_position`/`restore_position`, `PositionSizer`, or any order-journal write site. Snapshot includes `gross_profit` + `gross_loss` for per-engine P&L across restarts. `FakeExecutionStream` test double drives lifecycle transitions via `inject_event(...)` without spawning real `kraken ws executions` subprocess.

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
- "Tests pass" = both CI jobs green per `.github/workflows/ci.yml`: `engine-tests` (all `python tests/test_*.py` + live harness `--mode smoke` + `--mode mock` + module import smoke) + `dashboard-build` (`npm run build`). `mock` harness is mandatory for any PR touching the execution path.
- "ALL locations" = the 7-site list in §Version Management. Run `git grep -nE 'v?[0-9]+\.[0-9]+\.[0-9]+'` BEFORE bumping (Rule 5).
- Signed tag: `git tag -s vX.Y.Z -m "vX.Y.Z"`. Verify with `git tag -v vX.Y.Z` (Rule 3).
- Drive full cycle via the `/release` skill — codifies steps 1–7 with Hydra-specific expansions inline.

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
- Dashboard regime emoji (📈 ⚠️) + console portfolio block use the same theme — both crash on cp1252.
- `time.time()` has ~15 ms Windows resolution; in `BaseStream` heartbeat or `RESTART_COOLDOWN_S=30s` accounting it silently miscounts. Use `time.perf_counter()`.
- `start_hydra.bat` + `start_all.bat` use nested `if`-blocks around `--resume` and CBP sidecar launch — escape parens or cmd parser drops branches silently.
- WSL: kraken-cli runs via `wsl -d Ubuntu -- bash -c "source ~/.cargo/env && kraken ..."`. If distro is `Ubuntu-22.04` instead of `Ubuntu`, invocation silently routes nowhere — verify with `wsl -l -v`.
- Vite dev server falls off `:5173` to next free port if taken; dashboard WS proxy assumes `:5173`. Verify bound port in Vite startup log.

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
- **Feature gap:** CrossPairCoordinator Rule 2 (BTC recovery BUY boost) + Rule 3 (coordinated swap SELL) can conflict when BTC TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP — Rule 3 overwrites Rule 2 (favors safer SELL). Future: explicit priority or merge logic.
- Companion live execution opt-in: `HYDRA_COMPANION_LIVE_EXECUTION=1`. Without it, proposals are paper/advisory — confirm unset before live debugging.
- CBP sidecar failures silent by design (falls through to JSONL). If memory writes vanish, check `cbp-runner/state/ready.json` exists and `state/_disabled` does NOT.
- `kraken-cli` is an external WSL Ubuntu dep (`source ~/.cargo/env && kraken`). Dashboard footer pins the expected version — check there before debugging `--validate` schema errors.

## Audit Workflow

- For codebase audits, spawn parallel Task agents across file groups (pattern used successfully with 6-10 agents)
- Categorize findings as HIGH/MED/LOW and fix in that order
- Re-audit your own fixes before declaring done (self-audit has caught 7+ bugs in past sessions)

**Hydra-specific:**
- Natural 7-way partitions for parallel agents (Rule 1):
  1. Engine + tuner: `hydra_engine.py`, `hydra_tuner.py`
  2. Agent + streams: `hydra_agent.py` (`BaseStream` + `ExecutionStream` / `CandleStream` / `TickerStream` / `BalanceStream` / `BookStream`)
  3. AI layer: `hydra_brain.py`, `hydra_reviewer.py`, `hydra_shadow_validator.py`
  4. Backtest: `hydra_backtest*.py`, `hydra_experiments.py`
  5. Companion: `hydra_companions/`
  6. Dashboard: `dashboard/src/App.jsx`
  7. Tests: `tests/` + `tests/live_harness/`
- HIGH severity: violations of I1–I12 (§Backtesting), limit-post-only rule, 2s rate-limit floor, 15% circuit breaker, Wilder-EMA RSI/ATR spec, or `HYDRA_COMPANION_LIVE_EXECUTION` default-off.
- Two-phase self-audit (Rule 4): after fixing HIGH/MED, re-run partition sweep against your diff, then full §Testing block + `python tests/live_harness/harness.py --mode mock`. Declare done only when phase 2 is clean.
- Drive full cycle via the `/audit` skill.
