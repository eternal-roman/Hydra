"""One companion instance, one conversation.

Holds soul, transcript tail, mood state. Orchestrates a single turn:
classify intent -> pick model -> compose context -> call provider ->
journal -> return response. Non-streaming in Phase 1.
"""
from __future__ import annotations
import json
import re
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


# v2.14.2: voice-mode labels are INTERNAL infrastructure. The model's
# system prompt names the mode IDs (mentor, desk_clipped, reflective,
# etc.) so the model can choose cadence, but no user should ever see them
# — self-labeling reads as manipulative. Defense in depth:
#   Layer 1 — compiler.py injects an explicit "do not name" rule into
#             the system prompt.
#   Layer 2 — `_scrub_mode_labels` (below) removes any label that slips
#             through, from both the TurnResult sent to the user AND the
#             transcript entry written to disk (so past leakage cannot
#             prime future turns via the transcript tail).
# The scrubber is built per-companion from `soul.voice_modes`, so a new
# soul with new mode IDs is covered automatically — no hardcoded list.
def _build_mode_scrub_patterns(mode_ids: tuple[str, ...]) -> list[re.Pattern]:
    """Compile the regex patterns that strip mode-label leakage.

    Handles four shapes the model tends to produce when it leaks:
      1. Bracketed / parenthesized tag anywhere: `[mentor]`, `(mentor)`,
         `[mode: mentor]`, `(voice: mentor mode)`.
      2. Line-leading label: `^mentor:`, `^Mentor Mode —`, `^[mentor]\\s`.
      3. Inline meta phrase: `in mentor mode`, `using reflective register`,
         `switching to desk_clipped mode`, `my desk_clipped voice`.
      4. Trailing parenthetical: `... that's the call. (mentor mode)`.

    Only patterns where the mode ID co-occurs with a mode/voice/register
    marker — or brackets — are stripped, so natural uses of common
    English words like "mentor" (Apex talks about Denny, his mentor)
    survive untouched.
    """
    if not mode_ids:
        return []
    # Normalize each mode_id to match both snake_case and space/hyphen
    # variants the model might emit (`desk_clipped`, `desk-clipped`,
    # `desk clipped`). Also match capitalized / Title / UPPER forms.
    id_alternatives = []
    for mid in mode_ids:
        if not mid:
            continue
        escaped = re.escape(mid)
        # Let snake_case underscores also match hyphen or single whitespace
        # (re.escape does not escape `_`, so we replace the bare character).
        flexible = escaped.replace("_", r"[_\-\s]")
        id_alternatives.append(flexible)
    if not id_alternatives:
        return []
    ids_group = "(?:" + "|".join(id_alternatives) + ")"
    patterns: list[re.Pattern] = []
    # 1. Bracketed / parenthesized tag with optional mode: / voice: prefix.
    patterns.append(re.compile(
        rf"\s*[\[\(]\s*(?:mode\s*[:=]\s*|voice\s*[:=]\s*|register\s*[:=]\s*)?"
        rf"{ids_group}(?:\s*(?:mode|register|voice))?\s*[\]\)]",
        re.IGNORECASE,
    ))
    # 2. Line-leading label — `mentor:`, `Mentor Mode —`, etc.
    patterns.append(re.compile(
        rf"(?m)^\s*{ids_group}(?:\s+(?:mode|register|voice))?\s*[:\u2014\u2013\-]\s+",
        re.IGNORECASE,
    ))
    # 3. Inline meta phrase with explicit marker — `in <mode> mode`,
    #    `using <mode> register`, `my <mode> voice`. The trailing marker
    #    word anchors this against false positives on natural English.
    patterns.append(re.compile(
        rf"\b(?:in|using|my|your|in\s+my)\s+"
        rf"{ids_group}\s+(?:mode|register|voice|cadence)\b",
        re.IGNORECASE,
    ))
    # 4. Action-verb + mode_id — `switching to mentor`, `entering
    #    reflective`, `going into serious_mode`. Strong action verbs are
    #    themselves the meta-signal; the trailing marker is optional so
    #    this catches mode IDs that already contain "mode" (Broski's
    #    serious_mode), plus it also optionally eats the trailing
    #    marker if the model emits one.
    patterns.append(re.compile(
        rf"\b(?:switching\s+(?:to|into)|switch\s+to|going\s+into|going\s+to|"
        rf"entering|leaving|exiting|now\s+in)\s+"
        rf"{ids_group}(?:\s+(?:mode|register|voice|cadence))?\b",
        re.IGNORECASE,
    ))
    # 5. Bare "<mode> mode" / "<mode> register" anywhere — catches "I'll
    #    use mentor mode now." The mode marker word is required so
    #    `Denny was my mentor` survives.
    patterns.append(re.compile(
        rf"\b{ids_group}\s+(?:mode|register)\b",
        re.IGNORECASE,
    ))
    return patterns


def _scrub_mode_labels(text: str, patterns: list[re.Pattern]) -> str:
    """Apply compiled mode-label scrub patterns to a response string."""
    if not text or not patterns:
        return text
    out = text
    for p in patterns:
        out = p.sub("", out)
    # Collapse the whitespace residue left by stripped tags — common shape
    # is `[mentor] Ranging regime...` → ` Ranging regime...` → trim.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


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
        # v2.14.2: compile the mode-label scrub regex from the soul's own
        # voice_modes tuple. Done once at init; a new soul gets coverage
        # automatically as long as the voice.modes.modes_available block
        # is populated.
        self._mode_scrub_patterns = _build_mode_scrub_patterns(
            tuple(soul.voice_modes) if soul.voice_modes else ()
        )

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
            "chart_analysis", "trade_proposal", "ladder_proposal",
            "market_state_query", "idle_proactive_nudge",
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

        # v1.2: length-stop continuation. If the provider cut off mid-response
        # because max_tokens was hit, retry once with 2× the budget (capped
        # at 1500). Asks the provider to continue from where it stopped
        # rather than regenerating — cheaper and preserves voice.
        if (not resp.error) and resp.text and self._is_length_stop(resp):
            bumped = min(1500, max(decision.max_tokens * 2, decision.max_tokens + 400))
            continuation_messages = list(messages) + [
                {"role": "assistant", "content": resp.text},
                {"role": "user", "content": "continue from where you stopped — same voice, same mode, no recap"},
            ]
            resp_cont = self.provider.call(
                provider=decision.provider, model_id=decision.model_id,
                system=system, messages=continuation_messages,
                max_tokens=bumped, temperature=decision.temperature,
            )
            if (not resp_cont.error) and resp_cont.text:
                resp = ProviderResponse(
                    text=resp.text + resp_cont.text,
                    tokens_in=resp.tokens_in + resp_cont.tokens_in,
                    tokens_out=resp.tokens_out + resp_cont.tokens_out,
                    cost_usd=resp.cost_usd + resp_cont.cost_usd,
                    model_id=resp.model_id, provider=resp.provider,
                    stop_reason=resp_cont.stop_reason or resp.stop_reason,
                )

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

        # v2.14.2: scrub any internal voice-mode labels the model may
        # have leaked (`[mentor]`, `in mentor mode`, etc.) before the
        # text reaches the user OR the transcript. Applying to BOTH the
        # TurnResult and the journal ensures past leakage cannot prime a
        # future turn via the transcript tail we feed back as context.
        clean_text = _scrub_mode_labels(resp.text, self._mode_scrub_patterns)

        # Journal the successful exchange (user message first, then assistant).
        self._journal("user", user_text)
        self._journal("assistant", clean_text)

        return TurnResult(
            message=clean_text,
            intent=intent_result.intent,
            model_used=f"{decision.provider}:{decision.model_id}",
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            cost_usd=resp.cost_usd,
        )

    @staticmethod
    def _is_length_stop(resp: ProviderResponse) -> bool:
        """True when the provider cut off due to the max_tokens cap.

        Anthropic: stop_reason == 'max_tokens'. xAI (OpenAI-compatible):
        finish_reason == 'length'. Accept both so callers can treat a
        hard-cut response as a signal to request a continuation.
        """
        sr = (resp.stop_reason or "").lower()
        return sr in ("length", "max_tokens")

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
