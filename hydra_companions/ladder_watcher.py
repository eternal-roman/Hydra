"""LadderWatcher \u2014 background monitor for invalidation thresholds.

Pattern: one daemon thread per LadderWatcher instance, polling the
agent's latest price snapshot every POLL_INTERVAL_S seconds. For each
active ladder, if price crosses the invalidation_price in the wrong
direction, cancel the remaining unfilled rungs and broadcast
companion.ladder.invalidation_triggered.

This is a minimal viable watcher. It treats any rung lacking a fill
signal as "unfilled" \u2014 a production version would integrate with
ExecutionStream's userref-attributed fill tracking. For Phase 4,
cancellation is best-effort; the user ultimately still sees remaining
orders on Kraken if the cancel fails.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


POLL_INTERVAL_S = 2.0


@dataclass
class ActiveLadder:
    proposal_id: str
    companion_id: str
    user_id: str
    pair: str
    side: str                # "buy" | "sell"
    invalidation_price: float
    stop_loss: float
    rungs: list              # placed-rung dicts from live_executor
    cancelled: bool = False
    filled_idx: set = field(default_factory=set)


class LadderWatcher:
    def __init__(self, *, agent, broadcaster):
        self.agent = agent
        self.broadcaster = broadcaster
        self._active: dict[str, ActiveLadder] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="LadderWatcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def register(self, ladder_proposal, placed_rungs: list, autostart: bool = True) -> None:
        """Start watching a ladder that was just placed."""
        with self._lock:
            self._active[ladder_proposal.proposal_id] = ActiveLadder(
                proposal_id=ladder_proposal.proposal_id,
                companion_id=ladder_proposal.companion_id,
                user_id=ladder_proposal.user_id,
                pair=ladder_proposal.pair,
                side=ladder_proposal.side,
                invalidation_price=float(ladder_proposal.invalidation_price),
                stop_loss=float(ladder_proposal.stop_loss),
                rungs=list(placed_rungs),
            )
        if autostart and not (self._thread and self._thread.is_alive()):
            self.start()

    def mark_fill(self, proposal_id: str, rung_idx: int) -> None:
        """ExecutionStream hook \u2014 Phase 4+ can drive this when a rung fills."""
        with self._lock:
            act = self._active.get(proposal_id)
            if act:
                act.filled_idx.add(rung_idx)

    def deregister(self, proposal_id: str) -> None:
        with self._lock:
            self._active.pop(proposal_id, None)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._active.values() if not a.cancelled)

    # ----- internal -----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")
            if self._stop.wait(POLL_INTERVAL_S):
                return

    def _tick(self) -> None:
        snap = getattr(self.broadcaster, "latest_state", {}) or {}
        pairs = snap.get("pairs") or {}
        with self._lock:
            ladders = list(self._active.values())
        for lad in ladders:
            if lad.cancelled:
                continue
            pdata = pairs.get(lad.pair) or {}
            price = pdata.get("price")
            if not isinstance(price, (int, float)):
                continue
            invalidated = (lad.side == "buy" and price < lad.invalidation_price) \
                       or (lad.side == "sell" and price > lad.invalidation_price)
            if invalidated:
                self._invalidate(lad, price)

    def _invalidate(self, lad: ActiveLadder, current_price: float) -> None:
        cancelled_userrefs = []
        try:
            from hydra_agent import KrakenCLI
            for i, rung in enumerate(lad.rungs):
                if i in lad.filled_idx:
                    continue
                userref = rung.get("userref")
                txid = rung.get("txid")
                # Prefer userref-based cancel if the CLI supports it.
                try:
                    if hasattr(KrakenCLI, "cancel_order"):
                        if userref is not None:
                            KrakenCLI.cancel_order(userref=int(userref))
                        elif txid:
                            KrakenCLI.cancel_order(txid=txid)
                except Exception as e:
                    import logging; logging.warning(f"Ignored exception: {e}")
                cancelled_userrefs.append(userref)
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        with self._lock:
            lad.cancelled = True
        try:
            self.broadcaster.broadcast_message(
                "companion.ladder.invalidation_triggered", {
                    "proposal_id": lad.proposal_id,
                    "companion_id": lad.companion_id,
                    "user_id": lad.user_id,
                    "pair": lad.pair,
                    "current_price": current_price,
                    "invalidation_price": lad.invalidation_price,
                    "cancelled_userrefs": cancelled_userrefs,
                },
            )
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")
