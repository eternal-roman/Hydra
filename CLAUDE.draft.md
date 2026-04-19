# CLAUDE.md — Agent Instructions for HYDRA

> **HARD REQUIREMENT.** Update this file in the same change as: module
> add/remove/rename/split, launcher add/remove, version-bump site change,
> new env flag or kill switch, state-file ownership change, safety
> invariant change, CI gate change. If not possible in the same commit,
> leave `TODO(claude-md):` in code AND a matching `<!-- TODO(claude-md): -->`
> here. Stale CLAUDE.md = CI failure waiting to happen. Deep refs:
> `docs/COMPANION_SPEC.md`, `HYDRA_MEMORY.md`, `docs/BACKTEST_SPEC.md`,
> `docs/THESIS_SPEC.md`. This file is the agent-facing index — point,
> don't duplicate.

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

## Hydra Graph (CBP-style)

`defaults` are inherited prototypally by every node that doesn't override.
Nodes reference each other by id. `edges` capture cross-section
relationships using the standard-8 vocabulary (`causes`, `correlates`,
`contradicts`, `qualifies`, `supersedes`, `requires`, `inhibits`,
`amplifies`).

```json
{
  "$schema": "cbp/v0.1",
  "frame": "hydra_claude_md",
  "version_pin": "v2.13.4",

  "defaults": {
    "engine_lang": "python stdlib only (no numpy/pandas in engine)",
    "order_type": "limit post-only (--type limit --oflags post). Never market.",
    "engine_isolation": "one HydraEngine per pair, no shared state",
    "kraken_cli": "wsl -d Ubuntu -- bash -c \"source ~/.cargo/env && kraken ...\" (verify distro is exactly 'Ubuntu' via `wsl -l -v`)",
    "kraken_rest_min_interval_s": 2,
    "min_confidence": 0.65,
    "warmup_candles": 50,
    "circuit_breaker_drawdown_pct": 15,
    "ws_dashboard_port": 8765,
    "vite_dev_port_assumed": 5173,
    "state_file_owner": "agent",
    "state_file_safety": "stop owner; edit; verify persisted; restart",
    "env_flag_default": "off",
    "env_flag_enable_form": "<NAME>=1",
    "ci_authority": ".github/workflows/ci.yml",
    "ci_jobs": ["engine-tests", "dashboard-build"]
  },

  "project": {
    "name": "HYDRA",
    "purpose": "Regime-adaptive crypto trading agent for Kraken. Detects regime (trending/ranging/volatile), switches between 4 strategies (Momentum, MeanReversion, Grid, Defensive), executes limit post-only orders.",
    "pairs": ["SOL/USDC", "SOL/BTC", "BTC/USDC"]
  },

  "modules": [
    { "id": "engine",            "file": "hydra_engine.py",            "role": "indicators, regime detection, signals, position sizing" },
    { "id": "agent",             "file": "hydra_agent.py",             "role": "live agent: Kraken CLI via WSL, WS broadcast, execution, reconciler, snapshot + --resume" },
    { "id": "brain",             "file": "hydra_brain.py",             "role": "3-agent AI: Claude Analyst + Risk Manager + Grok Strategist" },
    { "id": "tuner",             "file": "hydra_tuner.py",             "role": "self-tuning params via exp-smoothed thresholds; apply_external_param_update + rollback_to_previous (depth=1 deque)" },
    { "id": "companions",        "file": "hydra_companions/",          "role": "chat/proposals/nudges/ladder/live executor/CBP client/souls; spec docs/COMPANION_SPEC.md" },
    { "id": "backtest",          "file": "hydra_backtest.py",          "role": "replay engine; reuses HydraEngine verbatim; HYDRA_VERSION lives here" },
    { "id": "backtest_metrics",  "file": "hydra_backtest_metrics.py",  "role": "bootstrap CI, walk-forward, Monte Carlo, regime P&L, sensitivity" },
    { "id": "backtest_server",   "file": "hydra_backtest_server.py",   "role": "BacktestWorkerPool (max=2 daemon, queue=20) + WS via mount_backtest_routes" },
    { "id": "backtest_tool",     "file": "hydra_backtest_tool.py",     "role": "8 Anthropic tool schemas + dispatcher + QuotaTracker (10/d caller, 3 concurrent, 50/d global, UTC reset)" },
    { "id": "experiments",       "file": "hydra_experiments.py",       "role": "Experiment + ExperimentStore (RLock — Lock deadlocks delete→audit_log re-entry); 8 presets; sweep/compare" },
    { "id": "reviewer",          "file": "hydra_reviewer.py",          "role": "AI Reviewer; 7 code-enforced rigor gates; PR-draft only" },
    { "id": "shadow_validator",  "file": "hydra_shadow_validator.py",  "role": "single-slot FIFO live-parallel validation before param writes" },
    { "id": "thesis",            "file": "hydra_thesis.py",            "role": "v2.13.0+: ThesisTracker, Ladder, IntentPrompt, Evidence; persistent worldview + user intent" },
    { "id": "thesis_processor",  "file": "hydra_thesis_processor.py",  "role": "v2.13.2+ daemon: research → ProposedThesisUpdate JSON awaiting human approval (Grok 4)" },
    { "id": "journal_maintenance","file": "journal_maintenance.py",    "role": "order journal compaction/rotation" },
    { "id": "journal_migrator",  "file": "hydra_journal_migrator.py",  "role": "one-shot legacy hydra_trades_live.json → hydra_order_journal.json (auto on first start; preserves .migrated)" },
    { "id": "dashboard",         "file": "dashboard/src/App.jsx",      "role": "single-file React, inline styles; tabs LIVE/BACKTEST/COMPARE/THESIS" }
  ],

  "deep_specs": {
    "SKILL.md": "full trading specification (agent-readable)",
    "AUDIT.md": "technical audit + verification checklist",
    "CHANGELOG.md": "version history",
    "HYDRA_MEMORY.md": "memory wiring + CBP sidecar topology",
    "SECURITY.md": "security policy",
    "docs/BACKTEST.md": "backtest user runbook",
    "docs/BACKTEST_SPEC.md": "backtest design spec (authoritative)",
    "docs/COMPANION_SPEC.md": "companion spec (authoritative)",
    "docs/THESIS_SPEC.md": "thesis design spec (authoritative)",
    "docs/THESIS.md": "thesis user runbook"
  },

  "claude_code_tooling": {
    "release_skill": ".claude/skills/release/SKILL.md (invoke /release)",
    "audit_skill":   ".claude/skills/audit/SKILL.md (invoke /audit)",
    "post_edit_hook": ".claude/hooks/post-edit.sh — path-scoped verification after Edit/Write; HYDRA_POSTEDIT_HOOK_DISABLED=1 to silence; failures advisory",
    "settings_split": "per-user .claude/settings.local.json + runtime .claude/scheduled_tasks.lock are gitignored; everything else under .claude/ is committed",
    "gitattributes_pin": "*.sh text eol=lf — prevents Windows core.autocrlf from CRLF-ing the hook shebang"
  },

  "state_files": [
    { "id": "snapshot",           "path": "hydra_session_snapshot.json", "notes": "atomic .tmp → os.replace; --resume target; embeds thesis_state" },
    { "id": "order_journal",      "path": "hydra_order_journal.json",    "notes": "snapshots immediately on any tick that appends (not periodic); crash cannot lose since last successful tick; gitignored" },
    { "id": "params",             "path": "hydra_params_<pair>.json",    "notes": "per-pair learned tuning params; gitignored" },
    { "id": "errors_log",         "path": "hydra_errors.log",            "notes": "tick try/except writes here with full traceback; loop continues (avoids start_hydra.bat restart)" },
    { "id": "thesis_state",       "path": "hydra_thesis.json",           "notes": "atomic .tmp → os.replace; THESIS_SCHEMA_VERSION bumps independently of HYDRA_VERSION; lazy subdirs hydra_thesis_{documents,processed,pending,evidence_archive}/; all gitignored" },
    { "id": "cbp_sidecar_state",  "path": "cbp-runner/state/",           "owner": "cbp-runner/supervisor.py", "notes": "kill: CBP_SIDECAR_ENABLED=0 or state/_disabled flag; Hydra falls through to JSONL — never blocks on sidecar" },
    { "id": "experiments_store",  "path": ".hydra-experiments/",         "owner": "experiments", "notes": "presets.json + reviewer_config.json bootstrap from code on first init (delete to regenerate); shadow_outcomes.jsonl append-only" }
  ],

  "memory_cbp_sidecar": {
    "auto_launch": "start_hydra.bat / start_all.bat → python %CBP_RUNNER_DIR%\\supervisor.py --detach",
    "default_dir": "C:\\Users\\elamj\\Dev\\cbp-runner (override CBP_RUNNER_DIR)",
    "client": "hydra_companions.cbp_client.CbpClient — reads state/ready.json on every call (tokens rotate)"
  },

  "build_run": {
    "dashboard_dev":         "cd dashboard && npm install && npm run dev",
    "agent_default":         "python hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC --balance 100  # conservative, 15-min, runs forever",
    "agent_competition":     "python hydra_agent.py --mode competition  # half-Kelly, lower threshold",
    "agent_5min":            "python hydra_agent.py --candle-interval 5  # faster, noisier",
    "agent_paper":           "python hydra_agent.py --mode competition --paper  # no API keys needed",
    "agent_resume":          "python hydra_agent.py --mode competition --resume  # restores engines + coordinator",
    "engine_demo":           "python hydra_engine.py  # synthetic, no API keys",
    "launchers": {
      "start_hydra.bat":           "agent watchdog (production: --mode competition --resume — do not remove)",
      "start_all.bat":             "full stack: CBP sidecar + agent + dashboard",
      "start_dashboard.bat":       "dashboard only",
      "start_hydra_companion.bat": "paper-mode companion testing harness (no real money)"
    }
  },

  "engine_invariants": {
    "indicators": {
      "rsi":       "Wilder's exponential smoothing (NOT SMA)",
      "atr":       "Wilder's exponential smoothing (NOT simple average)",
      "macd":      "full historical series then 9-EMA (NOT single-point)",
      "bollinger": "population variance — divide by N, not N-1",
      "shape":     "all stateless static methods; recompute from full price array each tick"
    },
    "regime": {
      "priority": "VOLATILE > TREND_UP > TREND_DOWN > RANGING (volatile checked first; overrides trend)",
      "warmup":   "50 candles before regime activates",
      "adaptive_volatility": "VOLATILE triggers when current ATR% > volatile_atr_mult (default 1.8) × asset's median ATR% across candle history. Same logic for BB width. Pair-relative, not fixed absolute. Tuner learns multiplier per pair. Floors (1.5% ATR, 0.03 BB width) prevent dead-market degeneracy."
    }
  },

  "trading_invariants": {
    "min_confidence": "0.65 both modes; applied to BUY AND SELL; <0.65 = <15% Kelly edge = filtered as negative-EV after costs",
    "sizing":         "(confidence*2 - 1) × multiplier × balance; quarter-Kelly conservative, half-Kelly competition",
    "minimums":       "pair-aware ordermin per base (0.02 SOL, 0.00005 BTC) + costmin per quote (0.5 USDC, 0.00002 BTC); enforced both buy/sell; partial sells below ordermin force full close to prevent dust",
    "price_precision":"KrakenCLI._format_price(pair, price) rounds to native decimals before .8f; raw f\"{price:.8f}\" rejected on low-precision pairs (SOL/USDC=2, BTC/USDC=2, SOL/BTC=7); load_pair_constants() queries `kraken pairs` at startup, apply_pair_constants() patches PRICE_DECIMALS/MIN_ORDER_SIZE/MIN_COST; API failure → hardcoded fallback",
    "system_status":  "each live tick: `kraken status` first; maintenance/cancel_only → skip with log; post_only treated normal (we only post-only); errors degrade to online; transitions logged once",
    "circuit_breaker":"15% drawdown halts engine permanently for the session; both tick() and _maybe_execute check halt flag",
    "rest_rate_limit":"2s minimum between every Kraken REST call (KrakenCLI._run); steady-state market/account flows via WS (bypass rate limit); REST is now narrow: warmup OHLC, per-tick status, placement, query-orders, balance fallback, pair constants",
    "execution":      "ExecutionStream (push from `kraken ws executions`) drives PLACED → FILLED/PARTIALLY_FILLED/CANCELLED_UNFILLED/REJECTED; placement stays REST (order_buy/sell + --userref correlation); WS events trigger engine rollback on non-fills; fill detection via shared _is_fully_filled() (1% tolerance)",
    "restart_gap":    "auto-restart: reconcile_restart_gap() queries query-orders for in-flight; terminal events injected into drain_events() and processed same tick; still-open remain in _known_orders for new stream to finalize",
    "resume_recon":   "--resume: _reconcile_stale_placed() scans journal for previous-session PLACED + queries exchange; terminal updates lifecycle directly; still-open re-register with ExecutionStream; engine rollback impossible for prev-session entries (no pre_trade_snapshot persisted) — warns if unfilled",
    "tick_isolation": "tick body wrapped try/except; exceptions logged to hydra_errors.log with full traceback; loop continues (dying triggers start_hydra.bat restart)",
    "forex_session":  "confidence modifier by UTC hour: London/NY overlap (12-16) +0.04, London (07-12) +0.02, NY (16-21) +0.02, Asian (00-07) -0.03, dead (21-00) -0.05; subject to +0.15 total cap shared with order-book and cross-pair modifiers",
    "shutdown":       "cancels all resting limit orders and flushes a snapshot — do not bypass"
  },

  "streams": {
    "base_class": "BaseStream — subprocess spawn/stop, reader+stderr threads, heartbeat health, auto-restart RESTART_COOLDOWN_S=30s; subclasses override _build_cmd/_on_message/_stream_label; heartbeat threshold 30s (kraken cold-start over WSL is 5–10s); stderr-drain prevents pipe-buffer freeze; per-tick ensure_healthy() auto-restarts; tick warnings rate-limited to transitions (one print per distinct reason + one 'healthy again' on recovery)",
    "instances": [
      { "id": "execution", "wsl_cmd": "ws executions", "purpose": "order lifecycle; health_status() → (healthy, reason): subprocess exit / reader crash / heartbeat stale" },
      { "id": "candle",    "wsl_cmd": "ws ohlc",       "purpose": "drives _fetch_and_tick() (zero REST, zero rate-limit); all pairs in one WS" },
      { "id": "ticker",    "wsl_cmd": "ws ticker",     "purpose": "order placement BLOCKED when unavailable; all pairs in one WS" },
      { "id": "balance",   "wsl_cmd": "ws balances",   "purpose": "feeds _build_dashboard_state(); asset names normalized (XXBT/XBT → BTC); equities/ETFs filtered" },
      { "id": "book",      "wsl_cmd": "ws book depth=10", "purpose": "order-book intel; WS {price,qty} dicts converted to REST [price,qty,ts] arrays so OrderBookAnalyzer works unchanged" }
    ]
  },

  "ai_brain": {
    "agents": {
      "analyst":      { "model": "Claude Sonnet", "role": "evaluates engine signals; emits thesis + conviction" },
      "risk_manager": { "model": "Claude Sonnet", "role": "approves/adjusts/overrides via size_multiplier (0.0–1.5); brain does NOT modify engine confidence — Kelly uses engine confidence directly" },
      "strategist":   { "model": "Grok 4 Reasoning (xAI)", "role": "called only on genuine disagreement: Risk Manager OVERRIDE, or analyst disagrees with engine at low conviction (<0.50); arbitrates contested action only" }
    },
    "fires_on":     "BUY/SELL only with fresh candles; HOLD = no API call (skipped in _apply_brain); per-pair candle-freshness gating ensures one eval per new candle",
    "fallback":     "engine-only on API failure / budget exceeded / missing key; does NOT mark candle evaluated → next tick retries",
    "env_keys":     ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"],
    "cost":         "~$1–2/day with narrow Grok escalation (~10–15% of signals)",
    "constraints":  ["do not change JSON response format in system prompts (parser depends on it)", "strategist always uses self.strategist_client (xAI) — do not route through primary client"],
    "tool_use":     "HydraBrain.__init__ kwargs: tool_dispatcher, enable_tool_use, enforce_budget, broadcaster, tool_iterations_cap. _call_llm_with_tools() runs Anthropic stop_reason loop with injectable iteration cap (default 4) + 8 KB result cap that truncates via structured JSON envelope (LLM sees truncated:true). max_tokens stop with pending tool_use blocks logged not silently dropped. Analyst+RM branch on _tool_use_enabled; _call_llm unchanged for fallback + Grok path. Enable: HYDRA_BRAIN_TOOLS_ENABLED=1."
  },

  "thesis_layer": {
    "introduced": "v2.13.0 (shipped A→E across v2.13.0–v2.13.4)",
    "stance":     "Hydra is the flywheel, not the shield. Thesis augments brain reasoning + surfaces user intent; does NOT throttle trading. BLOCK reserved for hard rules: ledger shield (0.20 BTC), tax friction floor ($50 realized), no-altcoin gate. Everything else is advisory.",
    "schema_field":"THESIS_SCHEMA_VERSION (in hydra_thesis.py); bump independently of HYDRA_VERSION on ThesisState changes",
    "brain_wiring":"HydraAgent._apply_brain injects state['thesis_context'] → HydraBrain._format_thesis_context prepends THESIS CONTEXT to ANALYST_PROMPT; intent prompts surfaced priority-ranked, scoped by pair; BrainDecision.thesis_alignment (in_thesis, intent_prompts_consulted, evidence_delta, posterior_shift_request) stamped onto journal entries alongside decision.thesis_posture and decision.thesis_intents_active",
    "size_hint":  "thesis.size_hint_for(pair, signal) → 1.0 under default advisory mode (brain's size_multiplier flows to Kelly unchanged); only posture_enforcement=='binding' derives non-unity hint from knobs.size_hint_range × posture; final clamped [0.0, 1.5] in _apply_brain",
    "intents":    "ThesisTracker.add_intent/remove/update/list; intent_prompt_max_active default 5 with FIFO eviction; on_tick sweeps expired; WS routes thesis_{create,delete,update}_intent",
    "posture_enforcement_optin":"knobs.posture_enforcement='binding' enables per-posture daily entry caps via knobs.max_daily_entries_by_posture (defaults: PRESERVATION=2, TRANSITION=4, ACCUMULATION=None). Agent calls thesis.check_posture_restriction(pair,side) before execute_signal; false `allow` SKIPs (≠ BLOCKs) the trade, logs, broadcasts thesis_posture_restriction, lets tick continue. No journal entry for skipped placements. SKIP ≠ BLOCK — BLOCK reserved for hard rules. Counter per-pair per-UTC-day; record_entry prunes yesterday each call.",
    "ladder_optin":"HYDRA_THESIS_LADDERS=1. ThesisTracker.create_ladder/list/cancel/match_rung/record_rung_{placement,fill}/check_stop_loss; per-pair cap knobs.max_active_ladders_per_pair; rung match 0.5% price tolerance; _place_order calls _journal_ladder_stamp(pair,side,price) → stamps decision.{ladder_id,rung_idx,adhoc} on every placement. Stop-loss is ADVISORY — on breach with any FILLED rung the ladder flips STOPPED_OUT, pending rungs CANCELLED, filled positions NOT auto-sold (deliberate non-goal). Per-tick expiry sweep when flag set; convert_to_market variant logged + treated as cancel. WS routes thesis_{create,cancel}_ladder.",
    "doc_processor":"hydra_thesis_processor.py + ThesisProcessorWorker daemon; bounded queue of uploaded research → Grok 4 reasoning → ProposedThesisUpdate JSON in hydra_thesis_pending/; nothing auto-applies; budget cap knobs.grok_processing_budget_usd_per_day default $5; requires XAI_API_KEY; big-shift proposals (|posterior_shift.confidence − 0.5| > 0.30) force requires_human=true in code (defensive, not prompt-dependent); _apply_proposal drops any hard_rules key — hard rules NEVER mutated by a proposal",
    "snapshot":   "_save_snapshot writes thesis_state; _load_snapshot calls thesis.restore(...); missing key fail-soft",
    "ws_mutators":["thesis_get_state","thesis_update_knobs","thesis_update_posture","thesis_update_hard_rules"],
    "ws_invariant":"all handlers broadcast updated thesis_state to keep clients in sync",
    "dashboard":  "THESIS tab sibling to LIVE/BACKTEST/COMPARE; Phase A panels (posture/knobs/hard rules/deadline) functional; B–E scaffolded",
    "hard_rule_floor":"ledger_shield_btc CANNOT be lowered below 0.20 BTC via API — dashboard typo or malicious WS payload cannot reduce protected BTC; test in test_thesis_tracker.py",
    "drift_invariant":"tests/test_thesis_drift.py enforces context_for → None and size_hint_for → 1.0 in both disabled and default-enabled modes; any future phase influencing the tick MUST preserve this for the disabled case"
  },

  "companion_subsystem": {
    "default":      "ON. Chat, proposals, proactive nudges active without env vars. The orb in the dashboard IS the activation.",
    "package":      "hydra_companions/ + souls at hydra_companions/souls/ (apex.soul.json, athena.soul.json, broski.soul.json)",
    "test_launcher":"start_hydra_companion.bat (paper, no real money)"
  },

  "backtest_platform": {
    "principle":     "strictly-additive on top of live agent; default behavior with no opt-in identical to v2.9.x",
    "safety_invariants": [
      "I1 live tick cadence unaffected (measured pre/post deploy)",
      "I2 backtest workers construct own engine instances — never hold refs to live",
      "I3 separate storage (.hydra-experiments/) — zero writes to live state files",
      "I4 all workers are daemon threads",
      "I5 every worker entry wrapped try/except; live loop isolated",
      "I6 HYDRA_BACKTEST_DISABLED=1 → v2.9.x behavior exactly",
      "I7 drift regression test on every commit (tests/test_backtest_drift.py)",
      "I8 reviewer NEVER auto-applies code — PR drafts only",
      "I9 param changes require shadow validation + explicit human approval before live write",
      "I10 Kraken candle fetches respect 2s rate limit; disk cache prevents redundancy",
      "I11 worker pool bounded — MAX_WORKERS_HARD_CAP=4 clamped in BacktestWorkerPool.__init__ (silently clamps + logs); queue 20; 50 experiments/day; 200k candles/experiment cap",
      "I12 every result stamped with git SHA, param hash, data hash, seed, hydra_version"
    ],
    "rigor_gates_all_must_pass_for_PARAM_TWEAK_autoapply": [
      "min_trades_50","mc_ci_lower_positive","wf_majority_improved","oos_gap_acceptable","improvement_above_2se","cross_pair_majority","regime_not_concentrated"
    ],
    "rigor_gates_note":"regime-only failure downgrades verdict to scoped CODE_REVIEW via set-equality check on failed-gate list (order-independent); see _assemble_decision in hydra_reviewer.py",
    "reviewer_tool_use":"REVIEWER_TOOLS allow hydra_*.py at repo root + tests/**/*.py; deny substrings .env, config.json, credentials, secret, token; per review 6 reads, 16 KB/file, 6 loop iterations; paths resolve against ResultReviewer.source_root; absolute paths, .., escaping symlinks rejected; reads land on ReviewDecision.source_files_read",
    "pr_drafts":"CODE_REVIEW emits .hydra-experiments/pr_drafts/{exp_id}_{ts}.md via write_pr_draft() — verdict + proposed_changes + rigor results + evidence + risk_flags + consulted source files; never touches source; advisory only",
    "retrospective_decay":"ResultReviewer.self_retrospective(lookback_days=30) joins review_history.jsonl × shadow_outcomes.jsonl by experiment_id → reviewer_accuracy_score = approved/evaluated; _recent_accuracy() cached 5 min; <0.5 with ≥5 samples → new HIGH verdicts decayed to MEDIUM + confidence_decayed:... risk_flag",
    "cost_disclosure":"brain + reviewer one-shot per-UTC-day: cumulative cost crosses COST_ALERT_USD=10.0 → log + cost_alert WS broadcast {component, daily_cost_usd, threshold_usd, day_key, enforce_budget}; INDEPENDENT of enforce_budget — backtest reviewers (enforce_budget=False) still alert; dashboard renders as banner",
    "budget_policy":"HydraBrain + ResultReviewer take enforce_budget=True default; backtest-triggered instances pass False to avoid stalling on live max_daily_cost cap; $10/day disclosure fires regardless",
    "dashboard_pieces":"App.jsx tab switcher LIVE/BACKTEST/COMPARE + BacktestControlPanel, ObserverModal (dual-state), ExperimentLibrary, CompareResults, ReviewPanel; shared RegimeBadge + SignalChip prevent LIVE/observer drift; equity history capped MAX_EQUITY_HISTORY_EXPERIMENTS=10 (LRU); typed-message → applyLiveState fallback gated on absence of `type` AND presence of LIVE_STATE_KEYS member (malformed typed messages can't corrupt LIVE); compareInFlight + viewInFlight debounce repeat clicks; DashboardBroadcaster (hydra_agent.py) dual-emits via compat_mode=True (raw state + {type,data} wrapper) for one-release back-compat",
    "test_subset": [
      "python -m pytest tests/test_backtest_engine.py tests/test_backtest_drift.py",
      "python -m pytest tests/test_backtest_metrics.py tests/test_experiments.py",
      "python -m pytest tests/test_backtest_tool.py tests/test_brain_tool_use.py",
      "python -m pytest tests/test_backtest_server.py tests/test_reviewer.py",
      "python -m pytest tests/test_shadow_validator.py",
      "python tests/live_harness/harness.py --mode smoke   # kill-switch verified"
    ],
    "gotchas": [
      "HYDRA_VERSION in hydra_backtest.py stamps every BacktestResult — see version_sites entry 6",
      "ExperimentStore uses threading.RLock() — switching to Lock deadlocks delete()→audit_log() re-entry",
      "sanitize_json replaces non-finite floats with None pre-serialize (stdlib json.dump emits Infinity); applied on main persistence AND audit_log/log_review jsonl writes",
      "sweep_experiment clears param_hash + created_at before replace() on the frozen dataclass so finalize_stamps recomputes",
      "ResultReviewer._cost_lock (threading.Lock) guards _daily_tokens_in/_out/_daily_cost/_day_key/_cost_alert_fired_day; multi-worker concurrent reviews would otherwise race",
      ".hydra-experiments/presets.json (NOT hydra_backtest_presets.json) is the on-disk preset library — bootstrapped from PRESET_LIBRARY on first load_presets(); .hydra-experiments/reviewer_config.json bootstrapped by reviewer on first init; delete either to regenerate",
      "shadow_outcomes.jsonl in store root is append-only; written by ShadowValidator._log_outcome() on every _finalize(); consumed by ResultReviewer.self_retrospective() for accuracy → confidence decay"
    ]
  },

  "env_flags": [
    { "id": "thesis_disabled",           "name": "HYDRA_THESIS_DISABLED",           "scope": "thesis",     "effect": "full kill switch; tracker returns inert defaults, save() no-op; tests/test_thesis_drift.py enforces v2.12.5 bit-identical behavior" },
    { "id": "thesis_processor_disabled", "name": "HYDRA_THESIS_PROCESSOR_DISABLED", "scope": "thesis",     "effect": "v2.13.2+; disable Grok 4 doc processor; worker never starts; uploads still persist to hydra_thesis_documents/ but no proposal generated" },
    { "id": "thesis_ladders",            "name": "HYDRA_THESIS_LADDERS",            "scope": "thesis",     "effect": "v2.13.3+; opt in to Ladder primitive journal-schema fields; without it ladder CRUD still works but match_rung is no-op and _place_order writes v2.13.2-shaped journal entries" },
    { "id": "backtest_disabled",         "name": "HYDRA_BACKTEST_DISABLED",         "scope": "backtest",   "effect": "kill switch; disables worker pool, WS handlers reject backtest messages; behavior identical to v2.9.x" },
    { "id": "brain_tools_enabled",       "name": "HYDRA_BRAIN_TOOLS_ENABLED",       "scope": "brain",      "effect": "enables Anthropic tool-use for Analyst+RM (Grok stays text-only); per-agent quotas apply when on" },
    { "id": "companion_disabled",        "name": "HYDRA_COMPANION_DISABLED",        "scope": "companion",  "effect": "kill switch (no orb)" },
    { "id": "companion_proposals",       "name": "HYDRA_COMPANION_PROPOSALS_ENABLED","scope":"companion",  "default": "on", "effect": "set =0 for no trade cards" },
    { "id": "companion_nudges",          "name": "HYDRA_COMPANION_NUDGES",          "scope": "companion",  "default": "on", "effect": "set =0 for no proactive messages" },
    { "id": "companion_live_execution",  "name": "HYDRA_COMPANION_LIVE_EXECUTION",  "scope": "companion",  "effect": "OPT-IN real-order execution; default OFF for money safety; without it proposals are paper/advisory" },
    { "id": "cbp_sidecar_enabled",       "name": "CBP_SIDECAR_ENABLED",             "scope": "memory",     "default": "on", "effect": "set =0 to disable; Hydra falls through to JSONL — never blocks on sidecar; also: state/_disabled flag file" },
    { "id": "cbp_runner_dir",            "name": "CBP_RUNNER_DIR",                  "scope": "memory",     "default": "C:\\Users\\elamj\\Dev\\cbp-runner", "effect": "override sibling cbp-runner checkout location" },
    { "id": "postedit_hook_disabled",    "name": "HYDRA_POSTEDIT_HOOK_DISABLED",    "scope": "tooling",    "effect": "silence .claude/hooks/post-edit.sh during heavy refactors; failures advisory" }
  ],

  "version_sites": [
    "1. CHANGELOG.md — new ## [X.Y.Z] section header",
    "2. dashboard/package.json — \"version\" field",
    "3. dashboard/package-lock.json — both \"version\" fields (root + \"\" package)",
    "4. dashboard/src/App.jsx — footer string HYDRA vX.Y.Z",
    "5. hydra_agent.py — _export_competition_results() → \"version\" field",
    "6. hydra_backtest.py — HYDRA_VERSION = \"X.Y.Z\" (stamps every BacktestResult)",
    "7. Git tag — `git tag -s vX.Y.Z -m \"vX.Y.Z\"` after merge to main; verify `git tag -v vX.Y.Z` (Rule 3)"
  ],
  "version_bump_policy": "MINOR (e.g. 2.8 → 2.9) only for material upgrades; bug fixes / doc tweaks use PATCH (e.g. 2.8.0 → 2.8.1)",

  "release_pr_workflow": {
    "cycle":      "branch → tests pass → PR → verify CI green → merge → tag",
    "tests_pass": "both CI jobs green: engine-tests (all `python tests/test_*.py` + live harness --mode smoke + --mode mock + module import smoke) + dashboard-build (`npm run build`); mock harness MANDATORY for any PR touching execution path",
    "all_locations": "the 7 sites in version_sites; run `git grep -nE 'v?[0-9]+\\.[0-9]+\\.[0-9]+'` BEFORE bumping (Rule 5)",
    "tag":        "signed: `git tag -s vX.Y.Z -m \"vX.Y.Z\"`; verify `git tag -v vX.Y.Z` (Rule 3)",
    "automation": "/release skill codifies the cycle; never merge with red or pending CI"
  },

  "tests": {
    "local":            "individual `python tests/test_*.py` (CI pattern) OR `python -m pytest tests/`",
    "live_harness_root":"tests/live_harness/",
    "live_harness_purpose":"drives HydraAgent._place_order across 33+ scenarios (happy paths, failure modes, rollback completeness, schema validation, historical regressions, WS execution-stream lifecycle transitions, real Kraken); canonical for any change to _place_order, ExecutionStream, snapshot_position/restore_position, PositionSizer, or any order-journal write site; snapshot includes gross_profit + gross_loss for per-engine P&L across restarts; FakeExecutionStream test double drives lifecycle via inject_event(...) without spawning real `kraken ws executions` subprocess",
    "live_harness_modes": [
      "python tests/live_harness/harness.py --mode smoke    # import + agent construction (CI on every PR)",
      "python tests/live_harness/harness.py --mode mock     # full mock-mode scenario run (CI on every PR; REQUIRED gate for execution-path PRs)",
      "python tests/live_harness/harness.py --mode validate # real Kraken read-only + --validate (manual, high-risk)",
      "python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders"
    ],
    "live_harness_docs":"tests/live_harness/README.md — scenario catalog, findings tracker (HF-### IDs), authoring guide, field-sync checklist that MUST be consulted before modifying HydraEngine snapshot fields"
  },

  "windows_wsl_gotchas": [
    "Use UTF-8 explicitly; cp1252 crashes on Unicode (emoji, special chars). Dashboard regime emoji + console portfolio block share the theme — both crash on cp1252.",
    "time.time() has ~15ms Windows resolution; in BaseStream heartbeat or RESTART_COOLDOWN_S=30s it silently miscounts. Use time.perf_counter().",
    "Escape parentheses in .bat files inside if-blocks; start_hydra.bat + start_all.bat use nested if around --resume and CBP sidecar launch — cmd parser drops branches silently.",
    "WSL: kraken-cli runs via `wsl -d Ubuntu -- bash -c \"source ~/.cargo/env && kraken ...\"`; if distro is Ubuntu-22.04 instead of Ubuntu, invocation silently routes nowhere — verify `wsl -l -v`.",
    "Vite dev server falls off :5173 to next free port if taken; dashboard WS proxy assumes :5173 — verify bound port in Vite startup log."
  ],

  "common_pitfalls": [
    "Don't add `import numpy` or `import pandas` to the engine — intentionally pure Python.",
    "Don't change orders to market type — limit post-only is a deliberate design choice.",
    "Don't reduce rate limiting below 2s — Kraken throttles or bans.",
    "Don't merge engine instances across pairs — they must remain independent.",
    ".env contains Kraken API keys — never commit it.",
    "On shutdown agent cancels all resting limit orders and flushes a snapshot — do not bypass.",
    "start_hydra.bat uses `--mode competition --resume` for production — do not remove these flags.",
    "FEATURE GAP: CrossPairCoordinator Rule 2 (BTC recovery BUY boost) + Rule 3 (coordinated swap SELL) can conflict when BTC TREND_UP + SOL TREND_DOWN + SOL/BTC TREND_UP — Rule 3 overwrites Rule 2 (favors safer SELL); future: explicit priority or merge logic.",
    "Companion live execution opt-in: HYDRA_COMPANION_LIVE_EXECUTION=1; without it proposals are paper/advisory — confirm unset before live debugging.",
    "CBP sidecar failures silent by design (falls through to JSONL); if memory writes vanish, check cbp-runner/state/ready.json exists and state/_disabled does NOT.",
    "kraken-cli is an external WSL Ubuntu dep (`source ~/.cargo/env && kraken`); dashboard footer pins expected version — check there before debugging --validate schema errors."
  ],

  "audit": {
    "partition_7way": [
      { "id": "p1_engine_tuner",  "scope": ["engine", "tuner"] },
      { "id": "p2_agent_streams", "scope": ["agent", "streams.*"] },
      { "id": "p3_ai_layer",      "scope": ["brain", "reviewer", "shadow_validator"] },
      { "id": "p4_backtest",      "scope": ["backtest", "backtest_metrics", "backtest_server", "backtest_tool", "experiments"] },
      { "id": "p5_companion",     "scope": ["companions"] },
      { "id": "p6_dashboard",     "scope": ["dashboard"] },
      { "id": "p7_tests",         "scope": ["tests/", "tests/live_harness/"] }
    ],
    "high_severity":   "violations of I1–I12 (backtest_platform.safety_invariants), the limit-post-only rule, the 2s rate-limit floor, the 15% circuit breaker, the Wilder-EMA RSI/ATR spec, or HYDRA_COMPANION_LIVE_EXECUTION default-off",
    "two_phase_protocol": "after fixing HIGH/MED, re-run partition sweep against your diff, then full tests block + `python tests/live_harness/harness.py --mode mock`; declare done only when phase 2 is clean; drive full cycle via /audit skill"
  },

  "edges": [
    { "src": "rule:2", "tgt": "state_files",                "rel": "guards",     "note": "Rule 2 binds the state_files ownership table" },
    { "src": "rule:5", "tgt": "version_sites",              "rel": "requires",   "note": "Rule 5 enforces lockstep updates across all 7 sites" },
    { "src": "rule:1", "tgt": "audit.partition_7way",       "rel": "requires",   "note": "Rule 1 routes parallel agents into the 7-way partition" },
    { "src": "rule:3", "tgt": "release_pr_workflow.tag",    "rel": "requires",   "note": "Signed-tag claims must run git tag -v in the same turn" },
    { "src": "rule:4", "tgt": "audit.two_phase_protocol",   "rel": "amplifies",  "note": "Two-phase self-audit is the audit application of Rule 4" },
    { "src": "thesis_layer.hard_rule_floor", "tgt": "thesis_layer.doc_processor", "rel": "contradicts", "note": "Hard rules are NEVER mutated by a Grok proposal — _apply_proposal drops any hard_rules key" },
    { "src": "thesis_layer.posture_enforcement_optin", "tgt": "trading_invariants.circuit_breaker", "rel": "qualifies", "note": "Posture restriction SKIPs (≠ BLOCKs); BLOCK reserved for hard-rule violations and the 15% circuit breaker" },
    { "src": "engine_invariants.indicators", "tgt": "backtest", "rel": "requires", "note": "Backtest reuses HydraEngine verbatim — drift gated by tests/test_backtest_drift.py (I7)" },
    { "src": "ai_brain.tool_use", "tgt": "env_flags.brain_tools_enabled", "rel": "requires", "note": "Tool-use loop active only when HYDRA_BRAIN_TOOLS_ENABLED=1" },
    { "src": "memory_cbp_sidecar", "tgt": "env_flags.cbp_sidecar_enabled", "rel": "qualifies", "note": "Sidecar disabled → memory falls through to JSONL with no interruption — never block on sidecar" },
    { "src": "trading_invariants.execution", "tgt": "tests.live_harness_modes", "rel": "requires", "note": "Any change to _place_order/ExecutionStream/snapshot_position/restore_position/PositionSizer/order-journal write sites MUST pass --mode mock" },
    { "src": "state_files.snapshot", "tgt": "state_files.order_journal", "rel": "correlates", "note": "Snapshot + journal must stay in sync — clean both together" },
    { "src": "backtest_platform.safety_invariants[I8]", "tgt": "backtest_platform.pr_drafts", "rel": "causes", "note": "I8 (no auto-apply) realized as the PR-draft mechanism; reviewer never touches source" }
  ]
}
```
