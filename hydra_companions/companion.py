"""One companion instance, one conversation.

Holds soul, transcript tail, mood state. Orchestrates a single turn:
classify intent -> pick model -> compose context -> call provider ->
journal -> return response. Non-streaming in Phase 1.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from typing import Optional

from hydra_companions.compiler import CompiledSoul
from hydra_companions.intent_classifier import IntentClassifier, IntentResult
from hydra_companions.memory import DistilledMemory
from hydra_companions.providers import ProviderClient, ProviderResponse
from hydra_companions.router import Router, RouteDecision
from hydra_companions import tools_readonly
from hydra_companions.config import TRANSCRIPTS_DIR, ROUTING_LOG, COSTS_LOG


TRANSCRIPT_TAIL_TURNS = 20


@dataclass
class TurnResult:
    message: str
    intent: str
    model_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error: Optional[str] = None


class Companion:
    def __init__(self, soul: CompiledSoul, *, agent, router: Router,
                 classifier: IntentClassifier, provider: ProviderClient,
                 user_id: str = "local"):
        self.soul = soul
        self.agent = agent
        self.router = router
        self.classifier = classifier
        self.provider = provider
        self.user_id = user_id
        self.transcript: list[dict] = []
        # Per-soul default mood label \u2014 pulled from mood_model.default in
        # compiler.py. The mood is currently a static label surfaced in
        # meta() for the UI. Dynamic mood transitions (VOLATILE \u2192
        # "cautionary", drawdown \u2192 "sober", etc.) are a Phase-7
        # follow-up once trigger wiring is in place.
        self.mood: str = getattr(soul, "default_mood", None) or "calm"
        # Broski-specific: a flag that the router reads to lower LLM
        # temperature when real risk is on the table. Currently only
        # reachable manually via `companion.set_serious_mode` WS route
        # (see coordinator.handle_set_serious_mode). Automated triggers
        # (rent-money language, tilt detection) are Phase-7 work.
        self.serious_mode: bool = False
        self._transcript_path = TRANSCRIPTS_DIR / f"{user_id}_{soul.id}.jsonl"
        self._load_transcript_tail()
        # Phase 5: distilled memory (topic-bucketed facts)
        self.memory = DistilledMemory(user_id=user_id, companion_id=soul.id)

    # ----- lifecycle -----

    def _load_transcript_tail(self) -> None:
        if not self._transcript_path.exists():
            return
        try:
            lines = self._transcript_path.read_text(encoding="utf-8").splitlines()
            tail = lines[-TRANSCRIPT_TAIL_TURNS * 2:]
            for ln in tail:
                try:
                    self.transcript.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    def _journal(self, role: str, content: str) -> None:
        entry = {"ts": time.time(), "role": role, "content": content}
        self.transcript.append(entry)
        try:
            with self._transcript_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        # Keep in-memory transcript bounded
        if len(self.transcript) > TRANSCRIPT_TAIL_TURNS * 3:
            self.transcript = self.transcript[-TRANSCRIPT_TAIL_TURNS * 2:]

    def clear_transcript(self) -> int:
        """Clear this companion's conversation. Returns # messages removed.

        Distilled memory (topic-bucketed facts) is NOT touched \u2014 use
        /forget or the memory WS route for that."""
        count = len(self.transcript)
        self.transcript = []
        try:
            if self._transcript_path.exists():
                self._transcript_path.unlink()
        except Exception:
            pass
        return count

    # ----- turn execution -----

    def respond(self, user_text: str) -> TurnResult:
        intent_result = self.classifier.classify(user_text)
        decision = self.router.pick(
            self.soul.id, intent_result.intent,
            serious_mode=self.serious_mode,
        )

        # Build the context-augmented user message. Cheap replacement
        # for tool use in Phase 1.
        needs_market = intent_result.intent in {
            "market_state_query", "trade_proposal", "ladder_proposal",
            "teaching_explanation", "idle_proactive_nudge",
            "chart_analysis",
        }
        # v1.1: chart + journal inclusion is gated on the soul's allowlist.
        # We pass a minimal soul dict to compose_context_blob so it can
        # check capabilities.tool_access without needing the full soul JSON.
        include_chart = intent_result.intent in {
            "chart_analysis", "trade_proposal", "ladder_proposal",
        }
        include_journal = intent_result.intent in {
            "chart_analysis", "trade_proposal",
        }
        soul_for_gate = {
            "id": self.soul.id,
            "capabilities": {"tool_access": list(self.soul.tool_access)},
        }
        augmented = user_text
        if needs_market:
            blob = tools_readonly.compose_context_blob(
                self.agent,
                max_bytes=2048,
                soul=soul_for_gate,
                include_chart=include_chart,
                include_journal_tail=include_journal,
            )
            if blob:
                augmented = f"{user_text}\n\n[LIVE CONTEXT]\n{blob}"

        # Build messages from transcript tail + new user message.
        messages = []
        for turn in self.transcript[-TRANSCRIPT_TAIL_TURNS * 2:]:
            if turn["role"] in ("user", "assistant"):
                messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": augmented})

        # Phase 5: append distilled memory to the system prompt (per-turn).
        memory_block = self.memory.compose_block()
        system = self.soul.system_prompt + ("\n\n" + memory_block if memory_block else "")

        resp = self.provider.call(
            provider=decision.provider,
            model_id=decision.model_id,
            system=system,
            messages=messages,
            max_tokens=decision.max_tokens,
            temperature=decision.temperature,
        )

        # Fallback cascade on error — walk the whole fallback chain until
        # one provider responds without error, or all candidates exhausted.
        if resp.error:
            tried = [f"{decision.provider}:{decision.model_id}"]
            current = decision
            for _ in range(4):  # hard cap so we never loop forever
                fb = self.router.fallback(current, already_tried=tried)
                if fb is None:
                    break
                tried.append(f"{fb.provider}:{fb.model_id}")
                resp2 = self.provider.call(
                    provider=fb.provider, model_id=fb.model_id,
                    system=system, messages=messages,
                    max_tokens=fb.max_tokens, temperature=fb.temperature,
                )
                if not resp2.error:
                    resp = resp2
                    decision = fb
                    break
                current = fb

        self._log_routing(intent_result, decision, resp)
        self._log_cost(resp)

        if resp.error or not resp.text:
            err_text = resp.error or "empty response"
            return TurnResult(
                message=f"(unable to reach {decision.provider}: {err_text})",
                intent=intent_result.intent,
                model_used=f"{decision.provider}:{decision.model_id}",
                tokens_in=0, tokens_out=0, cost_usd=0.0, error=err_text,
            )

        # Journal the successful exchange (user message first, then assistant).
        self._journal("user", user_text)
        self._journal("assistant", resp.text)

        return TurnResult(
            message=resp.text,
            intent=intent_result.intent,
            model_used=f"{decision.provider}:{decision.model_id}",
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            cost_usd=resp.cost_usd,
        )

    # ----- logging -----

    def _log_routing(self, intent: IntentResult, decision: RouteDecision,
                     resp: ProviderResponse) -> None:
        try:
            with ROUTING_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "user_id": self.user_id,
                    "companion_id": self.soul.id,
                    "intent": intent.intent,
                    "intent_confidence": intent.confidence,
                    "classifier_method": intent.method,
                    "model_selected": f"{decision.provider}:{decision.model_id}",
                    "tokens_in": resp.tokens_in,
                    "tokens_out": resp.tokens_out,
                    "cost_usd": resp.cost_usd,
                    "error": resp.error,
                    "serious_mode": self.serious_mode,
                }) + "\n")
        except Exception:
            pass

    def _log_cost(self, resp: ProviderResponse) -> None:
        if resp.cost_usd <= 0:
            return
        try:
            with COSTS_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "user_id": self.user_id,
                    "companion_id": self.soul.id,
                    "cost_usd": resp.cost_usd,
                    "tokens_in": resp.tokens_in,
                    "tokens_out": resp.tokens_out,
                    "model": f"{resp.provider}:{resp.model_id}",
                }) + "\n")
        except Exception:
            pass

    # ----- UI-surface metadata -----

    def meta(self) -> dict:
        return {
            "id": self.soul.id,
            "display_name": self.soul.display_name,
            "sigil": self.soul.sigil,
            "color_theme": self.soul.color_theme,
            "mood": self.mood,
            "serious_mode": self.serious_mode,
        }
