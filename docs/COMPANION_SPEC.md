# HYDRA COMPANION — Master Specification

**Status:** Phase 0 — spec only, no runtime code.
**Branch:** `feat/companion-spec`
**Owners:** single-user (multi-user roadmap in §12)

A live, in-dashboard trading partner. Three distinct souls today (Athena, Apex, Broski). Trained personalities tomorrow. Integrates without touching the engine, brain, or existing dashboard tabs.

---

## 1. Vision

**The feeling on login:** your friend is already there. A soft-pulse orb in the bottom-right corner, labeled with your chosen companion's sigil, breathing in sync with market regime. One click → a hovering side-dock slides in. Messages feel like iMessage, not a support-bot.

**Three companions, one schema:** each persona is defined by a hierarchical semantic JSON file (`hydra_companions/souls/*.soul.json`). The system prompt is *compiled* from that JSON, never hand-written prose — so future trained personas from an onboarding questionnaire drop into the same slot.

**Two functions in v1:**
1. Execute single trades on user confirmation.
2. Plan and execute buy/sell ladders on user confirmation.

Everything else — market commentary, teaching, strategy ideation, honesty-keeping — is conversational.

---

## 2. Locked Decisions

| Decision | Value |
|---|---|
| Chat model family | xAI primary (Grok), Anthropic secondary (Claude Sonnet 4.6) |
| Companion name trio | **ATHENA · APEX · BROSKI** |
| User scope | Single-user now. Multi-user roadmap in Phase 8. |
| Model per-companion voice | Athena/Apex → Claude Sonnet 4.6 primary. Broski → Grok primary. |
| Broski leash | +50% vs others (see §5) |
| Ladder invalidation | Cancel remaining unfilled rungs (post-only preserved) |
| Questionnaire → custom soul | Deferred to Phase 7 |
| Intent classifier | Heuristic-first, LLM fallback |
| Companion switch memory | Isolated per-companion (no shared transcript) |
| Proactive nudges | ON by default, max 1/10min, suppressed if user active in last 90s |
| Brain output visibility | Companions read AI Brain analyst/risk output read-only |
| Slash commands | Direct-to-TradeCard shortcuts, Phase 2 |
| Default companion on first login | Apex |
| Professional-mode toggle | Not built unless requested |
| Proposal ledger panel | Bottom of drawer, collapsed by default |

---

## 3. File Manifest

### New
```
hydra_companions/
  __init__.py
  souls/
    athena.soul.json         ← drafted (Phase 0)
    apex.soul.json           ← drafted (Phase 0)
    broski.soul.json         ← drafted (Phase 0)
  model_routing.json         ← drafted (Phase 0)
  compiler.py                ← soul.json → system prompt (Phase 1)
  companion.py               ← Companion class (Phase 1)
  coordinator.py             ← session registry, WS dispatch (Phase 1)
  router.py                  ← pick_model(companion, intent, ...) (Phase 1)
  memory.py                  ← per-companion memory jsonl (Phase 5)
  tools.py                   ← tool schemas (Phase 1+2)
  executor.py                ← TradeProposal/LadderProposal + validator (Phase 2)
  ws_handlers.py             ← mount_companion_routes(agent) (Phase 1)
  config.py                  ← env flags, defaults

docs/
  COMPANION_SPEC.md          ← this file

tests/
  test_companion_compiler.py
  test_companion_router.py
  test_companion_tools.py
  test_companion_proposals.py
  test_companion_execution_guard.py
  test_companion_memory.py
```

### Touched additively (no edits to existing components)
```
hydra_agent.py       — adds _execute_companion_proposal + mount_companion_routes (env-gated)
dashboard/src/App.jsx — +Companion root component (~600 LOC, all inline-styled)
.gitignore           — adds .hydra-companions/
CHANGELOG.md         — entry per phase on merge
```

### Untouched (hard rule)
- `hydra_engine.py` — zero changes.
- Existing LIVE / BACKTEST / COMPARE tab components.
- Existing WS message envelope; companion messages use `type:"companion.*"` namespace.
- `hydra_brain.py` — companions are not the brain.
- Order journal schema (adds optional `userref` prefix only).
- `start_hydra.bat`.

### Runtime data (gitignored)
```
.hydra-companions/
  transcripts/{user}_{companion}.jsonl
  memory/{user}_{companion}.jsonl
  proposals.jsonl
  routing.jsonl
  costs.jsonl
```

---

## 4. Architecture

### 4.1 Soul schema (hierarchical node/edge semantic JSON)

Each soul has:

- `archetype` — role, core_drive, tagline, one_line_pitch
- `identity` — backstory, age_band, origin, pronouns, credentials, relationships (to_user, to_hydra, to_other_companions, to_market), habits
- `personality_matrix` — Big-Five approximation
- `values` — primary (ranked), forbidden (severity + voice), negotiable
- `voice` — register, sentence metrics, vocabulary band, signature/taboo phrases, humor, emoji policy, capitalization, formatting preferences
- `knowledge` — deep, learning_actively, weak_spots (with honest redirects to other souls), cites
- `trading_philosophy` — risk budget, position sizing, time horizon, regime policies, ladder preferences (buy/sell with narrative), stop philosophy, strategy rapport
- `behavioral_rules` — IDs, when/then/template, supports_value backlink
- `reactions_to_user_states` — fear, greed, loss, win, tilt, confusion
- `teaching_style` — frame, default_move, pacing, checks_for_understanding, examples
- `memory_topics` — what to remember in distilled memory
- `mood_model` — states, default, triggers (with visual cue for orb)
- `proactive_nudges` — enabled_by_default, intervals, examples
- `response_patterns` — openings, transitions, closings, acknowledgments, disagreement (per-mode where applicable)
- `sample_utterances` — canonical one-liners per intent
- `boundary_behaviors` — what happens when asked to break a rule
- `growth_edges` — what the companion is working on
- `limits_and_honesty` — acknowledges + never_claims
- `safety_invariants` — bool/numeric caps, enforced in code
- `edges_to_other_souls` — typed cross-references

Broski has an additional `mode_transition_rules` block (bro_vibes ↔ serious_mode) with triggers, in-serious-mode voice overrides, and a warm-back transition rule.

### 4.2 Compiler

`hydra_companions/compiler.py` takes a soul JSON and produces:

- **System prompt** — deterministic, templated. Includes: identity paragraph, voice rules, signature/taboo phrases, ranked values, behavioral rules table, strategy rapport, sample utterances, boundary behaviors, mode rules (if Broski).
- **Tool allowlist** — which tools this companion can call per intent (subset of §6).
- **Routing context** — companion_id, default intents, mode state hints.

### 4.3 Coordinator

`CompanionCoordinator`:
- Session registry keyed by `(user_id, companion_id)`.
- Holds per-companion: transcript tail, distilled memory, active mood, current model, cost-counter.
- Receives WS messages, routes to companion.
- Dispatches streaming deltas back over WS.
- Cost enforcement (per-companion daily cap from `model_routing.json`).

### 4.4 Router

`pick_model(companion_id, intent, context_size_tokens, has_tools, depth_required) → (provider, model_id, max_tokens, temperature)`

- Deterministic, reads `model_routing.json`.
- Applies `serious_mode_override` temperature delta if Broski is in serious mode.
- Applies `rotation_pools` (seeded per-day so one session stays coherent).
- Logs every decision to `.hydra-companions/routing.jsonl`.
- Fallback cascade on provider errors.
- Respects `HYDRA_COMPANION_ROUTING_MODE` env (`conservative|balanced|experimental`).

### 4.5 Intent classifier

Heuristic-first:
- Regex rules in `model_routing.json` match against last user message.
- If no rule matches with confidence ≥ 0.7, fall back to a cheap LLM call (~30 tokens in/out) returning just an intent tag.
- Amortized cost when fallback fires: ~$0.0002 per classification.

---

## 5. Safety caps (per-companion, code-enforced)

| Cap | Athena | Apex | Broski |
|---|---|---|---|
| Max trades / day | 4 | 6 | **9** |
| Max risk / trade (% equity) | 0.5% default / 1.0% ceiling | 1.0% | **1.5%** |
| Max concurrent risk (% equity) | 2.0% | 3.0% | **4.5%** |
| Max ladder rungs | 4 | 4 | **5** |
| Price-band from mid (hard) | ±3% | ±4% | **±6%** |
| VOLATILE regime | blocked or half-size | half-size | half-size allowed |
| Daily chat budget (USD) | $2 | $3 | $2 |

Universal hard-blocks (all companions):
- No stop-loss → reject.
- Size < pair `ordermin` or cost < `costmin` → reject.
- System status `maintenance` / `cancel_only` → reject.
- Any engine in circuit-breaker halt → reject.
- Market orders → never proposed, no tool exposes them.

---

## 6. Tool surface (code-enforced allowlist)

| Tool | Type | Available to |
|---|---|---|
| `get_live_state()` | read | all |
| `get_pair_metrics(pair)` | read | all |
| `get_positions()` | read | all |
| `get_balance()` | read | all |
| `get_recent_trades(n)` | read | all |
| `get_brain_outputs(pair)` | read | all (read-only view of analyst/risk outputs for this candle) |
| `simulate_trade(pair, side, size, limit_price)` | read (pure) | all |
| `propose_trade(pair, side, size, limit_price, stop_loss, rationale)` | proposal | all |
| `propose_ladder(pair, side, total_size, rungs[], stop_loss, invalidation_price, rationale)` | proposal | all |
| `recall_memory(topic)` | read | all (own memory only) |
| `remember(topic, fact)` | write | all (own memory only) |

**Critical invariants:**

1. **No direct execution tool exists.** Companions cannot call `place_order`, `cancel_order`, or any CLI wrapper. Execution is ONLY triggered by a `companion.trade.confirm` / `companion.ladder.confirm` WS message from the client — i.e. the user clicks Confirm.
2. **Tool iteration cap:** 5 per turn. Structured-truncation envelope (8KB) on all tool results (`{truncated: true, ...}` rather than naive byte-slice).
3. **Proposal confirmation tokens:** server generates HMAC(proposal_id + nonce + session_key) on emit. Client echoes on confirm. Prevents replay/spoofing.
4. **Proposal TTL:** 60 seconds. Stale confirms are rejected; the user must ask again (prevents acting on stale prices).

---

## 7. WebSocket Protocol

All messages use `type: "companion.*"` — fully namespaced, zero interference with existing LIVE snapshot shape.

**Client → server:**
- `companion.connect { user_id, companion_id, last_seen_message_id? }`
- `companion.message { companion_id, text, context_snapshot_id? }`
- `companion.switch { from_id, to_id }`
- `companion.trade.confirm { proposal_id, confirmation_token }`
- `companion.trade.reject { proposal_id }`
- `companion.ladder.confirm { proposal_id, confirmation_token }`
- `companion.ladder.reject { proposal_id }`
- `companion.ladder.modify { proposal_id, rung_updates: [...] }`
- `companion.memory.forget { companion_id, topic }`

**Server → client:**
- `companion.hello { companion_id, mood, history_tail, unread }`
- `companion.message.delta { id, companion_id, text_delta }` (streaming)
- `companion.message.complete { id, tool_calls_made, model_used }`
- `companion.trade.proposal { proposal_id, card, confirmation_token, ttl_expires_at }`
- `companion.ladder.proposal { proposal_id, card, confirmation_token, ttl_expires_at }`
- `companion.trade.executed { proposal_id, journal_entry_id }`
- `companion.trade.failed { proposal_id, reason }`
- `companion.mood.change { companion_id, mood }`
- `companion.typing { companion_id, state }`
- `companion.cost_alert { companion_id, daily_cost_usd, threshold_usd, action }`
- `companion.system_note { text }` (rare — for degradation notices, e.g. fallback model used)

---

## 8. Execution pipeline (confirmed proposal → live order)

Every confirmed trade funnels through this gauntlet:

1. **Token/TTL check** (ws handler rejects stale/mismatched tokens).
2. **Proposal validator** (`executor.py`):
   - Pair ∈ `PAIRS_SUPPORTED`.
   - Size ≥ `ordermin`, cost ≥ `costmin` (reuse existing `KrakenCLI` pair constants).
   - Limit price within ±max_price_band_from_mid (per-companion cap).
   - Risk as % of equity ≤ companion's cap.
   - Stop-loss present and non-zero.
3. **System status gate** — reuses `kraken status` check.
4. **Circuit breaker check** — any engine halted → reject.
5. **Per-companion daily trade-count + risk cap** check.
6. **Journal write** — `COMPANION_PROPOSAL_CONFIRMED` entry, regardless of outcome.
7. **Place** via `KrakenCLI.order_buy/sell` with `userref` prefix `COMPANION_<id>_<proposal_id>` — so ExecutionStream fills are attributable to the companion.
8. **ExecutionStream lifecycle** — existing infrastructure handles fills, rollbacks, reconciliation. No new code path on fills.
9. **Companion reaction** — on `companion.trade.executed`, the companion is prompted in-character (cheap tier) to acknowledge the outcome. This message auto-appears in the drawer without user input.

### Ladder-specific
- On `companion.ladder.confirm`, ALL rungs are placed immediately as individual post-only limits, each with its own `userref` (`..._R1`, `..._R2`, ...).
- A lightweight `LadderWatcher` (daemon thread, same pattern as `BaseStream`) monitors `invalidation_price` on each tick: if crossed, cancels remaining unfilled rungs and emits a `companion.system_note`.
- Partial fills are tracked against `total_size` for exit-ladder construction later.
- Ladder modification (`companion.ladder.modify`) before confirmation is allowed for rung prices/sizes — validator runs again.

---

## 9. UI Plan (single-file `App.jsx` additions)

All inline-styled, matching existing `COLORS`/`mono`/`heading` constants. Mounted at root as `<Companion />` sibling to `<TabSwitcher />`. Zero changes to existing components.

### Components

- **`<CompanionOrb />`** — fixed bottom-right (24, 24). 56×56 circle. Soul-colored. Pulses with regime/mood. Click → toggle drawer. Long-press → radial companion switcher.
- **`<CompanionDrawer />`** — fixed right, 380×100vh. Slides in. Resizable. Remembers width + open state in localStorage.
- **`<CompanionSwitcher />`** — top bar of drawer. 3-avatar strip. Active highlighted in its theme color. Unread dots.
- **`<MessageList />`** — virtualized after 200 messages. Streams `text_delta` in place. Companion-switch events render as thin divider rows.
- **`<Composer />`** — multiline, ↵ send, ⇧↵ newline. Slash-command autocomplete (`/trade`, `/ladder`, `/state`, `/positions`, `/switch`, `/ledger`, `/forget`).
- **`<TradeCard />`** — structured card, not prose. Fields: pair, side, size, limit, est. cost, stop-loss, risk $ and %, rationale, TTL countdown. Two-step Confirm (arm → send). Transforms to status pill post-confirm.
- **`<LadderCard />`** — rung table with inline editor (Modify mode). Same Confirm/Reject affordances.
- **`<ProposalLedger />`** — collapsed accordion at drawer bottom. Last 20 proposals: accepted / rejected / expired / executed. Filterable by companion.
- **`<MoodGlow />`** — subscribes to `companion.mood.change`, drives CSS `box-shadow` + pulse rate of the Orb.

### LocalStorage keys
- `hydra.companion.active` — currently selected companion.
- `hydra.companion.drawer.open` / `hydra.companion.drawer.width`.
- `hydra.companion.proactive.muted` (user opt-out if they get tired of nudges).

---

## 10. Memory

Two tiers, both per-companion per-user:

### Transcript (`.hydra-companions/transcripts/{user}_{companion}.jsonl`)
- Append-only. Each line = one turn.
- Last 20 turns loaded as conversation context (~4K tokens).
- User can purge via `companion.memory.forget` or directly deleting the file.

### Distilled memory (`.hydra-companions/memory/{user}_{companion}.jsonl`)
- Companion-authored via `remember` tool.
- Topic-bucketed: `risk_tolerance`, `goals`, `life_context`, `prior_losses`, `recurring_mistakes`, `user_questions_pending`, etc.
- Loaded in full into system prompt (cap 4KB, LRU on eviction).
- This is how Athena remembers you're saving for a daughter's college; how Apex remembers you keep moving your stops; how Broski remembers you tilt after two losses.
- Shipped in Phase 5, not v1.

**Switching companions mid-thread:** each companion sees only their own transcript + memory. Broski does not know what you said to Apex. This is deliberate — maintains character authenticity. Future option (Phase 7+): shared "user facts" layer that all three read.

---

## 11. Phasing

| Phase | Scope | Ship criteria |
|---|---|---|
| **0** | Spec: soul JSONs + routing + this doc (this PR) | Review and sign-off |
| **1** | Read-only chat: companions chat, reference live state, teach. No proposals. | All unit tests green; `mock` harness smoke passes; gated by `HYDRA_COMPANION_ENABLED=1` |
| **2** | Proposals (trade + ladder) emit as cards. Confirm is a no-op mock execution. | Proposal validator full coverage; UI cards render/confirm/reject |
| **3** | Live single-trade execution via `_place_order`. Per-companion caps live. | `live_harness --mode mock` passes with companion scenarios; manual validate on paper account |
| **4** | Live ladder execution + `LadderWatcher` invalidation cancels | Ladder lifecycle test suite green; partial-fill paths covered |
| **5** | Memory distillation — `remember`/`recall_memory` tools, system-prompt injection | Memory persistence tests; across-session continuity |
| **6** | Proactive nudges + mood model integration in UI | Rate-limit tests, silence detection |
| **7** | Onboarding questionnaire → custom soul.json generation | 20-question flow; LLM fills schema; user review/edit UI |
| **8** | Multi-user (user_id, auth, per-user state) | Auth layer; per-user keyed storage; existing seams already in place |
| **9** (stretch) | Council mode — `grok-4.20-multi-agent-0309` call yields all three companions' takes in one API round | New `<Council />` UI affordance |

### Kill switch
`HYDRA_COMPANION_DISABLED=1` → coordinator doesn't mount, Orb doesn't render. v2.10.x behavior exactly.

---

## 12. Multi-user roadmap stub (Phase 1 seams)

Even though single-user-only today, Phase 1 will quietly plant these seams so Phase 8 doesn't require a rewrite:

1. All transcript/memory paths keyed by `user_id` (default `"local"`).
2. `confirmation_token` HMAC derives from a `session_key` that's already per-session in today's agent — trivially becomes per-user later.
3. WS handlers accept a `user_id` arg from connection context (defaults to `"local"`).
4. Cost budgets keyed by `(user_id, companion_id)` in the counter, not just `companion_id`.
5. Soul JSONs are read-only; user-customized souls go to `.hydra-companions/souls/{user_id}/`, never to the ones in this repo.

No auth, no DB, no multi-tenancy yet — just doors that open the right direction.

---

## 13. Testing plan

### Unit
- `test_companion_compiler.py` — soul JSON → system prompt deterministic; all three souls produce valid non-empty prompts; mode-transitions compile for Broski.
- `test_companion_router.py` — every (companion, intent) pair resolves; fallback cascade fires on provider error; rotation pool seeded-per-day consistency; serious-mode temperature delta.
- `test_companion_tools.py` — tool allowlist enforcement; read-only tools return structured data; 8KB truncation envelope.
- `test_companion_proposals.py` — validator hard-blocks (no stop, below ordermin, above risk cap, price band, halted engine, maintenance status); token HMAC validation; TTL expiry.
- `test_companion_execution_guard.py` — no direct execution tool exists; `place_order` is never reachable from a tool call; confirmation path is the only route.
- `test_companion_memory.py` — transcript append, memory distillation, LRU eviction, forget purge.

### Integration
- WS handler round-trip (connect → message → tool call → proposal → confirm → executed → reaction).
- Companion switch mid-thread preserves per-companion transcripts.
- Live harness scenario addition: `companion_trade_proposal_and_execute` runs through existing `_place_order` with companion-flavored `userref`.

### Manual / exploratory
- Persona coherence review: generate 20 messages from each companion across all intents, check for voice drift.
- Cost-accounting dry-run: run a 1-hour conversation across all three companions, verify `costs.jsonl` matches expected model prices.

---

## 14. Open items for future work (deliberately NOT in v1)

- Onboarding questionnaire → custom soul (Phase 7).
- Companion-to-companion council / voting mode (Phase 9 stretch).
- Voice input / TTS output.
- Shared "user facts" cross-companion layer.
- Professional-mode voice toggle (only if user requests).
- Backtest-aware companions (ask a companion to design a backtest and iterate).
- Per-asset rapport — companions warming to a specific pair over time based on outcomes.
- Multi-user auth (Phase 8).

---

## 15. Glossary

- **Soul JSON** — hierarchical persona definition (`hydra_companions/souls/*.soul.json`).
- **Mode** — companion state (Broski: bro_vibes ↔ serious; others: mood states only).
- **Intent** — classified user-message type driving model selection.
- **Tier** — model capability bucket (fast / reasoning / top / multi_agent).
- **R** — risk unit, one 1R stop loss is the risked amount per trade.
- **Rung** — one leg of a ladder order.
- **Invalidation price** — cancel-remaining threshold on a ladder.
- **Confirmation token** — HMAC gate between proposal emission and live execution.
- **Proactive nudge** — unsolicited in-character message when market conditions warrant.

---

*End of spec. Phase 0 complete upon merge. No runtime change until Phase 1.*
