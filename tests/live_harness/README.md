# Hydra Live-Execution Test Harness

A dedicated test harness that drives `HydraAgent._execute_trade` across every
code path — happy, failure, edge, schema, rollback, historical regression,
and real-Kraken — to catch hidden logic bugs that unit tests alone can't.

This is the **canonical validation tool** for any change that touches:
- `hydra_agent.py`: `_execute_trade`, `_execute_paper_trade`, `OrderReconciler`, the tick-loop wrapper at lines 876-909
- `hydra_engine.py`: `execute_signal`, `_maybe_execute`, `snapshot_position`, `restore_position`, `PositionSizer.calculate`
- Any trade-log write site or schema change

Every PR that touches these code paths **must** run at minimum:
- `python tests/live_harness/harness.py --mode mock` (required, 30s, zero cost)
- `python tests/live_harness/harness.py --mode validate` (recommended, 90s, zero cost)

For high-risk changes, also run:
- `python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders` (manual only, 3min, <$0.01)

## Run modes

| Mode | Cost | API calls | Duration | Coverage |
|---|---|---|---|---|
| `smoke` | $0 | none | ~5s | Imports + agent construction only |
| `mock` (default) | $0 | none (monkey-patched) | ~90s | 26 scenarios: all happy, failure, edge, schema, rollback, historical regression |
| `validate` | $0 | real Kraken read-only + `--validate` | ~30s | Real ticker fetches + real Kraken-side validation, no orders placed |
| `live` | <$0.01 | real Kraken with real post-only orders + immediate cancel | ~3min | Full `_execute_trade` end-to-end including real reconciler registration |

Live mode requires the `--i-understand-this-places-real-orders` flag as an
explicit opt-in. The harness places real post-only limit orders at deliberately
non-crossing prices (bid − 0.5%), so they cannot fill, then cancels them within
5 seconds via `kraken order cancel TXID`. Maximum possible cost: $0.00 in fees,
but network glitches could leave an order resting — the cancel loop retries up
to 3 times, and an exception handler calls `kraken order cancel-all` as a final
safety net.

## Usage

```bash
# Smoke test (import + agent construction only)
python tests/live_harness/harness.py --mode smoke

# Full mock test suite (default)
python tests/live_harness/harness.py --mode mock

# Run a single scenario
python tests/live_harness/harness.py --mode mock --scenario H3

# Verbose output
python tests/live_harness/harness.py --mode mock -v

# Machine-readable JSON report
python tests/live_harness/harness.py --mode mock --json report.json

# Live validation (real Kraken, no real orders)
python tests/live_harness/harness.py --mode validate

# Live real orders (requires explicit confirmation)
python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders
```

Exit codes: `0` = all passed, `1` = one or more scenarios failed, `2` = harness
setup error.

## Scenario catalog

### Category H — Happy paths (6)
| Code | Modes | Description |
|---|---|---|
| H1 | mock | Paper BUY SOL/USDC → `PAPER_EXECUTED` with full schema |
| H2 | mock | Paper SELL from preset position → `PAPER_EXECUTED`, position closed |
| H3 | mock | Live BUY SOL/USDC mocked, txid list unwrap, reconciler registers |
| H4 | mock | Live SELL mocked from position, total_trades incremented on close |
| H5 | live | Real post-only buy on SOL/USDC, reconciler registers, immediate cancel |
| H6 | live | SELL without position → engine correctly refuses (engine_rejected) |

### Category F — Failure paths (7)
Every F scenario runs through `_run_with_rollback_check`, which asserts that
all 13 engine fields are restored to their pre-trade snapshot via
`state_comparator.assert_rollback_complete`.

| Code | Trigger | Expected status |
|---|---|---|
| F1 | Ticker returns `{"error": ...}` | `TICKER_FAILED` |
| F2 | Ticker parses but lacks `bid`/`ask` keys | `TICKER_FAILED` |
| F3 | Validation returns post-only crossing error | `VALIDATION_FAILED` |
| F4 | Validation returns insufficient funds | `VALIDATION_FAILED` |
| F5 | Validation passes, execution returns error | `FAILED` |
| F6 | Execution times out | `FAILED` |
| F7 | Paper trade fails | `PAPER_FAILED` (no rollback needed) |

### Category E — Edge cases (7)
| Code | What it tests |
|---|---|
| E1 | Txid returned as list `{"txid": ["ABC"]}` → unwrapped to scalar |
| E2 | Txid nested under `{"result": {"txid": "ABC"}}` → extracted via fallback |
| E3 | Txid missing entirely → becomes `"unknown"`, reconciler skips |
| E4 | Txid empty list `{"txid": []}` → becomes `"unknown"` |
| E5 | Halted engine tick() returns HOLD with halt reason (production path) |
| E6 | Ordermin partial sell forces full close (commit 35a134d fix) |
| E7 | Unparseable Kraken JSON response → `FAILED` + rollback |

### Category S — Schema meta (1)
| Code | What it tests |
|---|---|
| S0 | The schema validator itself rejects: missing fields, wrong types, wrong order_type for status |

Schema compliance for production entries is enforced implicitly by every H/F/E
scenario calling `validate_entry(entry, expected_status=...)` on the resulting
trade log entries.

### Category R — Rollback meta (1)
| Code | What it tests |
|---|---|
| R0 | The rollback comparator catches tampered state (meta-test for `assert_rollback_complete`) |

Rollback completeness for production code is enforced implicitly by every F
scenario running through `_run_with_rollback_check`.

### Category H′ — Historical regression (6)
Each verifies a past bug fix stays in place.

| Code | Commit | Bug |
|---|---|---|
| Hp1 | `4effbea` | Falsy-zero `competition_start_balance` restoration must use `is not None` |
| Hp2 | `4effbea` | `_pre_trade_snapshot` must be stripped from dashboard broadcast |
| Hp3 | `88797ca` | BUY must NOT increment `total_trades` (only SELL close does) |
| Hp4 | `88797ca` | Break-even (P&L=0) must count as loss, not win |
| Hp5 | `9e652d5` | Kraken txid returned as list must be unwrapped |
| Hp6 | `35a134d` | Partial sell below ordermin must force full close |

### Category L — Live only (6)
Real Kraken API calls. Only run in `--mode validate` or `--mode live`.

| Code | Modes | Description |
|---|---|---|
| L1 | validate, live | Real ticker fetch for SOL/USDC, verify response shape |
| L2 | validate, live | Real `--validate` buy for SOL/USDC at ordermin, verify success |
| L3 | live | Real post-only buy on SOL/USDC + immediate cancel, full path including reconciler |
| L4 | live | L3 for XBT/USDC |
| L5 | live | L3 for SOL/XBT |
| L6 | validate | Real `--validate` below costmin → Kraken rejects |

## How the harness works

### Isolation guarantees

The harness applies three layers of isolation so it cannot corrupt the user's
real state files or interfere with a running agent:

1. **No `run()` call, ever.** The harness drives `_execute_trade` directly. The
   rolling log file `hydra_trades_live.json` is written only by the main tick
   loop (`hydra_agent.py:961`), not by `_execute_trade`. Same for
   `hydra_session_snapshot.json` (written by `_save_snapshot` on shutdown).
2. **Tuner save neutralized.** `ParameterTracker._save` is monkey-patched to a
   no-op on every tracker after agent construction, so the harness cannot
   overwrite the user's learned `hydra_params_*.json` files.
3. **Brain disabled.** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `XAI_API_KEY`
   are unset in `os.environ` before any agent is constructed, so `HydraBrain`
   is guaranteed `None` (see `hydra_agent.py:524-537`). This eliminates LLM
   nondeterminism and prevents the harness from making any LLM API calls.

Additionally, `DashboardBroadcaster.start()` is monkey-patched to a no-op as
a defensive measure, though `HydraAgent.__init__` does not call it anyway.

### The execute wrapper

`harness_execute()` in `harness.py` reproduces the tick-loop wrapper from
`hydra_agent.py:876-909` exactly:

1. Snapshot engine state via `engine.snapshot_position()`
2. Call `engine.execute_signal(action, confidence, reason, strategy)` which
   mutates engine state and returns a `Trade` object
3. Construct a trade dict and call `agent._execute_trade(pair, trade_dict)`
4. If `_execute_trade` returns `False`, call `engine.restore_position(pre_snap)`
   to roll back the engine state

The returned report dict has `outcome` ∈ `{success, failed_and_rolled_back,
engine_rejected}`, plus `pre_snap`, `trade`, `trade_dict`, and
`last_trade_log_entry` for post-scenario assertions.

### Stubbing Kraken

`stubs.py` provides:
- `StubRun` — installs a monkey-patched `KrakenCLI._run` that dispatches to
  canned responses. Responses can be a static dict (same for every call),
  a list (iterated in order), or a callable dispatcher (routes by arg keyword).
- `build_dispatcher({"ticker": ..., "order_validate": ..., "order": ...})` —
  convenience builder that routes by command keyword.
- Response builders: `kraken_ticker`, `kraken_order_success_list`,
  `kraken_order_error`, `kraken_paper_success`, `kraken_validate_success`,
  etc. — every response shape used by the scenarios.

Every `StubRun.install()` must be paired with `.restore()` via `try/finally`
so a failing assertion doesn't corrupt sibling scenarios.

### The 13-field rollback comparator

`state_comparator.py` exposes `assert_rollback_complete(before, after,
scenario_name)` which raises `RollbackDiff` on any mismatch among these
fields (mirroring `hydra_engine.py:1077-1110`):

1. `balance`
2. `position.size`
3. `position.avg_entry`
4. `position.realized_pnl`
5. `position.params_at_entry`
6. `total_trades`
7. `win_count`
8. `loss_count`
9. `len(trades)`
10. `len(equity_history)`
11. `peak_equity`
12. `max_drawdown`
13. `halted`

If a future PR adds an analytics field to `HydraEngine`, it MUST be added to
both `snapshot_position`/`restore_position` in `hydra_engine.py` AND to the
capture list in `state_comparator.py` for rollback completeness to remain
provably correct.

### The trade log schemas

`schemas.py` defines per-status schemas for every `trade_log.append` site in
`hydra_agent.py`. The validator (`validate_entry`) checks:
- All required fields are present
- Each field's type matches the schema
- Special constraints per status (`order_type=="limit post-only"` for EXECUTED,
  `result is None` for FAILED, etc.)
- `time` is a valid ISO 8601 timestamp

The schemas intentionally reflect the known gap that `TICKER_FAILED` and
`VALIDATION_FAILED` entries omit `reason`, `confidence`, and `order_type` —
those entries are written before the trade gets past pre-flight checks, so
those fields haven't been resolved yet. See `hydra_agent.py:1145-1170`.

## Findings tracker

Every issue discovered while building or running the harness is tracked here
with a stable `HF-###` ID. Each finding has a severity, current status, the
production-today impact, and a recommended fix. When a fix lands, update the
status and link the PR.

Every finding **must** have at least one scenario that will regress if the
bug comes back — or an explicit note saying why regression coverage isn't
feasible yet. This is how we preserve "continuity of the trade process"
across upgrades.

### Severity legend

- **S1 — critical**: active production bug causing incorrect trades, silent
  data loss, or unsafe fund movement. Must block any PR until fixed.
- **S2 — latent**: production is safe *today* but a foreseeable future change
  will hit this. Block any PR that would trigger it until fixed.
- **S3 — defensive**: no current or foreseeable path reaches the bug, but the
  code lacks defense-in-depth. Should be fixed opportunistically.
- **S4 — cosmetic**: display/logging/documentation drift with no functional
  impact.

### Tracked findings

| ID | Title | Sev | Status | Regression test | Links |
|---|---|---|---|---|---|
| HF-001 | KrakenCLI hardcodes `.8f` price precision | S2 | Open | L2 (validate mode) — will regress if fix breaks L2 | See HF-001 below |
| HF-002 | `execute_signal` bypasses halt check | S3 | Open | E5 (via production `tick()` path) — will need extension when fix lands | See HF-002 below |
| HF-003 | Silent `except Exception: pass` in rolling log writer | S3 | Open | Will regress if a fix loses the bare exception → needs unit test of the write path with a mocked I/O failure | See HF-003 below |
| HF-004 | **Trade persistence silently failing in the real agent** — `trade_log` and `engine.trades` not growing despite real trades executing on Kraken | **S1** | **ACTIVE IN PRODUCTION** — user's agent has been losing trades for 12+ hours as of discovery | None yet — requires the harness to gain a `run()`-loop scenario that detects snapshot-vs-reality drift | See HF-004 below |

---

### HF-001 — KrakenCLI hardcodes `.8f` price precision

**Severity**: S2 (latent, production safe today, blocks item #4)
**Status**: Open. No fix PR yet.
**Regression test**: Scenario `L2` (validate mode). If a fix changes the
`order_buy`/`order_sell` price formatting behavior and L2 starts failing
against real Kraken, we know the fix broke precision handling.
**Discovered**: First validate-mode run of harness, 2026-04-11.

`hydra_agent.py:211, 226` format the price as `f"{price:.8f}"` regardless of
the pair's native precision. Kraken rejects orders whose price has more
meaningful decimals than the pair allows:

| Pair | Max decimals | Example reject |
|---|---|---|
| SOL/USDC | 2 | `80.47450000` (4 meaningful) → `EOrder:Invalid price:SOL/USDC price can only be specified up to 2 decimals.` |
| XBT/USDC | 1 | `73010.52000000` (2 meaningful) → similar reject |
| SOL/XBT | 7 | `0.00110475` (8 meaningful) → similar reject |

**Why production is safe today**: `_execute_trade` at `hydra_agent.py:1153,
1177` uses `ticker["bid"]`/`ticker["ask"]` **unmodified**. Those values come
from Kraken at native precision, round-trip through Python float cleanly, and
format back with trailing zeros that Kraken ignores.

**Why this blocks item #4**: the drift→amend repricing loop will mathematically
derive a new limit price from `mid ± drift`. That introduces extra decimals.
The current `.8f` format will produce a price Kraken rejects. Item #4 cannot
ship until this is fixed.

**Other future paths this blocks**: maker-fee optimizer shading prices by
basis points, any tick-aware mid-crossing logic, any price smoothing between
ticker fetch and order placement.

**Recommended fix** (separate PR, <50 LOC):
1. Add a `PRICE_DECIMALS` dict to `KrakenCLI`, keyed by friendly pair name.
   Initial values verified against Kraken's `pairs` endpoint: `{"SOL/USDC": 2,
   "XBT/USDC": 1, "SOL/XBT": 7, "BTC/USDC": 1, "BTC/USD": 1, "SOL/BTC": 7}`.
2. Add a `_round_price(pair, price)` helper that looks up the precision and
   calls `round(price, decimals)`.
3. Replace `f"{price:.8f}"` with `f"{KrakenCLI._round_price(pair, price):.8f}"`
   in `order_buy`, `order_sell`, and `order_amend`.
4. Add a new test in `tests/test_kraken_cli.py` covering each pair's
   precision handling.
5. Regression: L2 continues to pass in validate mode.

Harness scenario L2 initially tried `bid * 0.95` and hit this error on its
first validate-mode run. It now uses `ticker["bid"]` directly, mirroring the
production-safe pattern.

---

### HF-002 — `engine.execute_signal` bypasses halt check

**Severity**: S3 (defensive, no current or foreseeable path reaches the bug)
**Status**: Open. No fix PR yet.
**Regression test**: Scenario `E5` currently tests the production path via
`engine.tick()`. When the fix lands, E5 should be extended to also call
`execute_signal` directly on a halted engine and assert it returns `None`.
**Discovered**: Scenario E5 initial implementation surfaced the gap, 2026-04-11.

Only `engine.tick()` checks the `halted` flag (early return at
`hydra_engine.py:866-868`). In production, `tick()` is always called first
in the main loop, so `execute_signal` is never reached on a halted engine.

**Why production is safe today**: The tick loop at `hydra_agent.py:~720`
always calls `engine.tick()` before any subsequent phase. A halted engine
returns HOLD from `tick()`, which prevents any downstream call to
`execute_signal`. The swap handler at `hydra_agent.py:1337` does call
`execute_signal` directly, but only after observing a non-HOLD signal
from the coordinator — which can only exist if the engine already passed
through `tick()`.

**Why this is still worth fixing**: defense-in-depth. If ANY future code
path calls `execute_signal` on a halted engine (e.g., a new strategy
implementation, a manual trade button, a brain override), the halt is
silently bypassed. The current design relies on a non-local invariant
("`tick()` always runs first") that isn't enforced at the `execute_signal`
boundary.

**Recommended fix** (separate PR, one-line + test):
1. Add `if self.halted: return None` at the top of
   `HydraEngine._maybe_execute` (around `hydra_engine.py:912`).
2. Extend scenario E5 to call `execute_signal` directly on a halted engine
   and assert it returns `None`.
3. Extend test_engine.py with a unit test for the halt check.

---

### HF-003 — Silent `except Exception: pass` in rolling log writer (INVESTIGATING)

**Severity**: TBD (background agent diagnosing)
**Status**: Investigating. See background agent task `a20ade49...`.
**Regression test**: None yet — pending diagnosis.
**Discovered**: User reported "I don't see anything coming through the trade
log" after running the harness, 2026-04-11.

The rolling log write at `hydra_agent.py:959-966`:

```python
if self.trade_log:
    rolling_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hydra_trades_live.json")
    try:
        with open(rolling_file, "w") as f:
            json.dump(self.trade_log, f, indent=2)
    except Exception:
        pass  # !! silently swallows all errors
```

**The concern**: if the write fails for any reason (permission, disk full,
path resolution, file lock from a parallel process), the user sees no log,
no warning, nothing. The agent keeps running as if nothing is wrong.

**Context**: the harness is by design in-memory only, so the harness cannot
diagnose this. But the user reports that even the real agent's trade log
hasn't been updating — suggesting a silent failure.

**Background investigation in progress** covers: file mtime/parse check,
parallel process detection, Kraken-side reconciliation (`closed-orders`,
`trades-history`, `ledgers`), code audit, and hypothesis ranking. Findings
will update this entry.

**Provisional recommended fix** (pending diagnosis):
1. Replace `except Exception: pass` with logged warning: `except Exception
   as e: print(f"  [WARN] rolling log write failed: {e}")`. At minimum, the
   failure becomes visible.
2. Add a startup health check that writes a dummy entry and verifies it
   reads back, aborting with a clear error if the write path is broken.
3. If the investigation reveals a concurrency issue (parallel agent process
   write-locking the file), consider moving to append-mode writes or a
   file-per-session pattern.

---

## Continuity protocol

This section codifies WHEN and HOW to use the harness as the codebase
evolves. The goal is zero trade-process regressions across all future
upgrades.

### When to run the harness

| Change touches... | Required mode(s) | Rationale |
|---|---|---|
| Any line of `_execute_trade`, `_execute_paper_trade` | **mock** (required) + **validate** (required) + **live** (required for high-risk) | Core execution path — must verify every branch + schema + rollback + real-Kraken compatibility |
| `OrderReconciler` (register, maybe_reconcile, known_orders) | **mock** (required) + **live** (recommended) | Affects txid tracking — mock covers logic, live verifies real txid registration |
| `engine.execute_signal`, `_maybe_execute`, `snapshot_position`, `restore_position` | **mock** (required) | Touches the rollback fields. E1-E7 + F1-F7 will catch regressions |
| `PositionSizer.calculate` | **mock** (required) | Changes sizing → harness scenarios verify trades are sized correctly per pair |
| Trade log schema (any `trade_log.append` site) | **mock** (required) | Schema validator catches field drift |
| Kraken CLI wrappers (`KrakenCLI.order_buy`/`order_sell`/`order_amend`/`ticker`) | **mock** (required) + **validate** (required) | Changes to argument construction or response parsing |
| Signal generation, regime detection, indicators | **mock** (optional) | Not on the execution path — existing test_engine.py is the primary gate |
| Dashboard, docs, tests | None | Out of harness scope |
| Any of the above PLUS modifications to fields in `snapshot_position` | **mock** (required) + ALSO update `state_comparator.py` capture list | Rollback comparator must know about new fields |

### How to respond to a harness finding

1. **Never silence a finding by weakening an assertion.** If a scenario fails,
   fix the underlying bug or document why the assertion is wrong.
2. **Every bug found becomes an HF-### entry** in the findings tracker
   above, with severity, status, regression test, and recommended fix.
3. **S1 findings block the PR entirely.** S2 findings block any PR that would
   trigger the latent path. S3 findings are documented and addressed
   opportunistically. S4 findings are logged but not blocking.
4. **A fix that closes a finding must include or reference a regression
   test** — typically a new or extended harness scenario. Update the finding
   entry's "Regression test" field when the fix lands.

### How to add new scenarios as the code path grows

When a PR adds a new branch in `_execute_trade` (new status, new failure
mode, new edge case), the PR must also add a harness scenario that
exercises the new branch. See "Adding new scenarios" section below.

### Pre-merge checklist for any PR touching the execution path

- [ ] `python tests/live_harness/harness.py --mode mock` — all scenarios pass
- [ ] `python tests/live_harness/harness.py --mode validate` — all scenarios pass (if change touches Kraken CLI arguments or parsing)
- [ ] `python tests/live_harness/harness.py --mode live --i-understand-this-places-real-orders` — all scenarios pass (if change is high-risk per the table above)
- [ ] All existing tests pass: `python tests/test_engine.py && tests/test_cross_pair.py && tests/test_order_book.py && tests/test_tuner.py && tests/test_balance.py && tests/test_kraken_cli.py`
- [ ] New branches in `_execute_trade` have a corresponding new scenario in `scenarios.py`
- [ ] New fields in `snapshot_position` are also added to `state_comparator.py`
- [ ] New trade_log statuses have a corresponding schema entry in `schemas.py`
- [ ] Any new finding is logged in the findings tracker with an HF-### ID
- [ ] Any closed finding has its status and regression test link updated

## Scenario authoring guide

Writing a new scenario is ~20 lines. Every scenario is a single function that
takes a `Harness` instance, raises on failure, and returns on success.

### Minimal happy-path template

```python
def scenario_H9_your_scenario_name(h: Harness):
    """One-sentence description of what this scenario verifies."""
    agent = h.new_agent(pairs=["SOL/USDC"], paper=True, initial_balance=200.0)
    h.seed_candles(agent, "SOL/USDC", base_price=100.0)

    stub = StubRun(build_dispatcher({
        "paper_buy": kraken_paper_success(),
    })).install()
    try:
        report = harness_execute(agent, "SOL/USDC", "BUY", 0.75, "H9 description")
    finally:
        stub.restore()

    assert report["outcome"] == "success"
    validate_entry(report["last_trade_log_entry"], expected_status="PAPER_EXECUTED")
```

### Minimal failure-path template (with rollback check)

```python
def scenario_F8_your_failure(h: Harness):
    """One-sentence description of the failure branch being tested."""
    _run_with_rollback_check(
        h, "F8",
        setup_stub=lambda: StubRun(build_dispatcher({
            "ticker": kraken_ticker("SOL/USDC", bid=100.0, ask=100.1),
            "order_validate": kraken_validate_success(),
            "order": kraken_order_error("EOrder:Your specific error here"),
        })),
        action="BUY", confidence=0.75,
        expected_status="FAILED",
    )
```

`_run_with_rollback_check` automatically captures the engine state before
the trade, runs it, asserts the expected status, and verifies all 13
rollback fields match the pre-snapshot.

### Registration

At the bottom of `scenarios.py`, add your scenario to `ALL_SCENARIOS`:

```python
Scenario("H9", "Your scenario description", "H", MOCK, scenario_H9_your_scenario_name),
```

Fields:
- `code`: stable identifier (`H9`, `F8`, etc.)
- `name`: human-readable title shown in output
- `category`: one letter `H`/`F`/`E`/`S`/`R`/`H_prime`/`L`
- `modes`: `MOCK` / `LIVE` / `LIVE_ONLY` / `VALIDATE_ONLY` — controls which `--mode` runs it
- `fn`: the scenario function

### Scenario code naming convention

Stable identifiers — **never reuse a code after a scenario is deleted or
repurposed**. Future reviewers read commit messages that reference "H5
regressed" and must find the same logic.

| Prefix | Category | When to use |
|---|---|---|
| `H*` | Happy path | Normal success flow |
| `F*` | Failure path | Triggers a specific failure branch in `_execute_trade`, expects rollback |
| `E*` | Edge case | Unusual inputs, boundary conditions (txid shapes, ordermin, halted engine) |
| `S*` | Schema meta | Validator sanity checks |
| `R*` | Rollback meta | Comparator sanity checks |
| `Hp*` | Historical regression | Named for a commit that introduced the original fix |
| `L*` | Live only | Real Kraken API — only runs in validate/live modes |

### The field-sync checklist — READ THIS BEFORE MODIFYING `HydraEngine`

The rollback comparator in `state_comparator.py` has a hardcoded list of
engine fields it captures. If you add a new field to `HydraEngine` that is
**included in `snapshot_position()` or `snapshot_runtime()`**, you MUST also
add it to `capture_engine_state()` in `state_comparator.py` in the SAME PR.

Missing fields in the comparator silently pass rollback tests even when the
rollback is incomplete — exactly the class of bug commit `4effbea` fixed.

**Current field list** (hydra_engine.py → state_comparator.py must agree):

| Field | In `snapshot_position` | In `snapshot_runtime` | In comparator |
|---|---|---|---|
| `balance` | ✓ | ✓ | ✓ |
| `position.size` | ✓ | ✓ | ✓ |
| `position.avg_entry` | ✓ | ✓ | ✓ |
| `position.realized_pnl` | ✓ | ✓ | ✓ |
| `position.params_at_entry` | ✓ | ✓ | ✓ |
| `total_trades` | ✓ | ✓ | ✓ |
| `win_count` | ✓ | ✓ | ✓ |
| `loss_count` | ✓ | ✓ | ✓ |
| `len(trades)` | ✓ | ✓ (HF-004 restores contents) | ✓ |
| `len(equity_history)` | ✓ | ✓ | ✓ |
| `peak_equity` | ✓ | ✓ | ✓ |
| `max_drawdown` | ✓ | ✓ | ✓ |
| `halted` | — | ✓ | ✓ |

When adding a new field, add a row to this table in the same PR that adds
the field. That's how this table stays truthful.

## File layout

```
tests/live_harness/
├── __init__.py          — Package marker
├── harness.py           — Harness class, CLI entry, harness_execute helper
├── scenarios.py         — All scenarios + ALL_SCENARIOS registry
├── stubs.py             — StubRun + Kraken response builders
├── state_comparator.py  — 13-field rollback comparator
├── schemas.py           — Per-status trade log entry schemas + validator
└── README.md            — This file
```
