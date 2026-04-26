#!/usr/bin/env python3
"""
HYDRA Shadow Validation Daemon (Phase 11 of v2.10.0 backtest platform).

Runs a reviewer-approved `PARAM_TWEAK` candidate alongside live trading
for N trades. Only candidates that actually outperform live by
`min_improvement_pct` during the observation window become eligible for
human promotion to the live tuner.

Why shadow validation exists
----------------------------
Rigor gates (Phase 7) are necessary but not sufficient. A change can
clear all 7 gates on synthetic/historical data and still underperform
live because:
  * Data distribution differs (real fills, real spread, real latency)
  * Regime regime differs from training window
  * Non-stationarity

Shadow validation adds a final safety check: "actually run it, see if it
still wins." Single-slot FIFO (spec C8) prevents contamination — only
one shadow experiment runs at a time.

Integration points (Phase 12 mount — not wired in this phase)
--------------------------------------------------------------
  * Dashboard sends `shadow_promote` WS message -> agent enqueues
  * Agent's tick loop -> `validator.ingest_candle(pair, candle)` after
    the live ingest, BEFORE live signal generation
  * Agent's trade close path -> `validator.record_live_close(pair, trade)`
  * Agent's tick loop -> `validator.poll_complete()` each tick; on
    terminal result, broadcasts `shadow_validation_result` for the UI
  * Human clicks "Approve" on dashboard -> `validator.approve(id)` ->
    `HydraTuner.apply_external_param_update(...)` write path

The module is fully usable standalone (tests drive it without the
agent). Integration is deliberately deferred to keep live code touch
surface minimal.

Design invariants
-----------------
  * Single active slot. Subsequent submits queue behind it.
  * Shadow engine runs with the proposed override applied via
    `HydraEngine.apply_tuned_params`. No real orders; signal-level
    tracking only.
  * Shadow never touches the live agent's engines (I2).
  * Persistence: `.hydra-experiments/shadow_state.json` — atomic write,
    restore on startup. Survives agent restart.
  * Thread-safe: a single lock serializes submit/approve/reject and
    internal state mutations. Tick hooks are the hot path and also
    acquire the lock (briefly); the agent's tick impact is bounded.

See docs/BACKTEST_SPEC.md §6.9 (Shadow validator / tuner write path).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hydra_engine import Candle, HydraEngine, SIZING_CONSERVATIVE, SIZING_COMPETITION
from hydra_backtest import _iso_utc_now
from hydra_reviewer import ProposedChange


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

DEFAULT_MIN_TRADES = 10            # live trades needed before decision
DEFAULT_MIN_IMPROVEMENT_PCT = 0.5  # shadow must exceed live by this %
DEFAULT_WINDOW_TIMEOUT_SEC = 7 * 86400.0   # auto-reject after 1 week of inactivity
DEFAULT_STORE_FILENAME = "shadow_state.json"


# ═══════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class ShadowCandidate:
    """One candidate going through shadow validation.

    `pair` is either a specific pair (e.g., "SOL/USD") when the proposed
    change is pair-scoped, or "*" for global scope (each live pair gets
    its own shadow engine). `proposed_overrides` carries the JSON-safe
    copy of {param: value} so we can serialize/restore without needing
    the original ProposedChange object.
    """
    id: str
    experiment_id: str
    pair: str                                  # "SOL/USD" | "*" (global)
    proposed_overrides: Dict[str, float]
    created_at: str
    status: str = "pending"                    # pending | active | approved
                                               # | rejected | expired | cancelled
    min_trades: int = DEFAULT_MIN_TRADES
    min_improvement_pct: float = DEFAULT_MIN_IMPROVEMENT_PCT

    # Telemetry
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    trades_observed: int = 0                   # live trades during window
    live_pnl_sum: float = 0.0                  # cumulative realized live P&L
    shadow_pnl_sum: float = 0.0                # hypothetical cumulative shadow P&L
    per_pair_live_pnl: Dict[str, float] = field(default_factory=dict)
    per_pair_shadow_pnl: Dict[str, float] = field(default_factory=dict)
    rationale: str = ""                        # populated when terminal
    decision_at: Optional[str] = None
    decision_by: str = ""                      # "auto" | "human:<name>" | "timeout"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    candidate_id: str
    verdict: str                               # "approve_eligible" | "rejected"
                                               # | "still_running" | "expired"
    live_pnl_sum: float
    shadow_pnl_sum: float
    delta_pct: float                           # shadow over live, %
    trades_evaluated: int
    rationale: str
    completed_at: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# ShadowValidator
# ═══════════════════════════════════════════════════════════════

class ShadowValidator:
    """Single-slot shadow-validation daemon. See module docstring."""

    def __init__(
        self,
        tuner_registry: Optional[Dict[str, Any]] = None,
        min_trades: int = DEFAULT_MIN_TRADES,
        min_improvement_pct: float = DEFAULT_MIN_IMPROVEMENT_PCT,
        window_timeout_sec: float = DEFAULT_WINDOW_TIMEOUT_SEC,
        store_root: Optional[Path] = None,
        broadcaster: Optional[Any] = None,
        mode: str = "conservative",
        initial_balance_per_pair: float = 100.0,
        candle_interval: int = 15,
        on_state_change: Optional[Callable[[str, ShadowCandidate], None]] = None,
    ) -> None:
        self.tuner_registry = tuner_registry or {}
        self.min_trades = min_trades
        self.min_improvement_pct = min_improvement_pct
        self.window_timeout_sec = window_timeout_sec
        self.store_root = Path(store_root) if store_root else Path(".hydra-experiments")
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.store_root / DEFAULT_STORE_FILENAME
        self.broadcaster = broadcaster
        self.mode = mode
        self.initial_balance_per_pair = initial_balance_per_pair
        self.candle_interval = candle_interval
        self.on_state_change = on_state_change

        self._lock = threading.RLock()
        self._queue: List[ShadowCandidate] = []          # FIFO; [0] is active
        self._shadow_engines: Dict[str, HydraEngine] = {}  # pair -> engine
        self._restore()

    # ─── Submission ───

    def submit(
        self,
        proposed: ProposedChange,
        experiment_id: str,
        pair: Optional[str] = None,
    ) -> str:
        """Queue a candidate. Returns its id. Raises on invalid proposal."""
        if proposed.change_type != "param":
            raise ValueError(
                f"shadow validator only accepts 'param' changes; got {proposed.change_type!r}"
            )
        if not proposed.target or proposed.proposed_value is None:
            raise ValueError("proposed change missing target or proposed_value")

        # Resolve pair scope: explicit `pair` arg > proposed.scope (e.g.
        # "pair:SOL/USD") > "*".
        if pair is None:
            if proposed.scope.startswith("pair:"):
                pair = proposed.scope.split(":", 1)[1]
            else:
                pair = "*"

        overrides = {proposed.target: float(proposed.proposed_value)}

        cand = ShadowCandidate(
            id=uuid.uuid4().hex,
            experiment_id=experiment_id,
            pair=pair,
            proposed_overrides=overrides,
            created_at=_iso_utc_now(),
            min_trades=self.min_trades,
            min_improvement_pct=self.min_improvement_pct,
        )

        with self._lock:
            self._queue.append(cand)
            # If this is the only candidate, activate immediately.
            if len(self._queue) == 1:
                self._activate(cand)
            self._persist()
        self._notify("submitted", cand)
        return cand.id

    # ─── Queue introspection ───

    def current(self) -> Optional[ShadowCandidate]:
        with self._lock:
            if self._queue and self._queue[0].status == "active":
                return self._queue[0]
            return None

    def queue_snapshot(self) -> List[ShadowCandidate]:
        with self._lock:
            return list(self._queue)

    def history(self, limit: int = 20) -> List[ShadowCandidate]:
        """Return the most recent terminal candidates, newest first.

        Terminal statuses: approved | rejected | expired | cancelled.
        """
        terminal = {"approved", "rejected", "expired", "cancelled"}
        with self._lock:
            items = [c for c in self._queue if c.status in terminal]
        items.sort(key=lambda c: c.completed_at or c.created_at, reverse=True)
        return items[:limit]

    # ─── Tick-time hooks (hot path; agent tick calls these) ───

    def ingest_candle(self, pair: str, candle: Candle) -> None:
        """Forward a live candle to the matching shadow engine.

        Called from the agent tick loop AFTER live's own `ingest_candle`
        so both consume identical data. Noop when no active candidate or
        when the active candidate's scope doesn't include `pair`.
        """
        with self._lock:
            active = self._active_or_none()
            if active is None:
                return
            engine = self._shadow_engines.get(pair)
            if engine is None:
                return
            # Feed the candle and generate a signal — but DO NOT execute.
            # Shadow tracks hypothetical P&L via its own position updates;
            # we don't call the agent's order path.
            engine.ingest_candle({
                "open": candle.open, "high": candle.high, "low": candle.low,
                "close": candle.close, "volume": candle.volume,
                "timestamp": candle.timestamp,
            })
            try:
                engine.tick(generate_only=False)
            except Exception:
                # Shadow faults must never affect live. Silently continue;
                # the overall window result still lands.
                pass

    def record_live_close(self, pair: str, live_trade: Dict[str, Any]) -> None:
        """Called by the live agent when a live trade CLOSES (SELL profit
        realized). Updates the candidate's per-pair live pnl tally and
        advances trades_observed. If the minimum is hit, computes and
        emits a ValidationResult via the broadcaster / on_state_change
        callback.
        """
        if live_trade.get("side") != "SELL":
            return
        profit = float(live_trade.get("profit", 0.0) or 0.0)
        if profit == 0.0:
            return  # BUY entries or zero-pnl closes don't count
        with self._lock:
            active = self._active_or_none()
            if active is None:
                return
            if active.pair != "*" and active.pair != pair:
                return
            active.trades_observed += 1
            active.live_pnl_sum += profit
            active.per_pair_live_pnl[pair] = (
                active.per_pair_live_pnl.get(pair, 0.0) + profit
            )
            # Compute shadow hypothetical P&L snapshot
            self._refresh_shadow_pnl(active)
            self._persist()
        self._notify("progress", active)

    def tick(self) -> None:
        """Called each agent tick; runs timeout check + poll. Cheap."""
        with self._lock:
            active = self._active_or_none()
            if active is None:
                return
            # Timeout: if a candidate hasn't closed its window after N days
            # of insufficient trades, expire it rather than leaving the
            # shadow hanging forever.
            if active.started_at and self.window_timeout_sec > 0:
                started_ts = _parse_iso(active.started_at)
                if started_ts and (time.time() - started_ts) > self.window_timeout_sec:
                    self._finalize(active, "expired",
                                   rationale=f"window timeout after {self.window_timeout_sec}s",
                                   decision_by="timeout")

    def poll_complete(self) -> Optional[ValidationResult]:
        """Returns a ValidationResult if the active candidate just reached
        its decision threshold (approve_eligible / rejected / expired),
        else None. The result is advisory — approval still requires an
        explicit human `approve()` call for `approve_eligible`."""
        with self._lock:
            active = self._active_or_none()
            if active is None:
                return None
            if active.trades_observed < active.min_trades:
                return None
            # Compute verdict
            self._refresh_shadow_pnl(active)
            delta_pct = self._delta_pct(active)
            if delta_pct >= active.min_improvement_pct:
                active.status = "active"   # still active until human approves
                rationale = (
                    f"shadow +{delta_pct:.2f}% over live across "
                    f"{active.trades_observed} trades -> eligible for promotion"
                )
                return ValidationResult(
                    candidate_id=active.id,
                    verdict="approve_eligible",
                    live_pnl_sum=active.live_pnl_sum,
                    shadow_pnl_sum=active.shadow_pnl_sum,
                    delta_pct=delta_pct,
                    trades_evaluated=active.trades_observed,
                    rationale=rationale,
                )
            # Not enough improvement -> auto-reject
            rationale = (
                f"shadow {delta_pct:+.2f}% vs live (threshold "
                f"{active.min_improvement_pct:+.2f}%) -> auto-reject"
            )
            self._finalize(active, "rejected", rationale=rationale,
                           decision_by="auto")
            return ValidationResult(
                candidate_id=active.id,
                verdict="rejected",
                live_pnl_sum=active.live_pnl_sum,
                shadow_pnl_sum=active.shadow_pnl_sum,
                delta_pct=delta_pct,
                trades_evaluated=active.trades_observed,
                rationale=rationale,
                completed_at=active.completed_at,
            )

    # ─── Human-in-the-loop decisions ───

    def approve(self, candidate_id: str, approver: str = "human") -> Dict[str, Any]:
        """Promote a validated candidate to live via the tuner write path.

        Only applies when the candidate is the current active slot AND
        has reached `approve_eligible`. Returns a dict describing the
        tuner application; raises on misuse.
        """
        with self._lock:
            active = self._active_or_none()
            if active is None or active.id != candidate_id:
                raise ValueError(f"candidate {candidate_id} is not the active slot")
            if active.trades_observed < active.min_trades:
                raise ValueError("candidate has not completed validation window")
            if self._delta_pct(active) < active.min_improvement_pct:
                raise ValueError("candidate did not meet improvement threshold")

            applied: Dict[str, Any] = {}
            if self.tuner_registry:
                target_pairs = (
                    [p for p in self.tuner_registry.keys()]
                    if active.pair == "*" else [active.pair]
                )
                for p in target_pairs:
                    tracker = self.tuner_registry.get(p)
                    if tracker is None:
                        continue
                    res = tracker.apply_external_param_update(
                        active.proposed_overrides,
                        source=f"shadow_validator:{approver}",
                    )
                    applied[p] = res

            self._finalize(active, "approved",
                           rationale="human approved promotion",
                           decision_by=f"human:{approver}")
        self._notify("approved", active)
        return {"candidate_id": candidate_id, "applied": applied,
                "pair_scope": active.pair}

    def reject(self, candidate_id: str, reason: str = "manual",
               rejected_by: str = "human") -> bool:
        with self._lock:
            # Allow rejecting either the active slot OR any pending queued item
            for cand in self._queue:
                if cand.id != candidate_id:
                    continue
                if cand.status in {"approved", "rejected", "expired", "cancelled"}:
                    return False
                self._finalize(cand, "rejected", rationale=reason,
                               decision_by=f"human:{rejected_by}")
                self._notify("rejected", cand)
                return True
        return False

    def cancel(self, candidate_id: str) -> bool:
        """Cancel a pending or active candidate without promoting."""
        with self._lock:
            for cand in self._queue:
                if cand.id != candidate_id:
                    continue
                if cand.status in {"approved", "rejected", "expired", "cancelled"}:
                    return False
                self._finalize(cand, "cancelled", rationale="cancelled",
                               decision_by="operator")
                self._notify("cancelled", cand)
                return True
        return False

    def rollback_last_approval(self) -> Dict[str, Any]:
        """Revert the most recent approved candidate's tuner write.

        Delegates to each target tuner's `rollback_to_previous`. Returns
        a dict of pair -> whether rollback succeeded. Does NOT re-queue
        the candidate — rolled-back changes stay rejected-on-disk.
        """
        results: Dict[str, bool] = {}
        if not self.tuner_registry:
            return results
        for pair, tracker in self.tuner_registry.items():
            try:
                ok = tracker.rollback_to_previous()
            except Exception:
                ok = False
            results[pair] = ok
        return results

    # ─── Internals ───

    def _active_or_none(self) -> Optional[ShadowCandidate]:
        if not self._queue:
            return None
        c = self._queue[0]
        return c if c.status == "active" else None

    def _activate(self, cand: ShadowCandidate) -> None:
        """Spin up shadow engines for the candidate and mark it active."""
        cand.status = "active"
        cand.started_at = _iso_utc_now()
        self._build_shadow_engines(cand)
        self._notify("activated", cand)

    def _build_shadow_engines(self, cand: ShadowCandidate) -> None:
        """Create HydraEngine instances with the proposed overrides.

        Pair scope handling:
          * "*": one engine per known pair (via tuner_registry keys),
                 or fall back to ["SOL/USD"] if registry empty.
          * "SOL/USD" / "SOL/USD" / etc: single-engine scope.
        """
        self._shadow_engines.clear()
        pairs: List[str]
        if cand.pair == "*":
            pairs = list(self.tuner_registry.keys()) or ["SOL/USD"]
        else:
            pairs = [cand.pair]

        sizing = SIZING_COMPETITION if self.mode == "competition" else SIZING_CONSERVATIVE
        for p in pairs:
            engine = HydraEngine(
                initial_balance=self.initial_balance_per_pair,
                asset=p,
                sizing=sizing,
                candle_interval=self.candle_interval,
            )
            # Seed with current tuner params (if any) then apply proposed overrides
            tracker = self.tuner_registry.get(p)
            if tracker is not None:
                try:
                    engine.apply_tuned_params(tracker.get_tunable_params())
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")
            try:
                engine.apply_tuned_params(cand.proposed_overrides)
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            self._shadow_engines[p] = engine

    def _refresh_shadow_pnl(self, cand: ShadowCandidate) -> None:
        """Snapshot shadow engine realized + unrealized P&L into cand."""
        total = 0.0
        per_pair: Dict[str, float] = {}
        for pair, engine in self._shadow_engines.items():
            # Shadow pnl = current balance + unrealized - initial
            try:
                unrealized = (engine.position.unrealized_pnl
                              if engine.position.size > 0 else 0.0)
            except AttributeError:
                unrealized = 0.0
            pnl = (engine.balance - engine.initial_balance) + unrealized
            per_pair[pair] = pnl
            total += pnl
        cand.shadow_pnl_sum = total
        cand.per_pair_shadow_pnl = per_pair

    @staticmethod
    def _delta_pct(cand: ShadowCandidate) -> float:
        """Shadow over live as a percentage. Safe-math for zero denominators."""
        live = cand.live_pnl_sum
        shadow = cand.shadow_pnl_sum
        if abs(live) < 1e-9:
            # Live made no money; shadow positive wins, shadow non-positive loses
            return 100.0 if shadow > 0 else (-100.0 if shadow < 0 else 0.0)
        return (shadow - live) / abs(live) * 100.0

    def _finalize(
        self,
        cand: ShadowCandidate,
        status: str,
        rationale: str,
        decision_by: str,
    ) -> None:
        cand.status = status
        cand.rationale = rationale
        cand.completed_at = _iso_utc_now()
        cand.decision_at = cand.completed_at
        cand.decision_by = decision_by
        # Tear down shadow engines — next candidate gets fresh ones
        self._shadow_engines.clear()
        # Append outcome to shadow_outcomes.jsonl so the reviewer's
        # self_retrospective() can compute accuracy (M16: feedback loop).
        self._log_outcome(cand)
        # Advance the queue: remove the candidate from the head; activate next pending
        if self._queue and self._queue[0] is cand:
            # Keep it in the queue as terminal history (we'll trim in _persist)
            self._queue.pop(0)
            self._queue.append(cand)   # append for history at tail
        # Pick next pending
        for c in self._queue:
            if c.status == "pending":
                # Move pending to head so current() finds it
                self._queue.remove(c)
                self._queue.insert(0, c)
                self._activate(c)
                break
        self._persist()

    def _log_outcome(self, cand: ShadowCandidate) -> None:
        """Append a terminal outcome record to shadow_outcomes.jsonl.

        Consumed by `ResultReviewer.self_retrospective()` to compute the
        reviewer's historical accuracy. One line per finalize call; append-
        only; schema-stable (new fields appended, never removed).
        """
        try:
            path = self.store_root / "shadow_outcomes.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "candidate_id": cand.id,
                "experiment_id": cand.experiment_id,
                "status": cand.status,
                "pair": cand.pair,
                "trades_observed": cand.trades_observed,
                "live_pnl_sum": cand.live_pnl_sum,
                "shadow_pnl_sum": cand.shadow_pnl_sum,
                "delta_pct": self._delta_pct(cand),
                "decision_by": cand.decision_by,
                "completed_at": cand.completed_at,
                "rationale": cand.rationale,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, default=_json_default) + "\n")
        except Exception:
            # Outcome logging is strictly observability — never block finalize.
            pass

    # ─── Persistence ───

    def _persist(self) -> None:
        """Atomic write of the full queue state."""
        try:
            path = self.state_path
            payload = {
                "version": 1,
                "updated_at": _iso_utc_now(),
                "queue": [c.to_dict() for c in self._queue],
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True,
                              default=_json_default)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError as e:
                    import logging; logging.warning(f"Ignored exception: {e}")
        except Exception:
            # Persistence must never crash the validator; we'll rebuild
            # from scratch on next run if this fails.
            pass

    def _restore(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        queue_data = raw.get("queue") or []
        restored: List[ShadowCandidate] = []
        for d in queue_data:
            try:
                # Cast dict back; unknown fields are skipped. For forward
                # compat: any new field defaults via dataclass defaults.
                restored.append(ShadowCandidate(
                    id=d["id"],
                    experiment_id=d["experiment_id"],
                    pair=d["pair"],
                    proposed_overrides=dict(d["proposed_overrides"]),
                    created_at=d["created_at"],
                    status=d.get("status", "pending"),
                    min_trades=d.get("min_trades", DEFAULT_MIN_TRADES),
                    min_improvement_pct=d.get("min_improvement_pct",
                                              DEFAULT_MIN_IMPROVEMENT_PCT),
                    started_at=d.get("started_at"),
                    completed_at=d.get("completed_at"),
                    trades_observed=d.get("trades_observed", 0),
                    live_pnl_sum=d.get("live_pnl_sum", 0.0),
                    shadow_pnl_sum=d.get("shadow_pnl_sum", 0.0),
                    per_pair_live_pnl=d.get("per_pair_live_pnl", {}) or {},
                    per_pair_shadow_pnl=d.get("per_pair_shadow_pnl", {}) or {},
                    rationale=d.get("rationale", ""),
                    decision_at=d.get("decision_at"),
                    decision_by=d.get("decision_by", ""),
                ))
            except (TypeError, KeyError, ValueError):
                continue
        self._queue = restored
        # Re-build shadow engines for any candidate whose status is still
        # "active". Trades_observed / pnl stay from the saved snapshot.
        for c in self._queue:
            if c.status == "active":
                self._build_shadow_engines(c)
                break

    # ─── Broadcast + callback ───

    def _notify(self, event: str, cand: ShadowCandidate) -> None:
        if self.on_state_change:
            try:
                self.on_state_change(event, cand)
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
        if self.broadcaster and hasattr(self.broadcaster, "broadcast_message"):
            try:
                self.broadcaster.broadcast_message("shadow_state", {
                    "event": event,
                    "candidate": cand.to_dict(),
                })
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        t = time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return time.mktime(t) - time.timezone
    except (ValueError, TypeError):
        return None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ═══════════════════════════════════════════════════════════════
# CLI smoke
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    import tempfile as _tempfile
    from hydra_reviewer import ProposedChange as _PC
    from hydra_tuner import ParameterTracker

    tmp = Path(_tempfile.mkdtemp(prefix="hydra-shadow-smoke-"))
    print(f"[shadow smoke] store: {tmp}")

    tracker = ParameterTracker(pair="SOL/USD", save_dir=str(tmp))
    v = ShadowValidator(
        tuner_registry={"SOL/USD": tracker},
        min_trades=3,
        store_root=tmp,
    )

    proposed = _PC(
        change_type="param",
        scope="pair:SOL/USD",
        target="momentum_rsi_upper",
        current_value=70.0,
        proposed_value=75.0,
        expected_impact={"sharpe": 0.3},
    )
    cid = v.submit(proposed, experiment_id="exp-smoke")
    print(f"[shadow smoke] submitted: {cid}")
    print(f"[shadow smoke] active: {v.current().id if v.current() else None}")
    # Simulate 3 live closes, alternating live win / big loss so shadow wins
    v.record_live_close("SOL/USD", {"side": "SELL", "profit": 1.0})
    v.record_live_close("SOL/USD", {"side": "SELL", "profit": -5.0})
    v.record_live_close("SOL/USD", {"side": "SELL", "profit": 0.5})
    res = v.poll_complete()
    print(f"[shadow smoke] poll_complete: {res}")
    if res and res.verdict == "approve_eligible":
        applied = v.approve(cid, approver="cli")
        print(f"[shadow smoke] applied: {applied}")
    print(f"[shadow smoke] history: {[c.id[:8] for c in v.history()]}")
