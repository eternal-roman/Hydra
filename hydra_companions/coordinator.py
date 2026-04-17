"""Per-session companion registry + thread pool.

One coordinator per agent. Holds three companions for the default user.
Dispatches WS messages to the right companion, runs the LLM call on a
background thread so the WS loop stays responsive, broadcasts results
back via the agent's broadcaster.

Daily cost budgets are tracked per companion. When a companion hits
its hard_stop_pct the coordinator rejects new turns and broadcasts a
cost_alert instead.
"""
from __future__ import annotations
import json
import threading
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from hydra_companions.compiler import load_all_souls
from hydra_companions.companion import Companion
from hydra_companions.config import (
    COMPANION_IDS, DEFAULT_USER_ID, ensure_runtime_dirs,
)
from hydra_companions.intent_classifier import IntentClassifier
from hydra_companions.providers import ProviderClient
from hydra_companions.router import Router


MAX_WORKERS = 3


class CompanionCoordinator:
    def __init__(self, agent):
        ensure_runtime_dirs()
        self.agent = agent
        self.router = Router()
        self.classifier = IntentClassifier()
        self.provider = ProviderClient()

        souls = load_all_souls()
        self._companions: dict[tuple[str, str], Companion] = {}
        for sid in COMPANION_IDS:
            if sid in souls:
                self._companions[(DEFAULT_USER_ID, sid)] = Companion(
                    soul=souls[sid], agent=agent,
                    router=self.router, classifier=self.classifier,
                    provider=self.provider, user_id=DEFAULT_USER_ID,
                )

        # Daily cost tracking per companion
        self._cost_lock = threading.Lock()
        self._daily_costs: dict[tuple[str, str], float] = defaultdict(float)
        self._day_key: str = _utc_day_key()
        self._alert_fired: set[tuple[str, str]] = set()

        # Bounded thread pool so one flood of messages can't spawn unbounded threads
        self._busy = threading.BoundedSemaphore(MAX_WORKERS)

    # ----- public -----

    def get(self, companion_id: str, user_id: str = DEFAULT_USER_ID) -> Optional[Companion]:
        return self._companions.get((user_id, companion_id))

    def handle_connect(self, payload: dict) -> Optional[dict]:
        """Reply-style handler for companion.connect \u2014 returns hello payload."""
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        comp = self.get(cid, uid)
        if comp is None:
            return {"success": False, "error": f"unknown companion: {cid}"}
        tail = [t for t in comp.transcript[-20:] if t.get("role") in ("user", "assistant")]
        return {
            "success": True,
            "companion": comp.meta(),
            "history_tail": tail,
            "all_companions": [
                self._companions[(uid, sid)].meta()
                for sid in COMPANION_IDS
                if (uid, sid) in self._companions
            ],
        }

    def handle_message(self, payload: dict) -> None:
        """Kick off an LLM call in a background thread. No reply here \u2014
        results broadcast asynchronously as companion.message.complete."""
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        text = (payload.get("text") or "").strip()
        msg_id = payload.get("message_id") or str(uuid.uuid4())
        if not text:
            return
        comp = self.get(cid, uid)
        if comp is None:
            self._broadcast("companion.system_note", {
                "text": f"unknown companion {cid}",
                "message_id": msg_id,
            })
            return
        if self._is_over_budget(uid, cid):
            self._broadcast("companion.message.complete", {
                "message_id": msg_id, "companion_id": cid, "user_id": uid,
                "text": f"({comp.soul.display_name} has hit today's cost cap; back online tomorrow.)",
                "model_used": "budget-capped",
                "intent": "budget_stop",
            })
            return
        t = threading.Thread(
            target=self._run_turn, args=(comp, cid, uid, text, msg_id), daemon=True,
        )
        t.start()

    def handle_switch(self, payload: dict) -> Optional[dict]:
        """Return the new active companion's meta + history tail."""
        to = (payload.get("to_id") or payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        comp = self.get(to, uid)
        if comp is None:
            return {"success": False, "error": f"unknown companion: {to}"}
        tail = [t for t in comp.transcript[-20:] if t.get("role") in ("user", "assistant")]
        return {"success": True, "companion": comp.meta(), "history_tail": tail}

    # ----- internals -----

    def _run_turn(self, comp: Companion, cid: str, uid: str, text: str, msg_id: str):
        acquired = self._busy.acquire(blocking=False)
        if not acquired:
            self._broadcast("companion.system_note", {
                "text": "busy \u2014 try again in a moment",
                "message_id": msg_id,
            })
            return
        try:
            self._broadcast("companion.typing", {
                "companion_id": cid, "user_id": uid, "state": "thinking",
                "message_id": msg_id,
            })
            result = comp.respond(text)
            self._record_cost(uid, cid, result.cost_usd)
            self._broadcast("companion.message.complete", {
                "message_id": msg_id,
                "companion_id": cid,
                "user_id": uid,
                "text": result.message,
                "intent": result.intent,
                "model_used": result.model_used,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost_usd": round(result.cost_usd, 6),
                "error": result.error,
            })
        except Exception as e:
            traceback.print_exc()
            self._broadcast("companion.message.complete", {
                "message_id": msg_id, "companion_id": cid, "user_id": uid,
                "text": f"(internal error: {type(e).__name__})",
                "error": str(e),
            })
        finally:
            self._busy.release()

    def _broadcast(self, msg_type: str, payload: dict) -> None:
        try:
            self.agent.broadcaster.broadcast_message(msg_type, payload)
        except Exception:
            pass

    # ----- budget tracking -----

    def _is_over_budget(self, uid: str, cid: str) -> bool:
        budget = self.router.daily_budget_usd(cid)
        if budget <= 0:
            return False
        with self._cost_lock:
            self._maybe_rollover()
            return self._daily_costs[(uid, cid)] >= budget

    def _record_cost(self, uid: str, cid: str, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        budget = self.router.daily_budget_usd(cid)
        with self._cost_lock:
            self._maybe_rollover()
            self._daily_costs[(uid, cid)] += cost_usd
            current = self._daily_costs[(uid, cid)]
            alert_key = (uid, cid)
            if budget > 0 and current >= 0.8 * budget and alert_key not in self._alert_fired:
                self._alert_fired.add(alert_key)
                self._broadcast("companion.cost_alert", {
                    "user_id": uid, "companion_id": cid,
                    "daily_cost_usd": round(current, 4),
                    "threshold_usd": round(0.8 * budget, 4),
                    "hard_stop_usd": budget,
                })

    def _maybe_rollover(self) -> None:
        today = _utc_day_key()
        if today != self._day_key:
            self._daily_costs.clear()
            self._alert_fired.clear()
            self._day_key = today


def _utc_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
