# Changelog

All notable changes to HYDRA are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.11.1] — 2026-04-17

Dashboard polish — Strategy Matrix panel restyle.

### Changed

- **Strategy Matrix (LIVE tab, right sidebar)** — replaced single-line
  rows with pressed-in bezel cavities, one per regime, tinted by the
  regime's category color. Active pairs render as colored pill chips
  on a second line inside the cavity. Emoji strategy icons removed;
  arrow separator dropped; `opacity: 0.35` ghost rows replaced by
  color-graded active/inactive states (dim regime tint + hollow-ring
  dot when inactive, strong tint + filled glowing dot when active).
  Cavity effect via stacked `inset` box-shadows on `COLORS.bg`: top-edge
  shadow for the debossed feel, inner regime-colored glow, 1px regime
  rim, plus a subtle bottom accent on active rows.

### Fixed

- **`HYDRA_VERSION` drift** (hydra_backtest.py) — constant was stuck
  at `2.10.1` despite main sitting at `2.11.0`. Every `BacktestResult`
  was stamping a stale version. Bumped in lockstep to `2.11.1` per the
  CLAUDE.md lockstep invariant.

---

## [2.11.0] — 2026-04-17

SOL/BTC phantom-balance fix — thesis-driven confluence architecture.

### Context

On 2026-04-17 the live journal accumulated three `PLACEMENT_FAILED`
entries on SOL/BTC with `terminal_reason: insufficient_BTC_balance`.
The account holds zero BTC; only USDC. The SOL/BTC engine had been
sizing BUYs against a USD-derived "phantom" BTC balance produced by
`_set_engine_balances` splitting the USDC pool 1/N across pairs and
converting the SOL/BTC slice to BTC at the current price. Every
oversold RSI tick re-attempted the same doomed trade — the preflight
rejection rolled back engine state, so nothing learned from the failure.

### Thesis (user-authored, formally verified)

SOL/BTC is a rotation / relative-value pair. A BUY is economically
actionable only for a BTC holder (rotate BTC → SOL at a favorable
ratio); a SELL only for a SOL holder who wants BTC. For a USDC-only
portfolio, bridging (USDC → BTC → SOL) is strictly dominated by
direct SOL/USDC because a SOL/BTC signal is satisfied by either SOL
weakness OR BTC strength, and bridging a BTC leg under "BTC strength"
buys at the indicator-confirmed local high. SOL/BTC retains value as
a **confluence signal** — when SOL/BTC and SOL/USDC agree and the two
pairs are co-moving, that's stronger evidence than either alone.

### Added

- **`HydraEngine.tradable: bool`** (hydra_engine.py) — new attribute
  gating the execution path. When `False`, `_maybe_execute` and
  `execute_signal` short-circuit to `None`; the drawdown circuit
  breaker is suppressed; signal generation still runs normally so
  other pairs can consume the signal. Preserved across
  snapshot/restore (defaults to `True` on pre-2.11.0 snapshots for
  backward compatibility). Pure-Python, zero dependency change.
- **`CrossPairCoordinator` Rule 4 — BUY/SELL signal confluence**
  (hydra_engine.py): when SOL/BTC and SOL/USDC emit the same
  non-HOLD action AND their log-return correlation over the last
  60 candles exceeds `CO_MOVE_THRESHOLD` (0.5), boost SOL/USDC
  confidence by a covariance-weighted bonus capped at `+0.10`. SELL
  confluence is further gated on holding a SOL position (symmetric
  with Rule 3). Emits an `ADJUST` override with a
  `confluence_source` field carrying
  `{source_pair, rho, bonus, other_conf, window}` for traceability.
- **Covariance helpers** (hydra_engine.py): `_log_returns`,
  `pair_correlation`, `confluence_bonus` as static methods on
  `CrossPairCoordinator`. Stdlib-only (honors the no-numpy engine
  invariant). Safe on insufficient data or zero-variance series —
  returns `0.0` rather than raising.
- **`HydraAgent._refresh_tradable_flags`** (hydra_agent.py): called
  once per live tick before signal generation. Reads the latest
  `BalanceStream.latest_balances()` snapshot and flips each non-USD
  pair's `tradable` flag based on whether we hold enough of the
  quote currency to clear `PositionSizer.MIN_COST[quote]`.
  Transitions are logged exactly once. On `False → True`
  (e.g. a BTC/USDC BUY just filled), the engine's balance and
  equity baselines are re-seeded from the real holding so the
  circuit breaker starts clean. Cheap: one dict lookup per pair.
- **Journal `confluence_source` field** (hydra_agent.py
  `_build_journal_entry`): persists Rule 4 metadata at the top of
  the `decision` block so downstream analytics and the dashboard
  can surface co-movement provenance without unwrapping the
  override dict.
- **Dashboard `INFO-ONLY` badge + `ρ` confluence chip**
  (dashboard/src/App.jsx): the pair header renders a warn-colored
  `INFO-ONLY` chip when `state.tradable === false`, and the signal
  panel renders an accent-colored `ρ=0.xx ↑ +0.yyy` chip on trades
  that received a Rule 4 boost.

### Changed

- **`HydraAgent._set_engine_balances`** (hydra_agent.py) now uses the
  real exchange balance of the quote currency for non-USD-quoted
  pairs instead of a USD-derived conversion. Pairs whose quote
  balance is below the exchange `costmin` are marked
  `tradable=False`. USDC-quoted pairs continue to receive a 1/N
  slice of the tradable USDC pool. Engines with existing positions
  still compute `initial_balance = cash + position_value` so P&L
  resets cleanly.
- **Placement preflight log** (hydra_agent.py `_place_order`): when
  the real-balance check fires on a `tradable=True` non-USD pair —
  a case that should be unreachable after this release — the log
  line is now `[TRADE] Unexpected insufficient {quote} balance on
  tradable=True engine {pair} — likely BalanceStream race or
  regression` so regressions surface immediately.
- **Dashboard broadcast state** (hydra_agent.py
  `_build_dashboard_state`) attaches a `tradable: bool` to each
  per-pair state entry.

### Invariants preserved

- Backtest replay engines default to `tradable=True`; the drift
  regression (`tests/test_backtest_drift.py`, invariant I7) stays
  green without modification.
- `PositionSizer` is unchanged — its existing `balance < costmin →
  return 0.0` behavior naturally composes with `balance = 0` on
  informational-only engines.
- No changes to `_execute_coordinated_swap` or Rules 1–3 of the
  coordinator. Rule 4 is strictly additive and skips when Rule 3
  produces an override for the same pair.
- Rate limiting, limit post-only, single-file dashboard, pure-Python
  engine, one-engine-per-pair: all unchanged.

### Tests

New: `TestTradableFlag` (tests/test_engine.py), `TestRule4Confluence`
(tests/test_cross_pair.py), `tests/test_covariance.py`,
`test_sol_btc_info_only_when_no_btc` (tests/test_balance.py), plus
the corresponding live-harness scenario in
`tests/live_harness/scenarios.py`. Full regression suite remains
green.

---

## [2.10.11] — 2026-04-17

Companion subsystem — end-of-day release-readiness audit + bug pass.
Fixes four correctness bugs, removes dead props, wires up three
previously-dormant code paths, and adds six unit tests. No feature
regressions; the same 73+2 test suite is green.

### Fixed — correctness

- **Router fallback cascade** (`hydra_companions/router.py`): now walks
  the full fallback chain via `already_tried` list. Previously only
  the first candidate was tried; a double-provider failure failed the
  whole turn even with viable alternates.
- **Daily trade-count rollover** (`hydra_companions/coordinator.py`):
  `_daily_trades` now clears at UTC midnight alongside `_daily_costs`
  and `_alert_fired`. Previously trade caps persisted across days.
- **Kraken status health check** (`hydra_companions/executor.py`):
  validator now reads `agent._last_kraken_status` (the real source)
  and walks `agent.engines` for halts, instead of
  `snap.get("kraken_status")` which nothing populates.
- **UI state cross-talk** (`dashboard/src/App.jsx`): per-companion
  `useState` hooks for messages/typing/unread replace the previous
  object-keyed state. Send lock via `useRef` + message-id dedup +
  cancellable 30s timeout. Addresses the "BooM! leaks to all three
  drawers" report.

### Wired — previously-dormant code paths

- `companion.set_serious_mode` + `/serious on|off` slash command so
  Broski's router temperature delta actually has a trigger.
- `companion.nudge.mute` + `/mute [seconds]` slash command so
  proactive nudges can be silenced from the UI.
- `companion.ladder.invalidation_triggered` now rendered on the
  dashboard: ladder card flips to status "invalidated" and a system
  note lands in the thread.
- `CompanionCoordinator.notify_fill(userref)` stub for the
  ExecutionStream \u2192 LadderWatcher fill bridge.
- NudgeScheduler init now prints a full traceback on failure instead
  of silently disabling.
- `typing:idle` is now broadcast *before* `message.complete` so there's
  no sub-frame flicker where dots restart after the reply lands.

### Pruned

- `ProposalCard.onStatusReset` and `CompanionDrawer.onResize` \u2014 dead
  props (no callers, no implementations).
- Duplicate `import time` in coordinator.py.
- Unused `field`, `Path`, `os`, `json` imports across six files.

### Added — tests

- `test_fallback_cascade_walks_past_tried_candidates`
- `test_fallback_cascade_returns_none_when_exhausted`
- `test_companion_rollover.py` (UTC-midnight clears daily trades + costs)

### Git hygiene

Verified runtime artifacts are ignored across the full history:
`.hydra-companions/transcripts/*.jsonl`, `memory/*.jsonl`,
`proposals.jsonl`, `routing.jsonl`, `costs.jsonl` all covered by
`.gitignore:59`. No runtime data leaked across 24 commits.

---

## [2.10.10] — 2026-04-17

Companion UX fix \u2014 **default-on**. The orb now appears immediately
when the dashboard connects to an agent. Clicking it IS the
activation; no env var required.

### Changed
- `hydra_companions/config.py`: `is_enabled()` defaults to True
  (kill switch `HYDRA_COMPANION_DISABLED=1` still respected). Chat,
  proposals, and proactive nudges are on by default. Live execution
  stays opt-in via `HYDRA_COMPANION_LIVE_EXECUTION=1` (money safety).
  Individual features can be suppressed with `=0` env overrides.
- Dashboard: orb renders optimistically on WS connect; only hides if
  the server reports the subsystem is disabled (failed connect_ack).
- `start_hydra_companion.bat`: no longer sets env vars; chat is on
  by default. Paper mode preserved for safe testing.

### Preserved
- `start_hydra.bat` unchanged \u2014 now also shows the orb, same
  default-on behaviour.
- All 66 unit tests green.

---

## [2.10.9] — 2026-04-17

Companion **Phase 6** — proactive nudges + mood visuals. Completes the
Phase 1\u20136 core delivery arc.

### Added
- `hydra_companions/nudge_scheduler.py`: daemon that watches
  live-state transitions and pushes unprompted in-character messages.
  600 s floor between nudges; suppressed after 90 s of user activity;
  `/mute` slash command via WS.
- Dashboard: proactive messages render with a "\u00b7 unprompted" marker
  next to the companion name. Orb pulse continues to track regime
  (established in P1).
- 5 new tests; 66 unique companion tests green.

### Notes

v2.11.0 will cut on merge of the full companion branch (Phases 1\u20136)
to main as the minor-version delivery of the subsystem.

---

## [2.10.8] — 2026-04-17

Companion **Phase 5** — distilled memory. Topic-bucketed per-companion
facts loaded into the system prompt on every turn.

### Added
- `hydra_companions/memory.py` with remember / recall / forget /
  compose_block. 4KB budget, LRU-by-timestamp eviction.
- Per-companion isolation: Athena doesn't see what you told Broski.
- WS routes: `companion.memory.{remember, recall, forget}`.
- 8 new tests; 61 companion tests green.

---

## [2.10.7] — 2026-04-17

Companion **Phase 4** — LadderWatcher with invalidation cancel. 2 s
background poll monitors active ladders and cancels remaining unfilled
rungs if price crosses invalidation in the wrong direction.

### Added
- `hydra_companions/ladder_watcher.py`: LadderWatcher daemon +
  register/mark_fill/deregister.
- LiveExecutor auto-registers ladders after placement.
- `companion.ladder.invalidation_triggered` WS event for UI.
- 7 new unit tests; 53 companion tests green.

---

## [2.10.6] — 2026-04-17

Companion **Phase 3** — live single-trade execution. Gated by
`HYDRA_COMPANION_LIVE_EXECUTION=1` on top of Phases 1 + 2.

### Added
- `hydra_companions/live_executor.py`: LiveExecutor places real limit
  post-only orders via `KrakenCLI.order_buy/sell`, tagged with a
  numeric userref (int31 SHA-256 prefix of proposal_id). Existing
  ExecutionStream lifecycle handles fills unchanged.
- Coordinator now enforces per-companion daily trade cap at confirm
  time when live execution is on (mock mode still counts for
  observability). Placement failures broadcast
  `companion.trade.failed`.
- 6 new tests (userref stability, order path, failure broadcast,
  ladder distinct userrefs, daily-cap delegation). 46 companion tests
  green.

---

## [2.10.5] — 2026-04-17

Companion **Phase 2** — proposals + TradeCard/LadderCard UI with
mock execution. Gated by `HYDRA_COMPANION_PROPOSALS_ENABLED=1` on
top of Phase 1's `HYDRA_COMPANION_ENABLED=1`.

### Added
- HMAC-SHA256 proposal tokens with 60 s TTL + nonce.
- TradeProposal / LadderProposal dataclasses + hard-coded validator
  (stop-first, price-band, risk cap, Kraken ordermin/costmin,
  system-status gate). Re-validated at confirm time.
- MockExecutor: journals to `.hydra-companions/proposals.jsonl` and
  broadcasts `companion.trade.executed` so the UI renders the full
  lifecycle without touching real orders.
- Six new WS routes: `companion.propose.{trade,ladder}` +
  `companion.{trade,ladder}.{confirm,reject}`.
- **ProposalCard** (dashboard): inline-rendered in MessageList, no
  modal. TTL bar, two-step Arm \u2192 Send with 5 s auto-disarm, status
  pill transitions on submit/fill/reject/fail. Ladder variant shows
  the rung table. 12 new unit tests; 40 companion tests green.

---

## [2.10.4] — 2026-04-17

Companion subsystem — **Phase 1: read-only chat** (Athena / Apex /
Broski). Fully functional chat experience behind
`HYDRA_COMPANION_ENABLED=1`. Default OFF; with the flag unset the
subsystem is entirely inert and v2.10.3 behaviour is preserved.

### Added

- `hydra_companions/` runtime package: deterministic soul compiler,
  per-intent per-companion model router, heuristic intent classifier,
  unified xAI+Anthropic provider shim, 6 read-only tools (live state,
  pair metrics, positions, balance, recent trades, brain outputs),
  Companion class (transcript + journal), CompanionCoordinator (thread
  pool, daily USD budget tracking with 80% alert + 100% hard stop,
  UTC-midnight rollover), WS route registration.
- Agent integration: single env-gated init block in `HydraAgent.__init__`
  with try/except isolation — any init failure leaves the live agent
  completely unaffected.
- Dashboard companion UI (all inline-styled, in `App.jsx`):
  - **CompanionOrb** — 56×56 breathing orb, pulses in sync with market
    regime (fast on VOLATILE, slow on RANGING); unread dot when a
    message lands with the drawer closed; per-companion color themes.
  - **CompanionDrawer** — 380px right-side slide-in with spring easing;
    glassmorphism over the dashboard; Esc closes; persists open-state
    and width in localStorage.
  - **CompanionSwitcher** — 3-sigil strip in drawer header; one-click
    voice swap; per-companion transcripts kept isolated.
  - **MessageList** — message bubbles with companion-colored gutters;
    staggered typing indicator while the turn is in flight; auto-scroll
    to bottom on new messages.
  - **Composer** — multiline input, Enter sends, Shift+Enter newline,
    Esc closes, disabled while disconnected.
  - Cost-alert banner inside the drawer when a companion hits 80% of
    its daily USD budget.
- 28 unit tests across compiler, router, classifier, tools_readonly.
  All green.

### Notes

- Phase 1 is non-streaming — companion messages arrive as a single
  complete reply. Streaming deltas are spec'd for Phase 6.
- Phase 1 exposes no trade/ladder tools. Proposals + confirmations land
  in Phase 2 behind `HYDRA_COMPANION_PROPOSALS_ENABLED=1`.
- No changes to LIVE/BACKTEST/COMPARE tabs or existing components.

---

## [2.10.3] — 2026-04-17

Companion subsystem — **Phase 0: specification only.** No runtime code,
no engine / brain / agent behaviour changes, no dashboard changes. The
`hydra_companions/` package and spec documents land on disk but are
inert until Phase 1 wires them up (gated by `HYDRA_COMPANION_ENABLED=1`).

### Added

- **Three hierarchical semantic soul JSONs**
  (`hydra_companions/souls/{athena,apex,broski}.soul.json`) defining
  distinct trading-companion personas: archetype, identity, voice,
  values, trading philosophy, behavioral rules, reactions, teaching
  style, mood model, sample utterances, boundary behaviors, safety
  invariants, and cross-soul edges. Broski includes a dedicated
  `mode_transition_rules` block (bro-vibes ↔ serious-mode flip).
- **Model routing configuration**
  (`hydra_companions/model_routing.json`): per-intent per-companion
  selection across Grok fast-reasoning, Grok reasoning, Grok
  multi-agent, and Claude Sonnet 4.6; rotation pools; fallback cascade;
  per-companion daily USD budgets; hard safety caps (trades/day, risk %,
  price-band, ladder rungs); heuristic-first intent classifier rules.
- **Master specification** (`docs/COMPANION_SPEC.md`): vision,
  architecture, WebSocket protocol (`type: "companion.*"` namespace),
  tool surface (no direct execution tool — confirmation via
  HMAC-tokened WS messages + 60 s TTL), execution pipeline, UI plan,
  nine-phase rollout, multi-user seam plan, testing plan, kill switch.
- `.gitignore` entry for `.hydra-companions/` runtime directory.

### Rollout plan reference

Phase 1 (chat, read-only) is the next planned increment and will land
as v2.10.4 behind `HYDRA_COMPANION_ENABLED=1`. Minor-version bump
(→ v2.11.0) is deferred until the companion subsystem is fully
delivered through Phase 6 (memory + nudges).

---

## [2.10.2] — 2026-04-16

Dashboard UX patch — no engine / agent / backtest-server behaviour
changes. Full BACKTEST and COMPARE tab rework plus a handful of
defensive fixes for legacy-run metrics.

### Dashboard

- **BACKTEST tab layout:** tri-panel (`Last Result | Backtest Status |
  Rigor Gates`) above the Observer chart; chart flex-fills down to
  the control panel's bottom; clarified synthetic data source
  ("Synthetic Candles ⓘ", "Experiment Seed ⓘ") with tooltips.
- **Rigor Gates:** live pass/fail pills with plain-English labels
  (Sample Size, MC Confidence, Walk-Forward, OOS Gap, Signal vs.
  Noise, Cross-Pair, Regime Spread) driven by the review's
  `gates_passed` dict. Grey / green / red states + hover tooltips.
- **Run Status panel:** rewritten as explicit submission lifecycle
  (idle / queued / running / complete / rejected) with plain-English
  body copy per state and a purple "Compare this run →" button that
  jumps to COMPARE with the just-finished experiment pre-selected.
- **COMPARE tab:** state-aware 3-step guided banner; collapsed
  advanced filters removed; library shows only comparable
  experiments (status=complete with non-null metrics); selection
  chip bar with per-chip deselect; inline "Compare N →" button in
  the library header; library auto-hides when results are on screen
  with a "← Change Selection" dismiss; animated quantum atom icon
  on the AI Brain pill.
- **Typography unified** across both tabs (titles 14 / data 12 /
  captions 11); header controls share one 38px height; LIVE /
  BACKTEST / COMPARE tabs + AI Brain / Engine Only pill all render
  at the same footprint with equal spacing.

### Fixed

- **fix(backtest):** emit finite sentinel `999.0` for
  `profit_factor` / Sortino when denominators are zero, instead of
  `math.inf`. `_sanitize_json` was converting inf → None on disk,
  and `compare()` then crashed with "must be real number, not
  NoneType" when ranking reloaded experiments.
- **fix(compare):** None-safe `_flatten_equity` / `_rets` — legacy
  equity curves with null-sanitised ticks no longer blow up the
  paired-bootstrap p-value pass.
- **fix(compare):** server handler wraps `compare()` in try/except
  and returns a readable, actionable error message on corrupt
  legacy data (pointing the user at re-running the experiment).
- **fix(dashboard):** auto-refresh the library on every
  `backtest_result` message so freshly-completed runs are
  comparable without manual refresh or tab-switch.

### Tests

- `test_sortino_no_downside_handled` loosened to accept the `999.0`
  sentinel alongside `math.inf` / `0.0`.
- 762+ tests pass across engine / streams / backtest / reviewer /
  live-harness smoke.

## [2.10.1] — 2026-04-16

Bug-fix release: audit-driven profit-leak fixes across the brain, agent,
engine, and metrics layers. No new features. All changes are net-safer
or net-more-symmetric than v2.10.0; signal-generation changes (Fix 5 and
Fix 6) are behind a data-driven revert gate (see
`tests/_ad_hoc_fix56_backtest_compare.py`).

### Fixed

- **fix(brain):** Risk Manager `size_multiplier` is now clamped to
  `[0.0, 1.5]` at the `_run_risk_manager` boundary. Previously the
  prompt documented the range but nothing enforced it — a model
  hallucination returning `2.5` would oversize positions by 67%.
  Non-numeric values fall back to `1.0` with a log line so drift
  frequency is observable.
- **fix(agent):** `_userref_counter` now persists across restarts via
  `_save_snapshot` / `_load_snapshot`, and on startup is reseeded above
  the historical maximum seen in the order journal
  (`_reseed_userref_from_history()` with a `_USERREF_SAFETY_GAP=1000`
  buffer). The wrap-path at `_next_userref` also consults the journal
  max. Previously a restart within the same second as a killed session
  could re-issue a userref already in flight on the exchange, routing
  WS fills to the wrong journal entry.
- **fix(engine):** new `HydraEngine.reconcile_partial_fill()` corrects
  the optimistic commitment after a `PARTIALLY_FILLED` execution event.
  When the pre-trade snapshot is available (current-session fills), the
  engine restores and replays only the actual `vol_exec` portion via new
  `_apply_buy_fill` / `_apply_sell_fill` helpers — indistinguishable
  from having called `execute_signal` with the real fill amount. When
  the snapshot is unavailable (resume-path), arithmetic fallback
  adjusts balance and position with loud warning on `avg_entry` drift.
  Previously `_apply_execution_event` logged *"engine over-committed"*
  and returned, causing the engine to phantom-hold inventory and
  oversize the next signal.
- **fix(metrics):** `_block_bootstrap_sample` now uses non-circular
  (truncated) block resampling. Previously `profits[(start + j) % n]`
  wrapped tail-to-head inside a single block, which on small trade
  counts (`n ≤ ~50`) blurred temporal autocorrelation and produced CIs
  that were artificially narrow — the reviewer's `mc_ci_lower_positive`
  rigor gate passed marginal strategies that shouldn't have.
- **fix(engine):** momentum SELL now uses symmetric AND-gates (RSI in
  range AND MACD fading past noise AND price below BB mid AND
  fading-or-fresh) instead of the previous OR of just two. Preserves a
  panic-exit override at `rsi > rsi_upper + 15` (≈ 85 on default 70
  threshold). Rationale: "losing entries is just as bad as losing exits"
  — a single-indicator flip was exiting trending winners on noise.
- **fix(engine):** any SELL above `min_confidence` now full-closes the
  position. Previously a 50/50 split at `confidence > 0.7` left awkward
  partial positions that often re-triggered the "force full close"
  fallback anyway. Kelly governs ENTRY size; EXIT is binary.

### Not fixed (audit false positives documented for posterity)

These were flagged by the audit subagents but verified against source
as NOT bugs:

- Drawdown base (`peak_equity` initializes to `initial_balance`; current
  behavior is actually more conservative than the reported misreading).
- Modifier-cap "applied too late" (cap DOES clip before `execute_signal`).
- MACD `prev_histogram` recompute via `prices[:-1]` (iterative EMA
  produces identical value at position `N-2`; equivalent to prior tick).
- FOREX midnight off-by-one (hour 0 is correctly caught by the `0 <= h < 7`
  branch; else branch correctly catches 21–23).
- Backtest look-ahead (signal uses candle T close, fill at T+1 — correct
  live-mirror).

### Infrastructure

- `tests/test_partial_fill_reconcile.py`: new, 11 cases covering
  BUY/SELL × snapshot/fallback × fresh-entry/average-in.
- `tests/test_resume_reconcile.py`: added `TestUserrefPersistence` with
  8 cases for journal scan, reseed directionality, wrap handling, and
  snapshot round-trip.
- `tests/test_brain_tool_use.py`: added `TestRiskManagerSizeMultiplierClamp`
  with 5 cases for above-max, below-min, non-numeric, in-range, and
  boundary values.
- `tests/test_backtest_metrics.py`: added `test_no_circular_wrap_within_block`
  and `test_block_contents_are_consecutive` to pin the non-wrap invariant.
- `tests/test_backtest_drift.py`: neutralized `CIRCUIT_BREAKER_PCT` at
  class level to prevent halt-state divergence between direct and
  backtester paths under Fix 5/6 semantics. Drift invariant continues
  to pin signal-layer equivalence.
- `tests/_ad_hoc_fix56_backtest_compare.py`: data-driven revert gate for
  commits 5 and 6 (standalone, not pytest-collected).

All 773 tests pass. 33/33 live-harness (mock mode), including the W4
PARTIALLY_FILLED scenario which now emits *"engine reconciled to actual
fill"*.

---

## [2.10.0] — 2026-04-16

Major additive release: backtesting & experimentation platform. Zero live-agent
logic drift (guaranteed by `tests/test_backtest_drift.py`). Default behavior
with no opt-in flag is identical to v2.9.x. Full user runbook in
`docs/BACKTEST.md`; authoritative design spec in `docs/BACKTEST_SPEC.md`.

### Added

- **feat(backtest):** Phase 1 — core replay engine (`hydra_backtest.py`).
  `BacktestConfig` (frozen dataclass, JSON round-trip, auto-stamped git SHA +
  param hash + data hash + seed + hydra_version), `BacktestRunner`,
  `CandleSource` hierarchy (`SyntheticSource`, `CsvSource`,
  `KrakenHistoricalSource` with disk cache under
  `.hydra-experiments/candle_cache/` respecting the 2s Kraken rate limit),
  `SimulatedFiller` (post-only fill model matching live), `PendingOrder`,
  `SimulatedFill`, `BacktestMetrics`, `BacktestResult`. Reuses `HydraEngine`
  verbatim — only I/O is mocked.
- **feat(backtest):** Phase 2 — advanced metrics (`hydra_backtest_metrics.py`).
  `bootstrap_ci`, `monte_carlo_resample`, `monte_carlo_improvement`,
  `regime_conditioned_pnl`, `walk_forward` (in-sample train → out-of-sample
  test slices), `out_of_sample_gap`, `parameter_sensitivity`.
  Dataclasses: `WalkForwardSlice`, `WalkForwardReport`, `MonteCarloCI`,
  `MonteCarloReport`, `ImprovementReport`, `OutOfSampleReport`,
  `ParamSensitivity`. `annualization_factor` helper + `ListCandleSource`.
- **feat(backtest):** Phase 3 — experiments framework (`hydra_experiments.py`).
  `Experiment` dataclass with full JSON round-trip, `ExperimentStore` with
  `threading.RLock` (NOT Lock — delete→audit_log re-entry would deadlock),
  eight in-code presets in `PRESET_LIBRARY` (`default`, `ideal`,
  `divergent`, `aggressive`, `defensive`, `regime_trending`, `regime_ranging`,
  `regime_volatile`) bootstrapped to `.hydra-experiments/presets.json` on
  first run for user edits, `run_experiment`, `sweep_experiment`, `compare`,
  `_atomic_write_json` with recursive `sanitize_json` for non-finite floats.
  `audit_log` and `log_review` writes also run through `sanitize_json`.
- **feat(backtest):** Phase 4 — agent tool API (`hydra_backtest_tool.py`).
  Eight Anthropic tool-use schemas (`BACKTEST_TOOLS`):
  `run_backtest`, `get_experiment`, `list_experiments`, `compare_experiments`,
  `list_presets`, `get_preset`, `get_metrics_summary`, `get_engine_version`.
  `BacktestToolDispatcher.execute(tool_name, tool_input, caller)` with
  `QuotaTracker` (per_caller_daily=10, per_caller_concurrent=3,
  global_daily=50, UTC midnight reset).
- **feat(brain):** Phase 5 — tool-use integration (`hydra_brain.py` +180 LOC
  additive). New `_call_llm_with_tools()` method implements the Anthropic
  stop_reason loop with an injectable `tool_iterations_cap` (default 4) and
  an 8 KB result cap that truncates via a structured JSON envelope (not a
  naive byte-slice) so the LLM sees a `truncated:true` signal instead of
  malformed JSON. `max_tokens` terminal with pending `tool_use` blocks is
  logged rather than silently dropped. Analyst + Risk Manager branch on
  `_tool_use_enabled`; Grok Strategist stays text-only. Opt-in via
  `HYDRA_BRAIN_TOOLS_ENABLED=1`. `_call_llm` and `_parse_json` unchanged for
  fallback path.
  Two new kwargs on `HydraBrain.__init__`: `enforce_budget` (default True;
  backtest brains pass False so experiments don't stall behind a live-cost
  ceiling) and `broadcaster` (for $10/day `cost_alert` WS disclosure).
- **feat(backtest):** Phase 6 — backend bridge (`hydra_backtest_server.py`).
  `BacktestWorkerPool` (max_workers=2, 4 max, daemon threads, queue depth 20).
  `mount_backtest_routes()` wires `backtest_start`, `backtest_cancel`,
  `experiment_list_request`, `experiment_get_request`, `experiment_compare_request`,
  `review_request`. Throttled progress broadcasts (every N ticks OR 500 ms).
  Worker exceptions routed to `hydra_backtest_errors.log`. `HydraAgent.__init__`
  mounts pool + dispatcher behind `HYDRA_BACKTEST_DISABLED=1` kill switch;
  shutdown drains the pool.
- **feat(backtest):** Phase 7 — AI Reviewer (`hydra_reviewer.py`).
  Seven **code-enforced rigor gates** in `DEFAULT_GATES` dict:
  `min_trades_50`, `mc_ci_lower_positive`, `wf_majority_improved`,
  `oos_gap_acceptable`, `improvement_above_2se`, `cross_pair_majority`,
  `regime_not_concentrated`. `ResultReviewer.review()`, `batch_review()`,
  `self_retrospective()`. Five verdicts: `NO_CHANGE`, `PARAM_TWEAK`,
  `CODE_REVIEW`, `RESULT_ANOMALOUS`, `HYPOTHESIS_REFUTED`. Regime-only failure
  downgrades to scoped `CODE_REVIEW` via set-equality check (order-independent).
  LLM optional — heuristic verdict works without client.
  Tool-use loop invokes `read_source_file` (allow-list: `hydra_*.py` +
  `tests/**/*.py`; deny-list blocks `.env`, `*config*.json`, secrets, tokens;
  6 reads per review, 16 KB per file with truncation notice). `CODE_REVIEW`
  verdicts emit advisory PR drafts to
  `.hydra-experiments/pr_drafts/{exp_id}_{timestamp}.md` — I8 invariant:
  reviewer never auto-applies code changes. Cost tracking protected by a
  `threading.Lock` so multi-worker concurrent reviews don't corrupt the
  daily counter. WF/OOS run failures surface to
  `RepeatabilityEvidence.run_failures` and promote into `risk_flags` so
  gate misses are self-explaining. New kwargs: `enforce_budget` (default
  True), `broadcaster` (WS hook for `cost_alert`), `source_root` (allow-list
  root). Tunable gates + Opus pricing live in
  `.hydra-experiments/reviewer_config.json`, bootstrapped on first init.
- **feat(dashboard):** Phase 8 — tab switcher (LIVE / BACKTEST / COMPARE) +
  `BacktestControlPanel` with preset picker, pair selector, date range,
  parameter overrides. All components inline in `App.jsx`, same neon styling.
  `DashboardBroadcaster` refactor in `hydra_agent.py`: `broadcast()` now wraps
  as `{type: "state", data}`, `compat_mode=True` dual-emits raw + wrapped for
  one-release backward compatibility; `broadcast_message(type, payload)`,
  `register_handler()`, `_dispatch_inbound()`.
- **feat(dashboard):** Phase 9 — dual-state observer modal. Dockable panel
  slides in when a backtest runs (human or agent triggered); pair cards,
  equity chart, and regime ribbon render with the SAME components as live.
  Replay speed controls; cancel button. `ReviewPanel` displays verdict, gate
  pass/fail, proposed changes, accept/reject/park controls after run completes.
- **feat(dashboard):** Phase 10 — `ExperimentLibrary` (paginated, filterable,
  sortable) + `CompareResults` view highlighting winner per metric across
  2–4 experiments with significance flagging.
- **feat(shadow):** Phase 11 — `hydra_shadow_validator.py` single-slot FIFO
  live-parallel validator. `submit`, `cancel`, `reject`, `approve`,
  `rollback_last_approval`, `ingest_candle`, `record_live_close`, `tick`,
  `poll_complete`. Atomic persistence to `.hydra-experiments/shadow_state.json`.
- **feat(tuner):** `HydraTuner.apply_external_param_update(params, source)`
  for shadow-approved writes — clamps to `PARAM_BOUNDS`, rejects
  non-finite/unknown keys, records prior state in depth=1 history deque.
  `HydraTuner.rollback_to_previous()` reverts exactly one external apply
  (never cascades). Existing observation-driven tuning loop untouched.

### Tests

- +328 new tests across nine files
  (`test_backtest_engine.py`, `test_backtest_drift.py`, `test_backtest_metrics.py`,
  `test_experiments.py`, `test_backtest_tool.py`, `test_brain_tool_use.py`,
  `test_backtest_server.py`, `test_reviewer.py`, `test_shadow_validator.py`).
  All 139 legacy tests still pass. Kill switch verified via
  `tests/live_harness/harness.py --mode smoke` with `HYDRA_BACKTEST_DISABLED=1`.

### Docs

- **docs/BACKTEST_SPEC.md** — authoritative 2200+ line design spec.
- **docs/BACKTEST.md** — user-facing runbook (dashboard workflow, preset
  library, AI Reviewer gates, shadow validation flow, kill switch, brain
  tool-use opt-in, storage layout, env flags, test invocation).
- **CLAUDE.md** — new "Backtesting & Experimentation" section (module map,
  invariants, rigor gates, env flags, gotchas).

### Safety invariants (I1–I12, all enforced)

1. Live tick cadence unaffected.
2. Backtest workers construct own engine instances — never hold refs to live.
3. Separate storage (`.hydra-experiments/`) — zero writes to live state files.
4. All workers are daemon threads.
5. Every worker entry point wrapped in try/except; live loop isolated.
6. `HYDRA_BACKTEST_DISABLED=1` → v2.9.x behavior exactly.
7. Drift regression test on every commit (tick-by-tick engine parity).
8. Reviewer NEVER auto-applies code — PR drafts only.
9. Param changes require shadow validation + explicit human approval.
10. Kraken candle fetches respect 2s rate limit; disk cache prevents redundancy.
11. Worker pool bounded (2 default, 4 max); queue depth 20; 50 experiments/day;
    200k candles/experiment cap.
12. Every result stamped with git SHA, param hash, data hash, seed,
    hydra_version.

### Changed

- `.gitignore` — added `.hydra-experiments/`, `hydra_backtest_errors.log`.

---

## [2.9.2] — 2026-04-15

### Fixed

- **fix(agent):** Coordinated swap atomicity — if the buy leg cannot proceed
  after the sell has been placed on the exchange, the resting sell is now
  cancelled via `KrakenCLI.cancel_order` so the swap is not left
  half-executed. Engine rollback completes automatically when the
  CANCELLED_UNFILLED event drains through the execution stream. Pre-flight
  checks (buy engine exists, buy price > 0) also run before the sell is
  placed so common failures never reach the exchange. Paper mode logs
  unbalanced swaps (synthetic fill cannot be cancelled).
- **fix(engine):** Momentum SELL reason string used a hardcoded "> 75"
  regardless of the tuned `rsi_upper`. Now reports the actual threshold
  (`rsi_upper + 5`), so logs remain truthful after the tuner adjusts it.
- **fix(engine):** Mean-reversion HOLD confidence normalized to `BASE`
  (0.50), matching momentum/defensive. HOLD confidence is informational
  only, but the prior 0.40 value was inconsistent on the dashboard.
- **fix(engine):** `grid_spacing` fallback changed from `1` (int) to `1.0`
  (float) so downstream arithmetic stays in float domain.
- **fix(engine):** On `restore_runtime`, candles without a `timestamp`
  field are now dropped rather than being assigned `time.time()` — the
  latter silently corrupted time ordering that Sharpe and ATR-series
  calculations depend on.
- **fix(agent):** `FakeTickerStream.ensure_healthy()` now returns
  `(healthy, reason)` to match `BaseStream`'s contract (previously
  returned `None`, which would break any caller that destructured it).
- **fix(agent):** `_build_triangle_context` net BTC exposure no longer
  subtracts `pos * price` for SOL/BTC holdings. Spot-buying SOL with BTC
  is not equivalent to being short BTC — the BTC spent is already
  reflected in the account balance. BTC exposure now comes exclusively
  from BTC/USDC holdings.
- **fix(agent):** `_print_tick_status` now uses 8-decimal price precision
  for BTC-quoted pairs (SOL/BTC ~0.00148 would render as `0.0015` at
  `.4f`). Applied to price, avg_entry, last_trade price/profit in tick
  status lines.
- **fix(brain):** `_build_summary` first-sentence extraction now splits
  on `. ` (period + whitespace) instead of plain `.`, so decimals like
  "RSI at 30.5" are no longer truncated mid-number.
- **fix(dashboard):** Renamed `state` to `entryState` inside the
  `orderJournal.map` callback — the previous name shadowed the component
  state variable.
- **fix(dashboard):** Added `mountedRef` guard to WebSocket callbacks to
  prevent setState-on-unmounted warnings in StrictMode (noticeable in
  dev double-mounts).

### Docs

- **docs:** README defaults were stale and contradictory in several
  places. Updated:
  - Volatility threshold description (now adaptive 1.8× median ATR% /
    BB width, not fixed 4% / 8%)
  - Architecture diagram tick cadence (15-min candles, 300s tick)
  - `--interval` CLI default (300, not 30)
  - Competition-mode confidence threshold (65%, not 50% — matches both
    the code and the table elsewhere in README)
  - Troubleshooting entry ("needs to exceed 65%", not 55%)

---

## [2.9.1] — 2026-04-15

### Added
- **journal_maintenance.py** — standalone maintenance tool for cleaning order journal + session snapshot in lockstep. Replaces error-prone manual two-file editing procedure. Commands: `status` (audit), `purge-failed` (remove PLACEMENT_FAILED entries), `purge <index>` (remove by index). Atomic writes, dry-run support, agent-running detection via PowerShell.

---

## [2.9.0] — 2026-04-14

### Added

- **feat(brain):** Portfolio-level self-awareness — `_build_portfolio_summary()` aggregates
  cross-pair positions, P&L, regime map, and recent fills into a portfolio context injected
  into analyst and risk manager prompts. Periodic `PORTFOLIO_STRATEGIST` review via Grok
  produces portfolio-wide guidance that persists across ticks.
- **feat(agent):** Journal merge supports backfill file (`hydra_order_journal_backfill.json`)
  for manual trades — one-shot merge on startup, file renamed to `.merged` after processing.
- **feat(engine):** Adaptive volatility threshold — VOLATILE regime now fires when current
  ATR% exceeds `volatile_atr_mult` (default 1.8) × the asset's own 20-candle median ATR%.
  Same logic for BB width. Replaces fixed absolute thresholds (4% ATR / 8% BB width).
  Floor values (1.5% ATR, 0.03 BB width) prevent degenerate behavior in dead markets.
- **feat(agent):** Quality signal filtering — default candle interval changed to 15-minute
  (from 5-minute in v2.7.0), FOREX session-aware confidence modifier (London/NY overlap
  +0.04, London +0.02, NY +0.02, Asian -0.03, dead zone -0.05), subject to +0.15 total
  external modifier cap.

### Changed

- **refactor(agent):** Default tick interval changed from 30s to 300s (5 minutes) — with
  15-minute candles and push-based WS data, faster ticks added noise without new information.
  Brain fires once per new candle via `call_interval=3` (~1/3 Sonnet cost reduction).
- **fix(brain):** Brain `size_multiplier` now wired into BUY sizing path in `_apply_brain`.
- **fix(brain):** Strategist cooldown reduced from 10 to 3 ticks for faster Grok re-evaluation.

### Fixed

- **fix(brain):** Persist `ai_decision` in dashboard state across ticks — previously lost
  on ticks where brain didn't fire, causing dashboard AI panel to flicker.
- **fix(brain):** Brain pipeline over-conservatism — timing architecture revised so brain
  evaluates fresh candle data rather than stale state from previous tick.
- **fix(engine):** Realized P&L now uses average-cost-basis for sold units only (was using
  total position avg_entry × total size, overstating realized P&L on partial sells).

---

## [2.8.3] — 2026-04-14

### Bug Fix

- **fix(agent):** Add real-balance preflight check in `_place_order` for BUY orders —
  checks actual exchange quote-currency balance (via BalanceStream / cached REST) before
  burning API calls on orders that will be rejected for insufficient funds. Primarily
  affects SOL/BTC where the engine's internal BTC balance is derived from a USD split
  and may not reflect actual BTC holdings on the account. Rejects immediately with
  `insufficient_{QUOTE}_balance` journal reason, saving rate-limit budget and brain tokens.

---

## [2.8.2] — 2026-04-13

### Dashboard Reporting Fixes

- **fix(agent):** Refresh stale state dict before dashboard broadcast — when AI brain
  is active, `tick(generate_only=True)` built state before `execute_signal()` updated
  engine counters; dashboard now sees authoritative values every tick
- **fix(agent):** Add `journal_stats` to WS payload — fill counts, per-pair buy/sell
  breakdown, fill-derived win rate (cost-basis reset per round trip), realized P&L
  from journal fills, unrealized P&L from open positions, all USD-converted
- **fix(dashboard):** Top stat "Trades" → "Fills" showing confirmed exchange executions;
  win rate falls back to journal fill-derived rate when engine round trips incomplete
- **fix(dashboard):** P&L now journal-derived (realized + unrealized, USD) — cumulative
  across all trades, survives `--resume` (engine `pnl_pct` resets on restart)
- **fix(dashboard):** Max drawdown corrected from current drawdown (recovers to 0 on
  bounce) to true historical max via running-peak scan of balance history
- **fix(dashboard):** Prevent blank screen when state has no pairs (agent restart,
  candle warmup) — shows "Waiting for first tick data..." splash
- **fix(dashboard):** Fix dangling `totalPnl` reference in balance history chart that
  caused React render crash

---

## [2.8.1] — 2026-04-13

### Signal Confidence Refinement + Churn Reduction

- **fix(engine):** Replace price-scale-dependent magic numbers with ATR-normalized
  dimensionless ratios (MACD/ATR, BB penetration, volume ratio) — confidence is now
  identical across SOL/USDC, SOL/BTC, and BTC/USDC
- **fix(engine):** Momentum MACD dead zone (0.10 * ATR) + direction filter eliminates
  noise oscillations; momentum BUY signals reduced 81%, SELL reduced 52%
- **fix(engine):** Defensive SELL threshold lowered from RSI 50 to 40 (midpoint of
  TA-standard oversold/neutral) — was dead code in TREND_DOWN, now fires correctly
- **fix(engine):** Mean reversion BB width factor derived from ATR (was hardcoded 0.04);
  grid ATR-band ratio corrected to 4.0 (was 2.0)
- **fix(engine):** Remove hardcoded `price_decimals` threshold (`< 1`); use 8 decimals
  universally
- **fix(engine):** Consistent `BASE=0.50` confidence architecture with self-documenting
  weight decomposition (BASE + primary_weight + vol_weight = cap)
- **fix(brain):** Per-pair Grok strategist cooldown (10 ticks / ~5 min) to reduce
  excessive escalation overnight

---

## [2.8.0] — 2026-04-12

### XBT → BTC Canonical Migration

- **refactor(all):** Migrated internal canonical pair names from XBT to BTC
  - `SOL/XBT` → `SOL/BTC`, `XBT/USDC` → `BTC/USDC`
  - ASSET_NORMALIZE now normalizes XBT/XXBT → BTC (was BTC → XBT)
  - PAIR_MAP sends BTC slashed form to CLI natively (CLI rejects XBT slashed form)
  - WS_PAIR_MAP is now identity (canonical matches WS v2 format)
  - Legacy XBT aliases preserved for snapshot/journal migration
  - `load_pair_constants` handles Kraken's XBT-format responses via alias mapping
  - `_extract_fee_tier` handles Kraken's XBT-format fee keys via alias mapping
  - `_normalize_pair_name()` migrates old snapshot/journal data on startup
- **fix(agent):** Snapshot migration normalizes XBT pair names on `--resume`
- **chore(tests):** Updated all 15 test suites + live harness for BTC canonical
- **docs:** Updated CLAUDE.md, README.md, AUDIT.md, SKILL.md for BTC naming

---

## [2.7.0] — 2026-04-12

### Architecture: Strip REST fallbacks, WS-native tick loop

- **Tick interval: 305s → 30s** — With WS push delivering real-time candle/ticker/book/balance data, ticks no longer need to align to candle closes. 30s default gives responsive execution event processing and intra-candle price updates.
- **Removed REST fallback paths** — CandleStream, TickerStream, BookStream, and BalanceStream are now the sole data sources in the tick loop. If a stream is unhealthy, the agent skips that data source until auto-restart recovers it (typically <30s).
- **Order placement requires TickerStream** — `_place_order` refuses to trade without live bid/ask from the ticker stream. No more REST ticker fallback — if the stream is down, trading halts until it recovers.
- **Removed spread REST polling** — `_record_spreads` and `KrakenCLI.spreads()` removed. Dashboard spread display now computed from live TickerStream data.
- **Removed dead methods** — `trade_balance()`, `open_orders()`, `paper_positions()`, `order_amend()`, `order_batch()`, `depth()`, `_reconcile_pnl()` stripped from codebase.
- **Removed `_kraken_lock`** — No longer needed without REST ticker fallback in brain path.
- **Added `FakeTickerStream`** — Test double for scenarios needing controlled ticker data injection.
- **SNAPSHOT_EVERY_N_TICKS: 12 → 120** — Maintains ~1h snapshot cadence at 30s ticks.
- **Test suite: 458 tests** across 15 suites (removed test_pnl_reconcile.py).

---

## [2.6.0] — 2026-04-12

### Added
- **System status gate** — tick loop checks `kraken status` before executing;
  skips during `maintenance`/`cancel_only`, logs transitions. Degrades to
  `"online"` on API failure. Paper mode skips the check entirely.
- **Dynamic pair constants** — `kraken pairs` loaded at startup to set
  `PRICE_DECIMALS`, `ordermin`, `costmin` dynamically. Hardcoded constants
  remain as fallbacks. Corrects XBT/USDC precision (was 1, Kraken says 2).
- **Reconciliation primitives** — `KrakenCLI.query_orders()` and
  `cancel_order()` wrappers. `ExecutionStream.reconcile_restart_gap()` queries
  the exchange after auto-restart to finalize orders that filled/cancelled
  while the stream was down.
- **Resume reconciliation** — `_reconcile_stale_placed()` runs on `--resume`
  to query PLACED journal entries from previous sessions. Terminal orders
  finalized; still-open orders re-registered with the live ExecutionStream.
- **BaseStream superclass** — extracted subprocess/reader/health/restart
  infrastructure from ExecutionStream. All 5 stream types inherit from it.
- **CandleStream** (ws ohlc) — push-based candle updates for all pairs in one
  WS connection. `_fetch_and_tick()` uses stream when healthy; REST fallback
  seamless. Eliminates 3 REST calls + 6s sleep per tick.
- **TickerStream** (ws ticker) — push-based bid/ask for all pairs. Used by
  `_apply_brain` spread assessment and `_place_order` limit pricing. Eliminates
  up to 4 REST ticker calls per tick.
- **BalanceStream** (ws balances) — real-time balance updates. Dashboard state
  builder uses stream when healthy; REST polling every 5th tick as fallback.
  Normalizes XXBT/XBT→BTC, filters equities.
- **BookStream** (ws book) — push-based order book depth 10 for all pairs.
  Phase 1.75 order book intelligence uses stream when healthy; REST `depth()`
  fallback. Converts WS `{price,qty}` dicts to REST `[price,qty,ts]` format
  for OrderBookAnalyzer compatibility. Eliminates 3 REST calls + 6s sleep.
- **Order batch** — `KrakenCLI.order_batch()` wraps `kraken order batch` for
  atomic 2–15 order submission (single-pair only; Kraken API limitation).
- **P&L reconciliation** — `_reconcile_pnl()` compares journal fill data
  against `kraken trades-history`. On-demand diagnostic; not in tick loop.
- **19 new test files / test classes**, 455 total tests across 16 suites.

### Changed
- `ExecutionStream` now inherits from `BaseStream` instead of being standalone.
  API unchanged; `_dispatch` renamed to `_on_message` (internal).
- `PositionSizer.apply_pair_limits()` added for dynamic `MIN_ORDER_SIZE` /
  `MIN_COST` updates.
- `KrakenCLI.trades_history()` now accepts optional `start`/`end` time filters.
- Dashboard version bumped to v2.6.0.
- Rate-limit sleeps in tick loop are now conditional: skipped when the
  corresponding WS stream is healthy (candle, book, ticker).

### Performance
- With all WS streams healthy: ~19s/tick saved from eliminated REST calls
  and rate-limit sleeps (3 ohlc + 3 depth + ~4 ticker + balance polling).

---

## [2.5.1] — 2026-04-11

### Fixed
- **`hydra_tuner.py` silent save failures** — `ParameterTracker._save()` and
  `reset()` had bare `except Exception: pass` (same class as HF-003 in the
  trade-log writer, which was fixed in v2.5.0). A save failure — permission
  denied, disk full, read-only install dir — would let the in-memory tuner
  keep updating while the on-disk file diverged; the next restart would load
  the stale file and discard every update in between. Replaced with a logged
  warning so the outer tick-body try/except surfaces the traceback to
  `hydra_errors.log`.
- **`hydra_tuner.py` dead-code default in `update()`** — the Bayesian update
  loop used `o["params"].get(param_name, self._defaults[param_name])` inside
  a list comprehension whose surrounding filter (`if param_name in ...`)
  made the default fallback unreachable. Two contradictory intents for the
  same line. Cleaned up to just `o["params"][param_name]` with a comment
  explaining why missing observations are skipped rather than defaulted
  (defaulting would fabricate datapoints biased toward the default value).
- **`hydra_tuner.py` NaN/Inf guard** — `max(lo, min(hi, val))` propagates
  NaN silently, so a corrupted or hand-edited `hydra_params_*.json` with a
  non-finite value could poison every clamped param. Added `math.isfinite`
  checks on both the load path and the post-shift value in `update()`.
  Low-likelihood (stdlib `json.dump` refuses to emit NaN) but defensive.
- **`hydra_brain.py` OpenAI/xAI response truncation not detected** — the
  Anthropic branch of `_call_llm` logged a warning on `stop_reason == "max_tokens"`,
  but the OpenAI/xAI branch did not check `finish_reason == "length"`. A
  truncated response would silently reach `_parse_json` and fail with an
  opaque parse error; the brain would fall back to engine-only cleanly but
  the user would have no diagnostic trail. Added parity check that prints
  the provider and `max_tokens` value when truncation is detected.
- **`hydra_brain.py` conviction default bypassed escalation** — the
  strategist-escalation gate used `analyst_output.get("conviction", 1.0) <
  threshold`. If the analyst LLM returned valid JSON but omitted the
  `conviction` key, the default of 1.0 was above any reasonable threshold,
  so the strategist was never consulted. Changed the default to 0.0, which
  treats "unknown" as "low confidence → escalate" — the safer posture for
  a malformed analyst output.

### Changed
- **Dashboard WebSocket URL is now build-time configurable** via
  `VITE_HYDRA_WS_URL`. Default remains `ws://localhost:8765` so existing
  single-machine setups are unchanged. Set the env var before `npm run build`
  or `npm run dev` to point the bundled dashboard at a remote agent.

### Removed (test cleanup)
- **`test_engine.py::TestBrain::test_brain_import`** — only asserted the
  module imported. Trivially passing; would have passed even if
  `HydraBrain.__init__` were broken.
- **`test_engine.py::TestBrain::test_call_interval_caching`** — claimed to
  verify the tick-counter interval skip, but never actually advanced the
  counter to trigger the cached path. Tested nothing it named.
- **`test_tuner.py::TestShiftDirection::test_shift_rate_is_conservative`
  recomputation** — the test re-implemented `SHIFT_RATE` math inside the
  assertion (`old + SHIFT_RATE * (win_mean - old)`) and compared against
  its own calculation. Tautological: a bug that changed `SHIFT_RATE` in
  both test and production would still pass. Kept the test but replaced
  the calculation with the literal expected value `4.2`.

### Tightened
- **`test_engine.py::TestEMA::test_basic`** — previously asserted only
  `isinstance(float) and > 14.0`. A regression that replaced EMA with
  SMA-of-last-5 or with `sum(prices)` would still pass. Pinned against
  the exact expected value 17.0 (SMA seed 12.0, then five smoothing steps
  with k=1/3 on the arithmetic sequence).
- **`test_order_book.py::TestVolumeCalculation::test_top_10_only`** —
  previously used equal volume on all 20 depth levels, so the assertion
  `bid_volume == 100.0` would pass whether the analyzer capped at 10 or
  took all 20 (since top_10 * 10 = 100 and top_20 * 10 = 200, yes it
  would catch that case, but an accidental `top_n = 5` cap would also
  still match). Changed to make levels 11-20 carry `999.0` volume so any
  off-by-one or missing cap produces `10090` instead of `100`.

---

## [2.5.0] — 2026-04-11

### Added
- **KrakenCLI wrappers** — `volume()`, `spreads()`, and `order_amend()`
  thin passthroughs over the kraken CLI commands of the same name. `volume`
  is called once per hour from `_build_dashboard_state` to cache the 30-day
  fee tier; `spreads` is polled every 5 ticks in a new Phase 1.8 to maintain
  a 120-entry rolling history per pair; `order_amend` is groundwork for a
  future drift-detect repricing loop (no caller yet).
- **Fee tier + spread diagnostics on the dashboard** — compact `Fee M/T`
  pill in each pair's Indicators row showing current maker/taker fee, and
  a `Spread X.X bps (N samples)` readout below it. Inline styles, no new
  components.
- **`KrakenCLI._format_price(pair, price)`** — pair-aware price rounding
  that looks up native precision in a new `PRICE_DECIMALS` dict (SOL/USDC=2,
  XBT/USDC=1, SOL/XBT=7, etc.) and rounds before the `.8f` format. Applied
  to `order_buy`, `order_sell`, and `order_amend`. Required for any future
  code path that computes a derived price (drift→amend, maker-fee shading).
- **Live-execution test harness** (`tests/live_harness/`) — drives
  `HydraAgent._execute_trade` across 34 scenarios (happy, failure, edge,
  schema, rollback, historical regression, real Kraken) in four modes:
  `smoke`, `mock` (default, ~1.5s), `validate`, `live`. Fast mock mode
  achieved by monkey-patching `time.sleep` to no-op. Runs in CI on every
  PR as a regression gate. Surfaced HF-001 through HF-004 on its first run.
- **Findings tracker** — stable `HF-###` IDs with severity (S1-S4), status,
  fix commit, and regression test. Documented in the harness README.
- **`hydra_errors.log`** — any exception caught by the new tick-body
  try/except writes a full traceback here with timestamp. Previously
  unhandled exceptions would silently kill `run()` and force a
  `start_hydra.bat` restart with lost in-memory state.
- **61 new tests in `test_kraken_cli.py`** — TestVolumeArgsAndParsing (8),
  TestSpreadsArgsAndParsing (7), TestPriceFormat (14), TestOrderAmendArgs (9),
  TestFeeTierExtraction (9), TestRecordSpreads (11), plus the `_StubRun`
  helper and Kraken response builders reused by the harness.
- **11 new tests in `test_engine.py`** — TestHaltedEngineExecuteSignal (3)
  for HF-002, TestSnapshotTradesRoundTrip (8) for HF-004.

### Fixed
- **HF-004 (S1, active production bug)** — `trade_log` silently frozen
  across tick crashes. Two-part root cause: (a) `HydraEngine.snapshot_runtime()`
  did not include `self.trades`, so every `--resume` started with
  `engine.trades == []` while counters were restored correctly — per-pair
  P&L from trade history was silently broken; (b) the tick loop body had
  no top-level try/except, so any unhandled exception killed `run()` and
  `start_hydra.bat` restarted from the stale snapshot (saved only every
  12 ticks ≈ 1h), losing all new entries since the last successful save.
  Fix: serialize `trades[-500:]` in `snapshot_runtime`; wrap tick body in
  try/except that logs tracebacks to `hydra_errors.log` and continues to
  the next iteration; save snapshot immediately after any tick that
  appends to `trade_log`, not just on the N-tick cadence.
- **HF-003** — `except Exception: pass` in the rolling log writer
  silently swallowed every write failure. Replaced with a logged warning
  so failures become visible.
- **HF-001** — `KrakenCLI` hardcoded `.8f` price precision regardless of
  pair. Production was safe today because `_execute_trade` only passed
  `ticker["bid"]`/`ticker["ask"]` unmodified, but any derived price would
  have hit Kraken's per-pair precision rejection. Fixed via
  `_format_price` helper (see Added).
- **HF-002** — `HydraEngine.execute_signal` did not check the `halted`
  flag. Only `tick()` did, so halt was enforced via a non-local invariant
  ("`tick()` always runs first") rather than at the boundary. Any future
  caller of `execute_signal` on a halted engine would silently trade.
  Fix: `if self.halted: return None` at the top of `_maybe_execute`.
- **Dashboard fee pill null-collapse** — when `_extract_fee_tier` couldn't
  parse a fee, it stored `null`; dashboard's `(null ?? 0).toFixed(2)`
  silently rendered `"0.00%"` (misleading "zero fees" display). Fixed
  via IIFE gate that hides the pill when both sides are null and shows
  `—` for individually-null sides.
- **`order_amend` txid validation** — previously accepted `None`/`""`
  silently and burned an API slot producing an obscure Kraken error. Now
  returns a clean local error dict matching the fail-fast pattern used
  for missing `limit_price`/`order_qty`.

### Changed
- Snapshot cadence: was strictly every `SNAPSHOT_EVERY_N_TICKS` ticks
  (default 12). Now also triggers immediately after any tick whose
  `trade_log` grew, so a subsequent crash can lose at most one unsaved
  append instead of up to an hour's worth.
- CI adds a `Run live-execution harness (smoke + mock)` step to the
  `engine-tests` job (~3 seconds added to total CI time).

---

## [2.4.0] — 2026-04-05

### Added
- **Order reconciler** (`OrderReconciler`) — polls `kraken open-orders` every
  5 ticks and detects orders that disappeared (filled, DMS-cancelled, rejected).
  Prevents silent divergence between agent and exchange state.
- **Session snapshots + `--resume`** — atomic JSON snapshots of engine state,
  coordinator regime history, and recent trade log. Written every 12 ticks
  (~1h at 5-min candles) and on SIGINT/SIGTERM. `start_hydra.bat` auto-restart
  now uses `--resume` for seamless recovery.
- **Shutdown cancel-all** — `_handle_shutdown` cancels all resting limit orders
  on Kraken before exit.
- **Trade log bounding** — capped at 2000 entries to prevent unbounded growth.

### Fixed
- **Brain JSON parsing** — strip markdown code fences from LLM responses;
  increased API timeout 10s→30s and max_tokens to prevent truncation.
- **ATR smoothing** — now uses Wilder's exponential smoothing (was simple average).
- **TREND_DOWN symmetry** — `down_ratio` uses multiplicative inverse `1/ratio`.
- **Coordinated swap state sync** — sell/buy legs call `execute_signal()` on
  engines before placing Kraken orders; swap sell pairs excluded from Phase 2.5
  to prevent premature position close.
- **Swap currency conversion** — buy-leg sizing converts proceeds to buy-pair
  quote currency via XBT/USDC price when currencies differ.
- **Tuner accuracy** — records on full position close only, using accumulated
  `realized_pnl`, with `params_at_entry` preserved on Trade object.
- **Ticker freshness** — re-fetches bid/ask immediately before order placement.
- **Price precision** — 8 decimals for all prices/amounts; pair-aware rounding
  for dollar values (2 for USDC/USD, 8 for crypto pairs).
- **Candle dedup** — ticker-fallback candles get interval-aligned timestamps.
- **Sharpe annualization** — uses observed candle timestamp deltas (median)
  instead of nominal `candle_interval`.
- **Txid handling** — unwraps list-format txids from Kraken API.
- **Trade confidence** — `last_trade` dicts now include `confidence` key.
- **Competition mode** — `start_hydra.bat` uses `--mode competition --resume`.

---

## [2.3.1] — 2026-04-02

### Changed
- Order book confidence modifier range reduced from ±0.20 to ±0.07 based on Monte Carlo
  analysis (50k paths) showing Sharpe peak at ±0.07 with rapid degradation above ±0.15.
- Added total external modifier cap of +0.15 — cross-pair coordinator + order book
  combined cannot boost confidence more than +0.15 above the engine's original signal.
  Downward modifiers remain uncapped (weak signals should be killable by external data).
- When cross-pair coordinator changes signal direction (e.g., BUY→SELL override),
  the cap baseline resets to the coordinator's confidence, not the engine's original.

### Fixed
- Stacking vulnerability where cross-pair (+0.15) and order book (+0.20) could inflate
  a 0.55 engine signal to 0.90, causing Kelly criterion to oversize speculative positions.

---

## [2.3.0] — 2026-04-02

### Added
- **Self-Tuning Parameters** (`hydra_tuner.py`) — Bayesian updating of regime detection and signal generation thresholds based on trade outcomes.
  - `ParameterTracker` class tracks 8 tunable parameters: `volatile_atr_pct`, `volatile_bb_width`, `trend_ema_ratio`, `momentum_rsi_lower/upper`, `mean_reversion_rsi_buy/sell`, `min_confidence_threshold`.
  - Conservative 10% shift per update cycle toward winning trade parameter means — prevents overfitting to recent market conditions.
  - Hard bounds on all parameters (e.g., RSI thresholds clamped 10–90, ATR 1%–8%) to prevent degenerate configurations.
  - Persists learned params to `hydra_params_{pair}.json` across restarts.
  - Updates trigger every 50 completed trades or on agent shutdown.
- **Tunable engine parameters** — `RegimeDetector.detect()` now accepts `trend_ema_ratio`, `SignalGenerator.generate()` accepts RSI thresholds for momentum and mean reversion strategies.
- `HydraEngine.snapshot_params()` / `apply_tuned_params()` — snapshot and apply tunable parameter sets.
- `Position.params_at_entry` — captures parameter state at BUY time so outcomes are attributed to the correct parameter values.
- `--reset-params` CLI flag — wipes all learned parameter files back to defaults.
- 26 new tuner tests (`tests/test_tuner.py`): defaults, recording, min observations guard, Bayesian shift direction, clamping, persistence (save/load/reset/corrupt), engine integration. Total: 146 tests.

---

## [2.2.0] — 2026-04-02

### Added
- **Order Book Intelligence** (`OrderBookAnalyzer` in `hydra_engine.py`) — analyzes Kraken order book depth to generate signal-aware confidence modifiers.
  - Computes bid/ask volume totals, imbalance ratio, spread in basis points.
  - **Wall detection** — flags bid or ask walls when a single level exceeds 3x the average level volume.
  - **Confidence modifier** (−0.07 to +0.07) based on imbalance vs signal direction: bullish book boosts BUY / penalizes SELL, bearish book boosts SELL / penalizes BUY, HOLD unchanged.
- `KrakenCLI.depth()` — fetches order book depth (top 10 levels per side) via `kraken depth` command.
- Order book data injected into engine state as `order_book` key, visible to AI brain for reasoning.
- Agent Phase 1.75: fetches depth for each pair between cross-pair coordination and brain deliberation, applies confidence modifier, logs imbalance/spread/wall status.
- 31 new order book tests (`tests/test_order_book.py`): parsing (direct + nested format), imbalance ratios, spread calculation, wall detection, BUY/SELL/HOLD modifier logic, edge cases (zero volume, malformed entries, small prices). Total: 120 tests.

---

## [2.1.0] — 2026-04-02

### Added
- **Cross-Pair Regime Coordinator** (`CrossPairCoordinator` in `hydra_engine.py`) — detects regime divergences across the SOL/USDC + SOL/XBT + XBT/USDC triangle and generates coordinated signal overrides.
  - **Rule 1: BTC leads SOL down** — when XBT/USDC shifts to TREND_DOWN while SOL/USDC is still TREND_UP or RANGING, overrides SOL/USDC to SELL with 0.80 confidence.
  - **Rule 2: BTC recovery boost** — when XBT/USDC shifts to TREND_UP while SOL/USDC is TREND_DOWN, boosts SOL/USDC confidence by +0.15 (capped at 0.95) for recovery buy.
  - **Rule 3: Coordinated swap** — when SOL/USDC is TREND_DOWN but SOL/XBT is TREND_UP with an open position, generates atomic sell-SOL/USDC + buy-SOL/XBT swap with shared `swap_id`.
- **Coordinated swap execution** in `hydra_agent.py` — executes two-leg swaps (sell first, then buy) as an atomic unit with shared swap ID, logged together in the trade log.
- 22 new cross-pair tests (`tests/test_cross_pair.py`): regime history tracking, all three override rules, no-override baselines, rule priority, and Sharpe annualization fix. Total: 89 tests.

### Fixed
- **Sharpe annualization bug** — `_calc_sharpe()` used `sqrt(525600)` assuming 1-minute candles. Now uses `sqrt(525600 / candle_interval)` to correctly annualize for 5-minute or other intervals.

---

## [2.0.0] — 2026-04-02

### Added
- **3-agent AI reasoning pipeline** (`hydra_brain.py`) — Claude + Grok evaluate every BUY/SELL signal before execution.
  - **Market Analyst** (Claude Sonnet) — analyzes indicators, regime, price action; produces thesis, conviction, agreement/disagreement with engine signal.
  - **Risk Manager** (Claude Sonnet) — evaluates portfolio risk, drawdown, exposure; produces CONFIRM / ADJUST / OVERRIDE decision with size multiplier.
  - **Strategic Advisor** (Grok 4 Reasoning) — called only on contested decisions (ADJUST/OVERRIDE or conviction < 0.65). Re-evaluates with full context from both prior agents and makes the final call.
- Multi-provider support: Anthropic Claude (primary) + xAI Grok (strategist). Both keys configurable via `.env`.
- Intelligent escalation: clear CONFIRM signals skip Grok (~$0.008/decision), contested signals escalate (~$0.011/decision).
- AI reasoning displayed in dashboard: decision badges (CONFIRM/ADJUST/OVERRIDE), analyst thesis, risk assessment, Grok strategist reasoning (when escalated), risk flags.
- AI Brain sidebar panel: decisions, overrides, escalations, strategist status, API cost, latency, active/offline status.
- Header badge switches to "AI LIVE" when brain is active.
- 5-layer fallback system: single failure, repeated failures (disable 60 ticks), budget exceeded, missing API key, timeout.
- Daily cost guard (`max_daily_cost`) prevents runaway API spend.
- 8 new brain tests (fallback, budget guard, JSON parser, prompt builders, caching). Total: 62 tests.

### Changed
- Agent now routes BUY/SELL signals through 3-agent AI pipeline before execution (HOLD signals skip AI to save cost).
- Trade log includes AI reasoning when brain is active.
- Dashboard shows AI reasoning inline in each pair panel, with Grok strategist panel on escalated decisions.

---

## [1.1.0] — 2026-04-01

### Added
- **Competition mode** (`--mode competition`) — half-Kelly sizing, 50% confidence threshold, 40% max position. Optimized for the lablab.ai AI Trading Agents hackathon (March 30 — April 12, 2026, $55k prize pool).
- **Paper trading** (`--paper`) — uses `kraken paper buy/sell` commands. No API keys needed, no real money at risk. Safe strategy validation before going live.
- **Competition results export** — `competition_results_{timestamp}.json` with per-pair PnL, drawdown, Sharpe, trade log, and session metadata for submission proof.
- **Configurable position sizing** — `PositionSizer` is now an instance with configurable `kelly_multiplier`, `min_confidence`, and `max_position_pct`. Two presets: `SIZING_CONSERVATIVE` and `SIZING_COMPETITION`.
- 7 new tests: competition sizing threshold, larger positions, higher max, half-Kelly ratio, preset validation, engine mode acceptance, defaults check. Total: 54 tests.

### Changed
- `PositionSizer` refactored from static class to configurable instance — breaks no external API, all existing behavior preserved via `SIZING_CONSERVATIVE` default.
- Dead man's switch and order validation skip in paper mode (not needed).
- Agent banner shows trading mode (LIVE/PAPER) and sizing mode (CONSERVATIVE/COMPETITION).
- Default `--interval` changed to 30s (was 60s).

---

## [1.0.0] — 2026-04-01

### Added
- Core trading engine (`hydra_engine.py`) with pure Python indicators: EMA, RSI (Wilder's), ATR, Bollinger Bands, MACD (proper 9-EMA signal line)
- Four-regime detection: TREND_UP, TREND_DOWN, RANGING, VOLATILE — with priority ordering (volatile overrides trends)
- Four trading strategies: Momentum, Mean Reversion, Grid, Defensive — each with BUY/SELL/HOLD signal generation
- Quarter-Kelly position sizing with hard limits (30% max position, 55% confidence threshold, $0.50 minimum)
- Circuit breaker at 15% max drawdown — halts all trading automatically
- Live trading agent (`hydra_agent.py`) connecting to Kraken via kraken-cli (WSL)
- Limit post-only orders (`--type limit --oflags post`) — maker fees, no spread crossing
- Order validation via `--validate` before every execution
- Dead man's switch (`kraken order cancel-after 60`) refreshed every tick
- Rate limiting — minimum 2 seconds between every Kraken API call
- Three trading pairs: SOL/USDC, SOL/XBT, XBT/USDC (full coin triangle)
- WebSocket broadcast server (port 8765) for real-time dashboard communication
- React + Vite live dashboard (`dashboard/`) with:
  - Candlestick charts (80 candles per pair, responsive SVG)
  - Signal confidence meter with color-coded BUY/SELL/HOLD
  - Per-pair regime detection with strategy matrix
  - Balance history line chart
  - Scrollable trade log with status indicators
  - Kraken account balance (cached every 5th tick)
  - Session configuration panel
  - Auto-reconnecting WebSocket with connection status indicator
- Three-headed Hydra SVG favicon with purple/cyan color scheme
- Smart price formatting (`fmtPrice`) handling $0.0012 to $67,000
- Smart indicator formatting (`fmtInd`) with dynamic decimal precision
- Auto-restart launcher scripts (`start_all.bat`, `start_hydra.bat`, `start_dashboard.bat`)
- Windows Startup shortcut via `create_shortcut.ps1`
- Continuous mode (`--duration 0`) for indefinite operation
- Graceful shutdown (Ctrl+C) with final performance report and trade log export
- SKILL.md agent skill definition for Claude Code / MCP compatibility
- AUDIT.md technical audit report (49 tests, all passing)
- Cross-pair regime swap detection (advisory logging)

### Fixed
- RSI: Replaced simple sum with Wilder's exponential smoothing
- MACD: Replaced incorrect `signal = macd * constant` with proper 9-EMA of historical MACD series
- Orders: Changed from market orders to limit post-only (maker)
- Rate limiting: Added 2s sleep between every API call (was batching multiple calls instantly)
- Trade log: Now logs actual limit price instead of engine's internal price
- Dead man's switch: Now refreshed every tick (was every 2nd tick, risking expiry)
- Dashboard balance: Cached every 5th tick (was fetching every tick, wasting API calls)
- Indicator precision: Dynamic decimals based on price magnitude (fixed SOL/XBT showing 0.00)
- Continuous mode: Fixed TypeError when `remaining` was string in dashboard state
- Performance report: Replaced misaligned box-drawing characters with clean ASCII formatting
