# WS Execution Stream Conversion — Handoff

> **Status:** in-progress on branch `feat/ws-execution-stream` (off main @ 5e3b673).
> **Paused:** 2026-04-11, mid-implementation, after spike validation and design lock-in but *before* any code changes to `hydra_agent.py`.
> **Purpose:** A future Claude session (or the user) should be able to pick this
> doc up cold and execute the conversion to completion without re-deriving any
> context. Every decision is locked, every file is listed, every edit is
> specified.

## Why this work exists

Follow-up to PR #40 (trade-log persistence), driven by the 2026-04-11 trade
history audit. The user directed a full conversion — not a staged one — to
move Hydra's order reconciliation onto `kraken ws executions`, delete the REST
`OrderReconciler` poll loop, reshape the local trade log so Kraken owns material
fill details (price/vol/cost/fee) and the bot owns only decision context
(strategy, regime, confidence, brain verdict, overrides) + lifecycle pointers,
and rename everything for consistency so "you couldn't tell the conversion
happened."

Plan predecessor: `docs/RECONCILER_PLAN.md` — historical context only. This
handoff supersedes it.

## Direction from the user (verbatim paraphrase)

- No dual paths. Delete the REST reconciler entirely.
- No feature flag. WS is the only reconciler.
- Kraken CLI WebSocket features are well-vetted — trust and use them.
- Local log stores only what Kraken doesn't: decision context, minimum
  summary for P&L (vol_exec + avg_fill_price + fee_quote).
- Naming consistent top-to-bottom: no `trade_log`, no `EXECUTED`, no
  `OrderReconciler`, no `_execute_trade`.
- Convert the existing log once, replace it. No dual-shape parser.
- Full test coverage before merging. High confidence, zero skipped checks.
- Review alignment with Kraken CLI WS behavior as a final sanity pass.
- Bot is stopped so files cannot self-overwrite work in progress.

## Spike results (validated, 2026-04-11)

`kraken ws executions -o json --snap-orders true --snap-trades true` spawned from
Python via `wsl -d Ubuntu -- bash -c "..."` with `subprocess.Popen(bufsize=1,
text=True)`, reading `stdout.readline()` in a loop.

**Verdict: line-buffered JSON flows reliably across the WSL→Windows-Python
boundary. Proceed.**

### Channels observed

| line | shape |
|---|---|
| 1 | `{"channel":"status","type":"update","data":[{"api_version":"v2","connection_id":"...","system":"online","version":"2.0.10"}]}` |
| 2 | `{"method":"subscribe","result":{"channel":"executions","maxratecount":125,"snap_orders":true,"snap_trades":true,"snapshot":true,"warnings":[...]},"success":true,"time_in":...,"time_out":...}` — subscribe ack |
| 3 | `{"channel":"executions","type":"snapshot","sequence":1,"data":[...50 exec entries...]}` |
| 4+ | `{"channel":"heartbeat"}` — arrives ~1/sec |

Deprecation warning in the subscribe ack: `cancel_reason is deprecated, use
reason`. Use `reason` field in new code.

### Execution entry shape (raw, types confirmed)

```json
{
  "cost": 0.007262,              // float
  "exec_id": "TBQGLU-646YF-3L6UC6", // str, unique per fill
  "exec_type": "trade",          // str
  "fee_usd_equiv": 0.0,          // float
  "fees": [{"asset":"USDC","qty":1.5e-05}],  // list of {asset, qty}
  "last_price": 85.16,           // float
  "last_qty": 8.528e-05,         // float, this fill's quantity
  "liquidity_ind": "m",          // str, 'm'=maker, 't'=taker
  "order_id": "OV33EK-VOGHL-DB43VD",  // str, the Kraken ordertxid
  "order_status": "filled",      // str
  "order_type": "limit",         // str
  "order_userref": 0,            // int (0 when we didn't set one)
  "side": "sell",                // str, buy|sell
  "symbol": "SOL/USDC",          // str, with slash
  "timestamp": "2026-04-11T16:48:26.620771Z",  // str, ISO
  "trade_id": 1241209            // int
}
```

**All numerics are real JSON floats/ints — NOT strings.** Earlier concern
about string coercion was wrong for CLI v0.2.3. Parser does not need to
coerce.

### Key shape facts

- **Multiple exec events per order are normal.** The 50-entry snapshot
  contained 2 entries with the same `order_id` but different `exec_id`
  and `trade_id` — a partial-fill pattern. The parser must aggregate by
  `order_id`.
- **Snapshot returns up to 50 recent trade events** (`--snap-trades true`)
  plus currently-open orders (`--snap-orders true`). On this machine the
  open-order snapshot was empty because nothing was resting.
- **Sequence number on the executions channel** (`sequence: 1`) for gap
  detection — store the last seen sequence per connection, warn on gap.
- **Heartbeats** arrive ~1/sec — use them as liveness signal.
- `liquidity_ind: 'm'` confirms post-only trades were maker (44 of 50 in
  snapshot). The 6 'taker' entries are the user's 2026-04-11 manual tests.

### order_status values (from Kraken WS v2 docs + observed)

`pending_new` | `new` | `partially_filled` | `filled` | `canceled` | `expired`
| `rejected` | `pending_cancel` | `triggered`

### exec_type values (from Kraken WS v2 docs)

`pending_new` | `new` | `trade` | `filled` | `canceled` | `expired` | `amended`
| `restated` | `status` | `rejected`

### Reject reasons to watch for

From the executions stream's `reason` field: `"Post only order"` (post-only
crossed book), `"CancelAllOrdersAfter Timeout"` (DMS), user cancels.

## Open questions — resolved

| # | Question | Decision |
|---|---|---|
| 1 | CLI flag vs env var for reconciler mode? | **Neither.** Delete the flag concept. WS is the only reconciler. No poll fallback. |
| 2 | WSL subprocess reliability spike? | **Passed.** Documented above. Proceed with confidence. |
| 3 | Startup backfill policy? | **Built-in.** `--snap-orders --snap-trades` already gives 50 recent exec snapshot on connect. For fills older than that window, one REST `trades-history --start <journal_tail_time>` call during startup (bounded, once). |
| 4 | Status nomenclature — legacy tolerance? | **No tolerance.** One-shot migration of on-disk file. Single state machine in new code: `PLACED → FILLED / PARTIALLY_FILLED / CANCELLED_UNFILLED / REJECTED / PLACEMENT_FAILED`. `EXECUTED`, `PLACED_NOT_FILLED`, `FAILED`, `PAPER_EXECUTED`, `PAPER_FAILED`, `TICKER_FAILED`, `VALIDATION_FAILED`, `COORDINATED_SWAP` all retired. |

Additional locked-in decisions:

- **Placement stays REST.** `KrakenCLI.order_buy` / `order_sell` unchanged
  in transport. Only correctness-critical thing is WS executions — placement
  latency is not the current bottleneck. Use `--userref <N>` for correlation.
- **Correlation via `order_userref`.** Monotonic counter per agent instance,
  seeded from `int(time.time())` at startup to survive restarts without
  collision. `KrakenCLI.order_buy/order_sell` gains a `userref: int = None`
  kwarg that appends `--userref N` to the CLI args.
- **Fallback correlation via `order_id`.** After REST placement returns a
  txid, also register that txid with the ExecutionStream. Double-keyed
  matching: userref OR order_id.
- **Paper mode gets a synthetic PaperExecutionStream.** Paper trades never
  touch WS; they go directly to `lifecycle.state: FILLED` with the requested
  vol/price as fill values. Keeps paper mode in the new shape without a
  different code path.
- **Engine rollback strategy unchanged.** `snapshot_position` / `restore_position`
  already exist on HydraEngine. On terminal events `CANCELLED_UNFILLED` and
  `REJECTED`, the ExecutionStream drain in the tick loop calls restore. On
  `PARTIALLY_FILLED (terminal)` it needs a new `adjust_position(new_size)`
  engine method — to be added as part of this PR.
- **Backfill is eager, not lazy.** Startup reads the journal tail time,
  calls `kraken trades-history --start <tail>`, and reconciles any order
  IDs in that range that the local journal doesn't already know about.
  This bounds to ≤1 REST call at startup.

## New data model — LOCKED

### Order journal entry (one per placed order)

```python
{
  "placed_at":       "2026-04-11T16:48:10.786592+00:00",  # ISO, bot clock
  "pair":            "SOL/USDC",
  "side":            "SELL",                   # uppercase
  "intent": {
    "amount":        0.03048528,               # what the bot asked for
    "limit_price":   85.16,
    "post_only":     true,
    "order_type":    "limit",                  # "limit" | "market" (market unused today)
    "paper":         false                     # true in paper mode
  },
  "decision": {
    "strategy":                    "MEAN_REVERSION",  # from state["strategy"]
    "regime":                      "RANGING",         # from state["regime"]
    "reason":                      "Mean reversion SELL: ...",  # free-form, human-readable
    "confidence":                  0.5463,            # final confidence at placement
    "params_at_entry":             {...},             # engine snapshot for tuner learning
    "cross_pair_override":         null,              # dict if a CrossPairCoordinator override applied
    "book_confidence_modifier":    -0.02,             # from OrderBookAnalyzer
    "brain_verdict":               null,              # dict if brain fired: {action, final_signal, summary}
    "swap_id":                     null               # groups legs of a coordinated swap
  },
  "order_ref": {
    "order_userref":  1775925890,                # numeric tag we passed
    "order_id":       "OV33EK-VOGHL-DB43VD"      # Kraken txid from placement response
  },
  "lifecycle": {
    "state":           "FILLED",                  # PLACED|FILLED|PARTIALLY_FILLED|
                                                  # CANCELLED_UNFILLED|REJECTED|PLACEMENT_FAILED
    "vol_exec":        0.03048528,                 # executed volume (0 for unfilled)
    "avg_fill_price":  85.15,                      # vol-weighted avg (null if no fills)
    "fee_quote":       0.005177,                   # total fees in quote asset (null if no fills)
    "final_at":        "2026-04-11T16:48:26.620771+00:00",  # ISO, from first terminal event
    "terminal_reason": null,                       # "post_only" | "dms_timeout" | "user_cancel" | "api_error:..."
    "exec_ids":        ["TBQGLU-646YF-3L6UC6", "TBOBAL-B4R3S-PBPA2S"]  # pointers to Kraken trades-history
  }
}
```

### On-disk files

- `hydra_order_journal.json` — rolling journal, full shape, replaces
  `hydra_trades_live.json`. Atomic write via `.tmp + os.replace`. Capped
  by `ORDER_JOURNAL_CAP = 2000`.
- `hydra_session_snapshot.json` — unchanged file, but now carries key
  `"order_journal": [...]` instead of `"trade_log": [...]`, capped at
  `[-200:]` for compactness (unchanged policy).
- Migration is a one-shot: old file → new file, then old file deleted.
  Old key in snapshot → new key in snapshot, same file path.

### In-memory

- `self.order_journal: List[dict]` — attribute replaces `self.trade_log`.
- `self._pending_orders: Dict[str, PendingOrder]` — maps userref/order_id
  to a small struct carrying the journal entry index + engine rollback
  snapshot, for use during WS event application.

## Naming map — apply consistently EVERYWHERE

| old name | new name |
|---|---|
| `OrderReconciler` class | `ExecutionStream` class |
| `self.reconciler` | `self.execution_stream` |
| `self.reconciler.register(...)` | `self.execution_stream.register(...)` |
| `self.reconciler.maybe_reconcile(tick)` | `self.execution_stream.drain_events()` |
| `self.reconciler.known_orders` | `self.execution_stream._known_orders` (internal) |
| `OrderReconciler.poll_every_ticks` | (removed — no polling) |
| `OrderReconciler.maybe_reconcile` | (removed — push not poll) |
| `hydra_trades_live.json` | `hydra_order_journal.json` |
| `self.trade_log` | `self.order_journal` |
| `TRADE_LOG_CAP` | `ORDER_JOURNAL_CAP` |
| `_execute_trade` | `_place_order` |
| `_execute_paper_trade` | `_place_paper_order` |
| `_merge_rolling_trade_log` | `_merge_order_journal` |
| `trade_log_grew` (local var in run()) | `order_journal_grew` |
| snapshot JSON key `trade_log` | `order_journal` |
| broadcast JSON key `trade_log` | `order_journal` |
| status `EXECUTED` | `lifecycle.state: FILLED` (after stream confirms) |
| status `PLACED_NOT_FILLED` | `lifecycle.state: CANCELLED_UNFILLED` or `REJECTED` |
| status `FAILED` | `lifecycle.state: PLACEMENT_FAILED` |
| status `PAPER_EXECUTED` | `lifecycle.state: FILLED` with `intent.paper: true` |
| status `PAPER_FAILED` | `lifecycle.state: PLACEMENT_FAILED` with `intent.paper: true` |
| status `TICKER_FAILED` | `lifecycle.state: PLACEMENT_FAILED`, `terminal_reason: "ticker_failed"` |
| status `VALIDATION_FAILED` | `lifecycle.state: PLACEMENT_FAILED`, `terminal_reason: "validation_failed:..."` |
| `COORDINATED_SWAP` type entry | gone — each leg is an ordinary entry with `decision.swap_id` set |
| `last_trade_log_entry` (harness) | `last_journal_entry` |
| `trade_log_count_before/after` | `journal_count_before/after` |

## File-by-file implementation plan

### `hydra_agent.py` (main file — largest diff)

1. **Imports** — no new std-lib needed (subprocess, threading, json, queue
   are all already imported or available).
2. **`KrakenCLI.order_buy` / `order_sell`** — add `userref: int = None` kwarg,
   append `["--userref", str(userref)]` when set. Same for `order_sell`.
3. **Delete `OrderReconciler` class** entirely (lines 444–489 currently).
4. **New class `ExecutionStream`** — inserted where OrderReconciler was.
   See implementation sketch below.
5. **`HydraAgent.__init__`**:
   - `self.trade_log = []` → `self.order_journal = []`
   - `self.reconciler = OrderReconciler(...)` → `self.execution_stream = ExecutionStream(paper=paper)`
     (paper mode gets the synthetic subclass or a `paper=True` flag).
   - `self._userref_counter = int(time.time())` — seed for correlation IDs.
   - `self._pending_orders: Dict[str, dict] = {}` — correlation map.
   - After snapshot/merge: `self.execution_stream.start()` — start the
     subprocess and background reader (live mode only, paper is synchronous).
6. **`_handle_shutdown`** — add `self.execution_stream.stop()` before
   `_save_snapshot`. Already calls `cancel_all`.
7. **`_snapshot_path`, `_save_snapshot`**:
   - Key rename `trade_log` → `order_journal`.
   - Still `[-200:]`.
8. **`_load_snapshot`**:
   - Read `order_journal` key (not `trade_log`).
   - **Migration tolerance**: if the key `trade_log` is present and
     `order_journal` is not, call `_migrate_legacy_trade_log` to convert
     in-place before assigning, then immediately save the snapshot in new
     shape. This handles existing on-disk snapshots without a separate
     migration script run. Remove this tolerance after one week in prod
     (or leave it — it's ~10 lines).
9. **`_merge_rolling_trade_log` → `_merge_order_journal`**:
   - Read `hydra_order_journal.json` instead.
   - **Legacy-file migration**: if `hydra_order_journal.json` does not
     exist but `hydra_trades_live.json` does, call
     `_migrate_legacy_trade_log_file(old_path, new_path)` which runs the
     migrator on the old file, writes the new file, **renames the old
     file to `hydra_trades_live.json.migrated`** (don't delete, keep as
     audit trail). Then reads the new file normally.
   - Dedup key: use `order_ref.order_id` if present, else
     `(placed_at, pair, side, intent.amount)`.
10. **`run()` tick body**:
    - Rename `trade_log_size_start` → `journal_size_start`, `trade_log_grew`
      → `journal_grew`.
    - Rolling save: `rolling_file = os.path.join(self._snapshot_dir, "hydra_order_journal.json")`.
    - **After the existing reconciler.maybe_reconcile block, replace with
      `events = self.execution_stream.drain_events()` and a dispatch loop
      that applies each event to `self.order_journal` and engine state via
      `_apply_execution_event(event)`**.
11. **Delete**: `_execute_trade` lines 1267–1352 and `_execute_paper_trade`
    lines 1354–1387. Replace with `_place_order` + `_place_paper_order` +
    `_apply_execution_event` + `_build_journal_entry`.
12. **`_execute_coordinated_swap`** — update both `_execute_trade` calls
    to `_place_order`, and pass a `swap_id` into the decision dict so both
    legs carry it.
13. **`_compute_pair_realized_pnl`** — iterate `self.order_journal`, skip
    entries whose `lifecycle.state` is in `{PLACED, PLACEMENT_FAILED,
    CANCELLED_UNFILLED, REJECTED}` (anything not filled), read `vol_exec`
    and `avg_fill_price` from `lifecycle`, still return `sell_revenue -
    buy_cost`. Delete the `PLACED_NOT_FILLED` branch (no longer the field
    name).
14. **`_build_dashboard_state`** — key `trade_log` → `order_journal`,
    still `[-20:]`.
15. **`_print_final_report`** — loop over `order_journal`, print new shape.
    Export filename stays `hydra_trades_{ts}.json` or rename to
    `hydra_orders_{ts}.json` for consistency. RECOMMEND rename.
16. **`_export_competition_results`** — embed `order_journal` not `trade_log`.
    Filename stays `competition_results_{ts}.json`.
17. **`TRADE_LOG_CAP = 2000` → `ORDER_JOURNAL_CAP = 2000`**.
18. **`SNAPSHOT_EVERY_N_TICKS`** — unchanged.

### New class: `ExecutionStream`

```python
class ExecutionStream:
    """Owns `kraken ws executions` subprocess. Delivers push events to the
    agent tick loop. Replaces REST-based OrderReconciler.

    Lifecycle:
        start()  — spawn subprocess, launch background reader thread
        register(order_id, userref, journal_index, pair, side,
                 placed_amount, engine, pre_trade_snapshot)
        drain_events() -> List[ExecutionEvent]  — tick loop consumes
        stop()  — terminate subprocess, join reader thread
        healthy -> bool  — True if subprocess + reader thread alive

    Correlation: matches incoming WS events to registered placements by
    (userref OR order_id). Paper mode uses PaperExecutionStream subclass
    that skips the subprocess and generates synthetic FILLED events
    inline from place_order.
    """

    HEARTBEAT_TIMEOUT_S = 15.0   # considered unhealthy if no heartbeat in this window
    READER_JOIN_TIMEOUT_S = 5.0

    def __init__(self, paper: bool = False):
        self.paper = paper
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._event_queue: queue.Queue = queue.Queue()
        self._known_orders: Dict[str, dict] = {}   # keyed by order_id
        self._userref_to_order_id: Dict[int, str] = {}
        self._last_heartbeat: float = 0.0
        self._last_sequence: Optional[int] = None
        self._lock = threading.Lock()
        self._shutdown = threading.Event()

    def start(self):
        if self.paper:
            return  # paper uses synthetic events, no subprocess
        cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-c",
               "source ~/.cargo/env && kraken ws executions -o json "
               "--snap-orders true --snap-trades true"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="ExecutionStream-reader", daemon=True,
        )
        self._reader_thread.start()
        self._last_heartbeat = time.time()
        print("  [EXECSTREAM] WS executions stream started")

    def _reader_loop(self):
        assert self._proc is not None
        for raw in self._proc.stdout:
            if self._shutdown.is_set():
                break
            line = raw.rstrip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Kraken CLI sometimes emits non-JSON diagnostic lines. Log
                # for forensic value but keep reading.
                print(f"  [EXECSTREAM] non-JSON line: {line[:120]}")
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict):
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._last_heartbeat = time.time()
            return
        if channel == "status":
            # Connection status update; informational
            return
        if msg.get("method") == "subscribe":
            # Subscribe ack; check success
            if not msg.get("success"):
                print(f"  [EXECSTREAM] subscribe failed: {msg}")
            return
        if channel != "executions":
            return
        # Sequence gap detection
        seq = msg.get("sequence")
        if seq is not None:
            if self._last_sequence is not None and seq != self._last_sequence + 1:
                print(f"  [EXECSTREAM] sequence gap: {self._last_sequence}→{seq}")
            self._last_sequence = seq
        msg_type = msg.get("type")  # "snapshot" | "update"
        data = msg.get("data") or []
        for entry in data:
            self._event_queue.put(("snapshot" if msg_type == "snapshot" else "update", entry))

    def register(self, order_id: str, userref: Optional[int],
                 journal_index: int, pair: str, side: str,
                 placed_amount: float, engine_ref, pre_trade_snapshot):
        with self._lock:
            self._known_orders[order_id] = {
                "userref": userref,
                "journal_index": journal_index,
                "pair": pair,
                "side": side,
                "placed_amount": placed_amount,
                "engine_ref": engine_ref,
                "pre_trade_snapshot": pre_trade_snapshot,
                "registered_at": time.time(),
                "vol_exec_running": 0.0,
                "cost_running": 0.0,
                "fee_running": 0.0,
                "exec_ids": [],
            }
            if userref is not None:
                self._userref_to_order_id[userref] = order_id

    def drain_events(self) -> List[dict]:
        """Called by the tick loop. Returns a list of typed lifecycle-change
        events for the agent to apply to the journal + engine state.

        Event shape:
            {
                "type": "fill" | "cancel" | "reject" | "ack",
                "order_id": str,
                "journal_index": int,
                "engine_ref": HydraEngine,
                "pre_trade_snapshot": dict,
                "state": "FILLED" | "PARTIALLY_FILLED" | "CANCELLED_UNFILLED" | "REJECTED",
                "vol_exec": float,
                "avg_fill_price": Optional[float],
                "fee_quote": float,
                "terminal_reason": Optional[str],
                "exec_ids": List[str],
                "timestamp": str,
            }
        """
        out: List[dict] = []
        while True:
            try:
                kind, entry = self._event_queue.get_nowait()
            except queue.Empty:
                break

            order_id = entry.get("order_id")
            if not order_id:
                continue

            # Match against registered placements
            with self._lock:
                known = self._known_orders.get(order_id)
                if known is None:
                    # Might be a snapshot entry for an order placed before
                    # this agent started, or a manual trade. Skip.
                    continue

                exec_type = entry.get("exec_type")
                order_status = entry.get("order_status")

                if exec_type == "trade":
                    last_qty = float(entry.get("last_qty") or 0)
                    last_price = float(entry.get("last_price") or 0)
                    cost = float(entry.get("cost") or (last_qty * last_price))
                    fees = entry.get("fees") or []
                    fee_quote = sum(float(f.get("qty") or 0) for f in fees)
                    known["vol_exec_running"] += last_qty
                    known["cost_running"] += cost
                    known["fee_running"] += fee_quote
                    exec_id = entry.get("exec_id")
                    if exec_id:
                        known["exec_ids"].append(exec_id)

                terminal = order_status in ("filled", "canceled", "expired", "rejected")

                if not terminal:
                    continue  # partial interim — we'll emit one event on terminal

                vol_exec = known["vol_exec_running"]
                avg_price = (known["cost_running"] / vol_exec) if vol_exec > 0 else None
                placed_amount = known["placed_amount"]
                eps = max(1e-9, placed_amount * 1e-6)

                if order_status == "filled":
                    state = "FILLED" if abs(vol_exec - placed_amount) <= eps else "PARTIALLY_FILLED"
                    terminal_reason = None
                elif order_status in ("canceled", "expired"):
                    reason = entry.get("reason") or order_status
                    if vol_exec <= eps:
                        state = "CANCELLED_UNFILLED"
                    else:
                        state = "PARTIALLY_FILLED"
                    terminal_reason = reason
                elif order_status == "rejected":
                    state = "REJECTED"
                    terminal_reason = entry.get("reason") or "rejected"
                else:
                    continue

                out.append({
                    "type": "terminal",
                    "order_id": order_id,
                    "journal_index": known["journal_index"],
                    "engine_ref": known["engine_ref"],
                    "pre_trade_snapshot": known["pre_trade_snapshot"],
                    "placed_amount": placed_amount,
                    "state": state,
                    "vol_exec": vol_exec,
                    "avg_fill_price": avg_price,
                    "fee_quote": known["fee_running"],
                    "terminal_reason": terminal_reason,
                    "exec_ids": list(known["exec_ids"]),
                    "timestamp": entry.get("timestamp"),
                })

                # Order is finalized — drop from known
                del self._known_orders[order_id]
                if known.get("userref") is not None:
                    self._userref_to_order_id.pop(known["userref"], None)
        return out

    @property
    def healthy(self) -> bool:
        if self.paper:
            return True
        if self._proc is None:
            return False
        if self._proc.poll() is not None:
            return False  # subprocess exited
        if self._reader_thread is None or not self._reader_thread.is_alive():
            return False
        # Liveness via heartbeat staleness
        if time.time() - self._last_heartbeat > self.HEARTBEAT_TIMEOUT_S:
            return False
        return True

    def stop(self):
        self._shutdown.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=self.READER_JOIN_TIMEOUT_S)
            self._reader_thread = None
```

Notes on the sketch:

- The reader loop uses the `for line in self._proc.stdout:` iterator
  which is line-buffered given `bufsize=1, text=True`. This is what the
  spike proved works.
- Sequence gap detection is purely informational — log a warning, don't
  halt. A gap means Kraken dropped events; the next snapshot on reconnect
  recovers the missing state.
- No reconnect logic in v1 — if the subprocess dies, `healthy=False` and
  the tick loop logs a warning. **This is a known v1 gap, flagged below
  under "Follow-ups".**
- Paper mode doesn't start the subprocess. `place_order` detects
  `self.execution_stream.paper` and emits a synthetic terminal event
  directly into the queue via a helper `_synthetic_fill(order_id, ...)`.

### New method: `_place_order` (replaces `_execute_trade`)

```python
def _place_order(self, pair: str, trade: dict, state: dict) -> bool:
    """Place a real order via KrakenCLI and write the initial journal entry.

    Writes exactly one journal entry (lifecycle.state=PLACED on success,
    lifecycle.state=PLACEMENT_FAILED on any pre-placement failure). On
    success, registers the order with the execution stream so incoming WS
    events can finalize the lifecycle on later ticks.

    Returns True if Kraken accepted the placement, False otherwise. The
    caller rolls back engine state on False. Post-placement failures (DMS,
    post-only reject, partial cancel) are handled asynchronously by
    drain_events and _apply_execution_event; those rollbacks happen later
    ticks, not here.
    """
    action = trade["action"].lower()
    amount = trade["amount"]
    entry = self._build_journal_entry(pair, trade, state)
    pre_trade_snap = state.get("_pre_trade_snapshot")

    if self.paper:
        return self._place_paper_order(pair, action, amount, trade, entry, pre_trade_snap)

    # ─── Live: limit post-only ───
    time.sleep(2)
    ticker = KrakenCLI.ticker(pair)
    if "error" in ticker or "bid" not in ticker:
        entry["lifecycle"] = {
            "state": "PLACEMENT_FAILED",
            "vol_exec": 0.0, "avg_fill_price": None, "fee_quote": 0.0,
            "final_at": datetime.now(timezone.utc).isoformat(),
            "terminal_reason": f"ticker_failed:{ticker.get('error','no bid/ask')}",
            "exec_ids": [],
        }
        self.order_journal.append(entry)
        return False

    limit_price = ticker["bid"] if action == "buy" else ticker["ask"]
    entry["intent"]["limit_price"] = limit_price

    time.sleep(2)
    userref = self._next_userref()
    if action == "buy":
        val_result = KrakenCLI.order_buy(pair, amount, price=limit_price, validate=True)
    else:
        val_result = KrakenCLI.order_sell(pair, amount, price=limit_price, validate=True)
    if "error" in val_result:
        entry["lifecycle"] = {
            "state": "PLACEMENT_FAILED",
            "vol_exec": 0.0, "avg_fill_price": None, "fee_quote": 0.0,
            "final_at": datetime.now(timezone.utc).isoformat(),
            "terminal_reason": f"validation_failed:{val_result['error']}",
            "exec_ids": [],
        }
        self.order_journal.append(entry)
        return False

    # Re-fetch ticker
    time.sleep(2)
    fresh_ticker = KrakenCLI.ticker(pair)
    if "error" not in fresh_ticker and "bid" in fresh_ticker:
        limit_price = fresh_ticker["bid"] if action == "buy" else fresh_ticker["ask"]
        entry["intent"]["limit_price"] = limit_price

    if action == "buy":
        result = KrakenCLI.order_buy(pair, amount, price=limit_price, userref=userref)
    else:
        result = KrakenCLI.order_sell(pair, amount, price=limit_price, userref=userref)

    if "error" in result:
        entry["lifecycle"] = {
            "state": "PLACEMENT_FAILED",
            "vol_exec": 0.0, "avg_fill_price": None, "fee_quote": 0.0,
            "final_at": datetime.now(timezone.utc).isoformat(),
            "terminal_reason": f"placement_error:{result['error']}",
            "exec_ids": [],
        }
        self.order_journal.append(entry)
        return False

    order_id = result.get("txid", result.get("result", {}).get("txid", "unknown"))
    if isinstance(order_id, list):
        order_id = order_id[0] if order_id else "unknown"

    entry["order_ref"] = {"order_userref": userref, "order_id": order_id}
    entry["lifecycle"] = {
        "state": "PLACED",
        "vol_exec": 0.0, "avg_fill_price": None, "fee_quote": 0.0,
        "final_at": None, "terminal_reason": None, "exec_ids": [],
    }
    self.order_journal.append(entry)
    journal_index = len(self.order_journal) - 1

    # Register with stream so WS events can finalize this order's lifecycle
    self.execution_stream.register(
        order_id=order_id, userref=userref, journal_index=journal_index,
        pair=pair, side=trade["action"], placed_amount=amount,
        engine_ref=self.engines[pair], pre_trade_snapshot=pre_trade_snap,
    )
    return True
```

The `_build_journal_entry` helper snapshots the decision context from
`state` (strategy, regime, signal reason, confidence, cross-pair override,
brain verdict, order book modifier, swap_id if set). The
`_place_paper_order` path builds the same entry shape but immediately
populates `lifecycle.state=FILLED` with `vol_exec=amount, avg_fill_price=intent.limit_price,
fee_quote=0.0` — no subprocess, no register.

### New method: `_apply_execution_event`

Called in the tick loop on each event from `drain_events()`. Patches the
journal entry at `journal_index` with the terminal lifecycle, and does
engine corrections:

```python
def _apply_execution_event(self, event: dict):
    idx = event["journal_index"]
    if idx >= len(self.order_journal):
        return
    entry = self.order_journal[idx]
    state_name = event["state"]
    entry["lifecycle"] = {
        "state": state_name,
        "vol_exec": event["vol_exec"],
        "avg_fill_price": event["avg_fill_price"],
        "fee_quote": event["fee_quote"],
        "final_at": event.get("timestamp"),
        "terminal_reason": event.get("terminal_reason"),
        "exec_ids": event.get("exec_ids") or [],
    }
    engine = event["engine_ref"]
    pre_snap = event["pre_trade_snapshot"]

    if state_name == "CANCELLED_UNFILLED" or state_name == "REJECTED":
        if engine and pre_snap:
            engine.restore_position(pre_snap)
            print(f"  [EXECSTREAM] {entry['pair']} {entry['side']} rolled back after {state_name}")
    elif state_name == "PARTIALLY_FILLED":
        # New engine method required: adjust_position(new_size) that
        # preserves avg_entry but resizes cost_basis proportionally.
        if engine and pre_snap:
            placed = event["placed_amount"]
            vol = event["vol_exec"]
            if placed > 0 and vol < placed:
                engine.adjust_position(
                    pre_snap,
                    actual_size_delta=(vol if entry["side"] == "BUY" else -vol),
                )
        print(f"  [EXECSTREAM] {entry['pair']} {entry['side']} PARTIALLY_FILLED: "
              f"{event['vol_exec']}/{event['placed_amount']}")
    # FILLED = no engine correction needed (optimistic commit was right)
```

### `hydra_engine.py` — new method `adjust_position`

Only needed for the partial-fill path. Signature TBD in implementation.
Reuses the existing snapshot/restore primitives. If time is tight, an
acceptable v1 is: on partial fill, fully restore and then call
`execute_signal` with the actual filled amount. That's less surgical but
avoids a new engine method — defer the cleanup to a follow-up.

### `dashboard/src/App.jsx` — minimal diff

Only one line references `trade_log` (line 190):
```js
if (data.trade_log) setTradeLog(data.trade_log);
```
Rename to `order_journal` and rename the React state variable
`tradeLog`→`orderJournal` and `setTradeLog`→`setOrderJournal` everywhere
(grep — probably 3–5 uses). Update the journal display table to read new
field paths (`lifecycle.state`, `lifecycle.vol_exec`, `lifecycle.avg_fill_price`).
The display columns are a judgment call — keep time/pair/side/amount/price/
state and drop the confidence column only if you run out of width.

### Tests — `tests/live_harness/`

1. **`schemas.py`** — rewrite entirely for the new shape. Single schema
   function `validate_journal_entry(entry, expected_state=None)` that
   checks:
   - top-level keys: `placed_at, pair, side, intent, decision, order_ref, lifecycle`
   - `intent` keys: `amount, limit_price, post_only, order_type, paper`
   - `decision` keys: `strategy, regime, reason, confidence, params_at_entry,
     cross_pair_override, book_confidence_modifier, brain_verdict, swap_id`
   - `order_ref` keys: `order_userref, order_id` (both may be None pre-placement)
   - `lifecycle` keys: `state, vol_exec, avg_fill_price, fee_quote, final_at,
     terminal_reason, exec_ids`
   - `lifecycle.state` ∈ the 6 allowed values
   - Type checks for each field.
2. **`harness.py`** — `_execute_trade` → `_place_order`, `agent.trade_log`
   → `agent.order_journal`, `last_trade_log_entry` → `last_journal_entry`,
   `trade_log_count_*` → `journal_count_*`. The stashed files list now
   includes `hydra_order_journal.json` alongside the legacy name for
   transition. Prefer to pass the `state` dict positional arg into
   `_place_order` in addition to the trade dict.
3. **`scenarios.py`** — every scenario's assertion block rewritten. Most
   scenarios become simpler because the distinction between TICKER_FAILED
   / VALIDATION_FAILED / FAILED / EXECUTED collapses into
   `lifecycle.state` + `terminal_reason`. Use `expected_state="PLACED"`
   for successful placements (lifecycle finalization happens via stream
   events which aren't in mock-mode flow).
4. **New: `FakeExecutionStream`** — drop-in replacement for
   `ExecutionStream` in mock mode. Has a `push_event(event_dict)` method
   that tests call to simulate WS events. Harness swaps the real stream
   for the fake one in `new_agent`.
5. **New scenarios W1-W7** to cover the stream:
   - W1: place → fake event full fill → FILLED, engine unchanged
   - W2: place → fake partial fill + cancel → PARTIALLY_FILLED + adjust
   - W3: place → fake post-only reject → REJECTED + engine restored
   - W4: place → fake DMS cancel → CANCELLED_UNFILLED + engine restored
   - W5: place → no event → remains PLACED (non-terminal)
   - W6: snapshot contains a pre-existing order_id we didn't register → skipped
   - W7: fake subprocess death → `healthy=False` after heartbeat timeout
6. **`stubs.py`** — no changes needed (docstring references to
   `_execute_trade` can be left or updated).
7. **`state_comparator.py`** — comment reference to `_execute_trade` in
   docstring only; update.
8. **`__init__.py`** — update docstring.

### Migrator — `scripts/migrate_trade_log_to_order_journal.py` (new)

One-shot migration script. Committable. Usable both as a standalone CLI
(`python scripts/migrate_trade_log_to_order_journal.py`) and as an
importable function from `hydra_agent._merge_order_journal`.

What it does:

1. Reads `hydra_trades_live.json` if present.
2. For each entry, maps to new shape:
   - `time` → `placed_at`
   - `pair`, `action` → `side` (UPPER)
   - `amount`, `price`, `order_type` ("limit post-only") → `intent` (post_only=True for "limit post-only")
   - `reason`, `confidence` → `decision.reason`, `decision.confidence` (other decision fields null — historical entries don't have them)
   - `result.txid[0]` → `order_ref.order_id`; `order_userref` → null (historical placements didn't use userref)
   - `status` → `lifecycle.state`:
     - `EXECUTED` → `FILLED` (trust — legacy field, most really filled)
     - `PAPER_EXECUTED` → `FILLED`, `intent.paper = True`
     - `PLACED_NOT_FILLED` → `CANCELLED_UNFILLED`, `lifecycle.terminal_reason` from `reconciliation_note`
     - `FAILED` → `PLACEMENT_FAILED`, `lifecycle.terminal_reason` from `error`
     - `TICKER_FAILED` / `VALIDATION_FAILED` → `PLACEMENT_FAILED` with matching `terminal_reason`
     - `PAPER_FAILED` → `PLACEMENT_FAILED`, `intent.paper=True`
     - COORDINATED_SWAP marker entries → drop (the legs are already in the log)
   - For `FILLED` entries: fill `lifecycle.vol_exec = amount`,
     `lifecycle.avg_fill_price = price`, `lifecycle.fee_quote = null`
     (historical fee data not in local log), `lifecycle.exec_ids = []`
     (we don't have them — could be backfilled via trades-history
     during startup, but not in the migrator to keep it offline).
3. Sorts by `placed_at`.
4. Writes `hydra_order_journal.json` atomically.
5. Renames `hydra_trades_live.json` → `hydra_trades_live.json.migrated`
   (preserved as audit trail).
6. Also migrates `hydra_session_snapshot.json` in place: if it contains a
   `trade_log` key, maps each entry via the same logic and replaces with
   `order_journal` key. Atomic write.

**Idempotency**: if the new file already exists and the old one is
already renamed, the script is a no-op.

**Coordinated swap marker handling**: current hydra_trades_live.json
does NOT contain any COORDINATED_SWAP marker entries (user has only ever
traded individual orders so far), so the migrator can simply drop them
defensively if encountered.

**This machine's data**: 37-entry `hydra_trades_live.json` after the
PR #40 data repair. Migrator should produce a 37-entry
`hydra_order_journal.json` with 34 FILLED + 2 CANCELLED_UNFILLED + 1
PLACEMENT_FAILED, sorted by time from 2026-04-05T10:41 to
2026-04-11T16:48.

### CLAUDE.md + docs + memory

- `CLAUDE.md` — update the "Trading" bullet about `trade_log`
  persistence to reference `order_journal`. Update the "Common Pitfalls"
  section if any trade_log reference remains.
- `AUDIT.md`, `CHANGELOG.md`, `README.md` — scan for stale references,
  update. These are docs, non-blocking.
- `HYDRA_MEMORY.md` — no change (schema doc, doesn't mention
  `trade_log` by name in the body, only the `session.trade_log`
  category in the groups table — that stays).
- `.hydra-memory/graph.json` — retire `agent.reconciler_fill_gap` node
  (status: resolved), add `agent.execution_stream` node, update
  `agent.trade_log_persistence` → split into two nodes or rename to
  `agent.order_journal_persistence`, update edges.

## Order of operations (so tests stay green at each step)

1. **Commit the handoff doc + gitignore** (this commit you're reading).
2. **Write the migrator script** — standalone, unit-tested against a
   synthetic legacy file.
3. **Write `ExecutionStream` + `FakeExecutionStream`** as a new file
   or in-line section at the top of hydra_agent.py. Unit tests exercise
   the parser against the real WS event shapes captured in this doc.
4. **Add `KrakenCLI.order_buy` / `order_sell` userref kwarg.** Backwards-
   compat default `None` — won't break existing callers before the rename.
5. **Add `_place_order` + `_build_journal_entry` + `_apply_execution_event`
   methods AS NEW METHODS alongside the old ones.** Don't delete the old
   methods yet. Gives a reversible step.
6. **Switch the tick-loop caller** from `_execute_trade` to `_place_order`.
7. **Switch `run()` to use `self.execution_stream` + `drain_events()`**
   instead of `self.reconciler.maybe_reconcile`.
8. **Now delete `OrderReconciler` + `_execute_trade` + `_execute_paper_trade`.**
9. **Rename `trade_log` → `order_journal` everywhere in hydra_agent.py.**
   (Use Edit with replace_all=True on distinct tokens.)
10. **Rename `hydra_trades_live.json` → `hydra_order_journal.json`**
    everywhere (including snapshot key and merge method name).
11. **Update `_compute_pair_realized_pnl`.**
12. **Update `_build_dashboard_state`, `_print_final_report`,
    `_export_competition_results`.**
13. **Update dashboard/src/App.jsx.**
14. **Update tests/live_harness/{harness.py, scenarios.py, schemas.py,
    state_comparator.py, __init__.py, stubs.py}.**
15. **Run `python tests/test_engine.py` and every other test module.**
16. **Run the harness smoke + mock modes.**
17. **Run the migrator on the real hydra_trades_live.json on this
    machine.** Verify output. Verify `_merge_order_journal` reads it.
18. **Update docs and memory graph.**
19. **Commit, push, open PR.**
20. **Watch CI.**
21. **Merge.**

Each step above is independently mergeable-to-branch and independently
testable. Steps 1-4 touch no runtime behavior. Step 5 introduces the new
code without activating it. Step 6 flips behavior. Steps 7-20 are cleanup
and rename.

## Follow-ups explicitly out of scope for this PR

Track as separate issues, not blockers:

- **WS reconnect on subprocess death.** V1 just flags `healthy=False` and
  logs loudly. Subsequent PR should add supervisor logic to respawn with
  exponential backoff.
- **`ws balances` integration.** Would close the "engine balance drifts
  from Kraken balance" class of bugs, but is a separate feature.
- **`ws add-order` replacing REST placement.** Latency improvement, not
  correctness. Defer indefinitely.
- **`ws ticker` / `ws book` / `ws ohlc`.** Correctness-neutral at 5-minute
  tick cadence. Defer.
- **Backfill via `trades-history` for fills older than the snapshot
  window.** Already designed in the data model (`exec_ids` can reference
  any Kraken trade_id) but not implemented — v1 relies on the snapshot
  alone. Add if we see missing entries in practice.
- **Fees in `_compute_pair_realized_pnl`.** The function computes gross
  P&L today. The new shape has `lifecycle.fee_quote` so the refactor
  makes net P&L a trivial addition — but it changes the number everyone's
  been looking at, so do it as a separate, clearly-labeled change.

## Pick-up checklist — FIRST FIVE ACTIONS ON RESUME

If a future session starts cold, do these in order:

1. **Read this document end-to-end.** Don't start coding before.
2. **`git checkout feat/ws-execution-stream`** and `git log --oneline -5`
   to confirm branch state.
3. **Run `python tests/live_harness/harness.py --mode mock`** to confirm
   the pre-conversion baseline passes. You should see 26/26.
4. **Re-run the WS spike** to confirm nothing regressed on the CLI side:
   `python -c "import subprocess, time, json; ..."` (the exact script is
   in this doc under "Spike results"). Expect heartbeats and a snapshot
   line within 6 seconds.
5. **Start at "Order of operations" step 2** (the migrator) and proceed
   sequentially.

## Files modified so far on this branch

- `.gitignore` — added entries for `hydra_order_journal.json` and
  `*.harness_stash`. Not yet committed.
- `docs/WS_EXECUTION_STREAM_HANDOFF.md` — this document. Not yet committed.
- `docs/RECONCILER_PLAN.md` — carried over from main working tree, now
  superseded by this handoff. Safe to delete on the next commit.

No `hydra_agent.py` changes yet. No test changes yet. `hydra_trades_live.json`
is unmodified — still 37-entry repaired state from PR #40.

## Open risks / sharp edges

1. **Engine `adjust_position` method does not yet exist.** The partial-fill
   path depends on it. Fallback: `restore_position(pre) + execute_signal(
   smaller_amount)`. Ugly but works for v1.
2. **`intent.limit_price` is written twice in `_place_order`** — once
   from the initial ticker, once after the re-fetch. Make sure the
   journal entry reflects the second (actually-placed) value, not the
   first.
3. **`order_id` returned by REST `order add` can be `"unknown"`** (see
   the existing `_execute_trade` at line 1264). In that case WS
   correlation falls back to `userref` only. Make sure the stream
   handles "unknown" gracefully — a known `unknown` key would be ambiguous
   across multiple orders. Solution: if `order_id == "unknown"`, skip
   registration and mark `lifecycle.state = PLACED` with a warning, let
   the WS snapshot on the next reconnect catch it.
4. **Paper mode** must not spawn the subprocess. `ExecutionStream(paper=True)`
   is a flag, not a subclass, to keep the code path uniform. Verify by
   running the live harness mock mode — it constructs agents with
   `paper=False` (mock mode mocks the CLI, not paper mode), so the
   subprocess WILL try to spawn. The harness needs to stub
   `ExecutionStream.start()` or provide a `FakeExecutionStream` via
   injection. **This is the single most likely thing to break the harness
   on first run.** Address it explicitly.
5. **Rename spread across many files at once** risks missing a reference.
   After the rename, do a final `grep -rn "trade_log\|OrderReconciler\|
   _execute_trade\|TRADE_LOG_CAP\|EXECUTED\|PLACED_NOT_FILLED\|
   COORDINATED_SWAP\|hydra_trades_live" --include="*.py" --include="*.jsx"`
   and ensure the only remaining hits are intentional (docs, migrator's
   own references).
6. **The 2 legacy phantom entries (OVJ7Q7, OMJVXB) already have status
   `PLACED_NOT_FILLED`** from PR #40. The migrator must map that to
   `CANCELLED_UNFILLED`, NOT `PLACEMENT_FAILED`. Get this right.

## Contact points

- Branch: `feat/ws-execution-stream`
- Parent: `main @ 5e3b673` (PR #40 merge)
- Related PRs: #39 (memory system), #40 (trade-log persistence)
- Related files on disk (gitignored but present):
  `hydra_trades_live.json`, `hydra_session_snapshot.json`,
  `.hydra-memory/graph.json`, `HYDRA_MEMORY.md`
