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
    proposals_enabled, live_execution_enabled,
)
from hydra_companions.executor import (
    LadderProposal, LadderRung, MockExecutor, ProposalValidator,
    TradeProposal, new_ladder_id, new_proposal_id,
)
from hydra_companions.intent_classifier import IntentClassifier
from hydra_companions.providers import ProviderClient
from hydra_companions.router import Router
from hydra_companions.tokens import TokenBroker
import time


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

        # ─── Phase 2+: proposals ───
        self.tokens = TokenBroker(ttl_seconds=60.0)
        self.validator = ProposalValidator(agent=agent, router=self.router)
        # MockExecutor is the default. Phase 3 swaps LiveExecutor in when
        # live_execution_enabled() is true.
        self.executor = MockExecutor(broadcaster=agent.broadcaster)
        self._live_executor = None  # set by _maybe_install_live_executor()
        # Pending proposals awaiting confirm (keyed by proposal_id).
        self._pending: dict[str, tuple[str, object]] = {}   # id -> (kind, proposal)
        # Daily trade-count per companion (for Phase 3 caps; tracked now for observability).
        self._daily_trades: dict[tuple[str, str], int] = defaultdict(int)
        self._maybe_install_live_executor()
        # ─── Phase 4: ladder invalidation watcher ───
        try:
            from hydra_companions.ladder_watcher import LadderWatcher
            self.ladder_watcher = LadderWatcher(agent=agent, broadcaster=agent.broadcaster)
        except Exception:
            self.ladder_watcher = None

        # ─── Phase 6: proactive nudge scheduler ───
        self.nudge_scheduler = None
        try:
            from hydra_companions.config import nudges_enabled
            if nudges_enabled():
                from hydra_companions.nudge_scheduler import NudgeScheduler
                self.nudge_scheduler = NudgeScheduler(coordinator=self, agent=agent)
                self.nudge_scheduler.start()
                print("  [COMPANION] nudge scheduler started")
        except Exception as e:
            print(f"  [COMPANION] nudge scheduler init failed: {e}")

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
        if self.nudge_scheduler is not None:
            self.nudge_scheduler.record_user_activity()
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

    # ----- Phase 2+: proposal flow -----

    def _maybe_install_live_executor(self):
        if not live_execution_enabled():
            return
        try:
            from hydra_companions.live_executor import LiveExecutor
            self._live_executor = LiveExecutor(agent=self.agent, coordinator=self)
            self.executor = self._live_executor
            print("  [COMPANION] live executor installed")
        except Exception as e:
            print(f"  [COMPANION] live executor install failed: {e}; staying on MockExecutor")

    def handle_propose_trade(self, payload: dict) -> Optional[dict]:
        if not proposals_enabled():
            return {"success": False, "error": "proposals disabled (set HYDRA_COMPANION_PROPOSALS_ENABLED=1)"}
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        try:
            pair = str(payload["pair"])
            side = str(payload["side"]).lower()
            size = float(payload["size"])
            limit_price = float(payload["limit_price"])
            stop_loss = float(payload["stop_loss"])
        except (KeyError, TypeError, ValueError) as e:
            return {"success": False, "error": f"bad payload: {e}"}
        rationale = str(payload.get("rationale") or "")
        pid = new_proposal_id()
        now = time.time()
        bundle = self.tokens.mint(pid)
        proposal = TradeProposal(
            proposal_id=pid, companion_id=cid, user_id=uid,
            pair=pair, side=side, size=size, limit_price=limit_price,
            stop_loss=stop_loss, rationale=rationale,
            created_at=now, expires_at=bundle.expires_at,
            risk_usd=abs(limit_price - stop_loss) * size,
            estimated_cost=size * limit_price,
        )
        # Compute risk_pct_equity now so the card can show it
        eq = self.validator._current_equity_usd()
        if eq > 0:
            proposal = TradeProposal(**{**proposal.to_dict(),
                                        "risk_pct_equity": (proposal.risk_usd / eq) * 100})
        vr = self.validator.validate_trade(proposal)
        if not vr.ok:
            return {"success": False, "error": vr.reason, "proposal_id": pid}
        self._pending[pid] = ("trade", proposal)
        self._broadcast("companion.trade.proposal", {
            "proposal_id": pid, "companion_id": cid, "user_id": uid,
            "card": proposal.to_dict(),
            "confirmation_token": bundle.token,
            "nonce": bundle.nonce,
            "ttl_expires_at": bundle.expires_at,
        })
        return {"success": True, "proposal_id": pid}

    def handle_propose_ladder(self, payload: dict) -> Optional[dict]:
        if not proposals_enabled():
            return {"success": False, "error": "proposals disabled"}
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        try:
            pair = str(payload["pair"])
            side = str(payload["side"]).lower()
            total_size = float(payload["total_size"])
            stop_loss = float(payload["stop_loss"])
            invalidation_price = float(payload.get("invalidation_price") or stop_loss)
            rungs_raw = payload["rungs"]
            rungs = tuple(LadderRung(
                pct_of_total=float(r["pct_of_total"]),
                limit_price=float(r["limit_price"]),
                offset_atr=float(r.get("offset_atr") or 0.0),
            ) for r in rungs_raw)
        except (KeyError, TypeError, ValueError) as e:
            return {"success": False, "error": f"bad payload: {e}"}
        rationale = str(payload.get("rationale") or "")
        lid = new_ladder_id()
        now = time.time()
        bundle = self.tokens.mint(lid)
        proposal = LadderProposal(
            proposal_id=lid, companion_id=cid, user_id=uid,
            pair=pair, side=side, total_size=total_size, rungs=rungs,
            stop_loss=stop_loss, invalidation_price=invalidation_price,
            rationale=rationale, created_at=now, expires_at=bundle.expires_at,
            risk_usd=abs((rungs[0].limit_price if rungs else 0) - stop_loss) * total_size,
        )
        eq = self.validator._current_equity_usd()
        if eq > 0:
            proposal = LadderProposal(**{**proposal.to_dict(), "rungs": tuple(rungs),
                                         "risk_pct_equity": (proposal.risk_usd / eq) * 100})
        vr = self.validator.validate_ladder(proposal)
        if not vr.ok:
            return {"success": False, "error": vr.reason, "proposal_id": lid}
        self._pending[lid] = ("ladder", proposal)
        self._broadcast("companion.ladder.proposal", {
            "proposal_id": lid, "companion_id": cid, "user_id": uid,
            "card": proposal.to_dict(),
            "confirmation_token": bundle.token,
            "nonce": bundle.nonce,
            "ttl_expires_at": bundle.expires_at,
        })
        return {"success": True, "proposal_id": lid}

    def handle_confirm(self, payload: dict) -> Optional[dict]:
        pid = payload.get("proposal_id")
        token = payload.get("confirmation_token")
        nonce = payload.get("nonce")
        expires_at = payload.get("ttl_expires_at") or 0
        if not pid or pid not in self._pending:
            return {"success": False, "error": "unknown or expired proposal"}
        if not self.tokens.verify(proposal_id=pid, token=token,
                                  nonce=nonce, expires_at=expires_at):
            self._pending.pop(pid, None)
            return {"success": False, "error": "bad/expired token"}
        kind, proposal = self._pending.pop(pid)
        # Re-validate at confirm time (regime could have moved).
        if kind == "trade":
            vr = self.validator.validate_trade(proposal)
        else:
            vr = self.validator.validate_ladder(proposal)
        if not vr.ok:
            self._broadcast("companion.trade.failed", {
                "proposal_id": pid, "companion_id": proposal.companion_id,
                "reason": vr.reason,
            })
            return {"success": False, "error": vr.reason}
        # Daily cap check — enforcing when live execution is on.
        if live_execution_enabled():
            cap = self.router.safety_cap(proposal.companion_id, "max_trades_per_day", 0)
            if cap > 0:
                count = self._daily_trades.get((proposal.user_id, proposal.companion_id), 0)
                if count >= cap:
                    self._broadcast("companion.trade.failed", {
                        "proposal_id": pid, "companion_id": proposal.companion_id,
                        "reason": f"daily cap hit ({count}/{cap})",
                    })
                    return {"success": False, "error": f"daily cap hit ({count}/{cap})"}
        try:
            if kind == "trade":
                self.executor.execute_trade(proposal)
            else:
                self.executor.execute_ladder(proposal)
            self._daily_trades[(proposal.user_id, proposal.companion_id)] += 1
            return {"success": True, "proposal_id": pid}
        except Exception as e:
            self._broadcast("companion.trade.failed", {
                "proposal_id": pid, "companion_id": proposal.companion_id,
                "reason": f"{type(e).__name__}: {e}",
            })
            return {"success": False, "error": str(e)}

    def handle_reject(self, payload: dict) -> Optional[dict]:
        pid = payload.get("proposal_id")
        self._pending.pop(pid, None)
        return {"success": True}

    # ----- Phase 5: memory API -----

    def handle_remember(self, payload: dict) -> Optional[dict]:
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        comp = self.get(cid, uid)
        if comp is None:
            return {"success": False, "error": f"unknown companion: {cid}"}
        topic = payload.get("topic") or "general"
        fact = payload.get("fact") or ""
        if not fact.strip():
            return {"success": False, "error": "fact required"}
        comp.memory.remember(topic, fact)
        return {"success": True, "companion_id": cid, "topic": topic.lower()}

    def handle_recall(self, payload: dict) -> Optional[dict]:
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        comp = self.get(cid, uid)
        if comp is None:
            return {"success": False, "error": f"unknown companion: {cid}"}
        topic = payload.get("topic")
        entries = comp.memory.recall(topic)
        return {
            "success": True, "companion_id": cid,
            "entries": [{"ts": e.ts, "topic": e.topic, "fact": e.fact} for e in entries],
        }

    def handle_forget(self, payload: dict) -> Optional[dict]:
        cid = (payload.get("companion_id") or "apex").lower()
        uid = payload.get("user_id") or DEFAULT_USER_ID
        comp = self.get(cid, uid)
        if comp is None:
            return {"success": False, "error": f"unknown companion: {cid}"}
        topic = payload.get("topic")  # None = forget all
        removed = comp.memory.forget(topic)
        return {"success": True, "companion_id": cid, "removed": removed, "topic": topic}

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
            # Always clear typing and surface the note in-thread.
            self._broadcast("companion.typing", {
                "companion_id": cid, "user_id": uid, "state": "idle",
                "message_id": msg_id,
            })
            self._broadcast("companion.message.complete", {
                "message_id": msg_id, "companion_id": cid, "user_id": uid,
                "text": "busy \u2014 try again in a moment",
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
                "text": f"(internal error: {type(e).__name__}: {e})",
                "error": str(e),
            })
        finally:
            # Explicit typing clear so the UI never gets stuck.
            try:
                self._broadcast("companion.typing", {
                    "companion_id": cid, "user_id": uid, "state": "idle",
                    "message_id": msg_id,
                })
            except Exception:
                pass
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
