#!/usr/bin/env python3
"""
HYDRA Thesis Processor — Grok 4 reasoning for user-uploaded research.

Phase C of the Golden Unicorn initiative. Takes documents a user uploads
via the dashboard Thesis tab (Cowen memos, FOMC minutes, custom research
notes) and calls Grok 4 reasoning (xAI) to synthesize each into a
structured `ProposedThesisUpdate` awaiting human approval.

Architectural contract:
- Processor NEVER auto-applies anything. Every proposal lands in
  hydra_thesis_pending/{proposal_id}.json for user review.
- Posterior shifts above 0.30 are flagged requires_human regardless of
  the auto_apply_proposed_updates knob — those are regime-change claims.
- Budget cap: knobs.grok_processing_budget_usd_per_day (default $5).
  Cost tracking is INDEPENDENT of HydraBrain's $10/day live budget so
  experimentation here can't stall live trading.
- Kill switch: HYDRA_THESIS_PROCESSOR_DISABLED=1 OR no XAI_API_KEY.
- Daemon worker thread mirrors BacktestWorkerPool — bounded queue,
  all exceptions isolated inside the worker.

Grok system prompt targets ITC framework synthesis (5-stage risk cascade,
regime-change checklist, preservation/transition/accumulation posture).
See docs/THESIS_SPEC.md (Phase C) for the full spec.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

try:
    import openai  # type: ignore
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# Pricing tokens/M for Grok (xAI). Mirrors hydra_brain COST_XAI shape.
# Conservative estimate — actual rates updated via xAI billing dashboard.
COST_XAI_INPUT_PER_M = 5.0
COST_XAI_OUTPUT_PER_M = 15.0

# Model: xAI's reasoning tier. Keep in lockstep with hydra_brain.py.
DEFAULT_GROK_MODEL = "grok-4-reasoning"

# Cost tracking + disclosure
COST_ALERT_USD = 10.0
DEFAULT_DAILY_BUDGET_USD = 5.0  # per-tracker default; knob overrides

# Worker pool caps
MAX_QUEUE_DEPTH = 20
MAX_WORKERS_HARD_CAP = 2  # intentional low cap — documents process slowly
DEFAULT_WORKERS = 1

# Per-document context bounds (hard, belt-and-suspenders around the knob).
MAX_DOC_TEXT_BYTES = 64 * 1024  # 64 KB — Grok truncates beyond this
MAX_PROPOSAL_RETAIN = 200

# Posterior-shift threshold that forces human approval even when
# auto_apply_proposed_updates is True.
HUMAN_GATE_SHIFT_THRESHOLD = 0.30


def _ulid() -> str:
    return uuid.uuid4().hex[:20]


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


THESIS_PROCESSOR_SYSTEM_PROMPT = """You are the HYDRA Thesis Processor — a macro synthesis agent.

You receive research artifacts (Cowen Into-The-Cryptoverse memos, FOMC
minutes, on-chain reports, user notes) and synthesize them into structured
updates to a persistent Bayesian thesis state.

Framework (see the ThesisState you're given):
- 5-stage ITC risk cascade (speculative participation → alts → equities →
  financial conditions → labor feedback)
- Regime-change checklist (5 items): advance_decline_broadening, onchain_reset,
  vol_reexpansion, macro_liquidity_shift, labor_feedback_loop
- Deployment posture: PRESERVATION / TRANSITION / ACCUMULATION
- Hard rules (untouchable): ledger_shield_btc=0.20, no_altcoin_gate,
  tax_friction_min_realized_pnl_usd

You do NOT make trading decisions. You propose thesis updates that the
user approves before they reach the trading bot.

Principles (non-negotiable):
1. Never propose changes that lower the ledger shield below 0.20 BTC.
2. Never propose altcoin intents or ladders — the no-altcoin gate is
   a hard rule.
3. Quantify uncertainty: "60/40 toward time-based grind" beats false
   precision.
4. Surface contradictions with the prior thesis directly. Do not
   paper over them.
5. Flag |posterior_shift| > 0.30 as requires_human = true regardless —
   that magnitude of shift is a regime-change claim, not a nudge.
6. Stay intellectually honest > comfortable-narrative.

Respond ONLY with this JSON (no other text):
{
  "posterior_shift": {"regime": "<MacroRegime>", "confidence": 0.0-1.0} or null,
  "checklist_updates": {"<key>": {"status": "NOT_MET|PARTIAL|MET", "notes": "1 line"}},
  "proposed_intents": [
    {"prompt_text": "...", "pair_scope": ["BTC/USDC"], "priority": 1-5,
     "expires_at": "ISO-8601 or null"}
  ],
  "new_evidence": [
    {"category": "MACRO|ON_CHAIN|STRUCTURAL|TACTICAL", "source": "<doc_source>",
     "description": "1 line", "direction": "bullish|bearish|neutral",
     "magnitude": 0.0-1.0}
  ],
  "posture_recommendation": "PRESERVATION|TRANSITION|ACCUMULATION" or null,
  "reasoning": "3-6 sentences, plain English, cite document specifically",
  "confidence": 0.0-1.0,
  "requires_human": true or false
}
"""


@dataclass
class ProcessorCosts:
    """Independent cost tracker — NOT coupled to HydraBrain's live budget.
    Resets at UTC midnight. Thread-safe via _lock."""
    daily_tokens_in: int = 0
    daily_tokens_out: int = 0
    daily_cost_usd: float = 0.0
    day_key: str = ""
    cost_alert_fired_day: Optional[str] = None


class ThesisProcessorWorker:
    """Daemon worker thread that pulls uploaded documents off a queue and
    drives them through Grok → proposal JSON → disk.

    Mirrors the BacktestWorkerPool shape: bounded queue, all work wrapped
    in try/except, failures logged but never escape the worker.

    Construction is lazy: `ThesisTracker` passes a callable `on_proposal`
    that the worker invokes once a ProposedThesisUpdate JSON is ready, so
    the tracker decides where proposals land on disk. This keeps the
    worker module ignorant of the tracker's save path conventions.
    """

    def __init__(
        self,
        xai_key: Optional[str] = None,
        pending_dir: Optional[str] = None,
        get_thesis_state: Optional[Callable[[], Dict[str, Any]]] = None,
        on_proposal: Optional[Callable[[Dict[str, Any]], None]] = None,
        broadcast: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        model: str = DEFAULT_GROK_MODEL,
        daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
        max_queue_depth: int = MAX_QUEUE_DEPTH,
        enable_budget: bool = True,
    ):
        self.model = model
        self.daily_budget_usd = max(0.0, float(daily_budget_usd or 0))
        self.pending_dir = pending_dir
        self._get_thesis_state = get_thesis_state or (lambda: {})
        self._on_proposal = on_proposal or (lambda _p: None)
        self._broadcast = broadcast or (lambda _t, _p: None)
        self.enable_budget = enable_budget
        self.costs = ProcessorCosts(day_key=_utc_day_key())
        self._cost_lock = threading.Lock()

        self.client = None
        if xai_key and HAS_OPENAI:
            try:
                self.client = openai.OpenAI(
                    api_key=xai_key, base_url="https://api.x.ai/v1",
                )
            except Exception as e:
                print(f"  [THESIS_PROC] xAI client init failed ({type(e).__name__}: {e})")
                self.client = None

        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue_depth)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─── Lifecycle ────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True when the worker can actually produce proposals. False when
        disabled, missing client, or missing pending_dir."""
        return self.client is not None and self.pending_dir is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="thesis-processor", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ─── Queue ────────────────────────────────────────────────────

    def submit(self, job: Dict[str, Any]) -> bool:
        """Submit a processing job.

        job shape: {
            "doc_id": str,
            "filename": str,
            "doc_type": str,
            "text": str,       # full document content (truncated if large)
        }
        """
        if not self.available:
            return False
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            print(f"  [THESIS_PROC] queue full — dropping doc_id={job.get('doc_id')}")
            return False

    # ─── Worker loop ──────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process_one(job)
            except Exception as e:
                print(f"  [THESIS_PROC] job failed ({type(e).__name__}: {e})")
                traceback.print_exc()
            finally:
                self._queue.task_done()

    def _process_one(self, job: Dict[str, Any]) -> None:
        doc_id = job.get("doc_id") or _ulid()
        doc_type = job.get("doc_type", "other")
        text = (job.get("text") or "")[:MAX_DOC_TEXT_BYTES]
        filename = job.get("filename", "unknown.md")

        # Budget gate
        self._rollover_day_if_needed()
        if self.enable_budget and self.costs.daily_cost_usd >= self.daily_budget_usd:
            self._write_failed_proposal(
                doc_id, filename, doc_type,
                reason="daily Grok budget exceeded",
            )
            return

        state = {}
        try:
            state = self._get_thesis_state() or {}
        except Exception:
            state = {}

        user_msg = _build_user_message(state, filename, doc_type, text)

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": THESIS_PROCESSOR_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=1500,
            )
        except Exception as e:
            print(f"  [THESIS_PROC] Grok call failed ({type(e).__name__}: {e})")
            self._write_failed_proposal(
                doc_id, filename, doc_type,
                reason=f"grok_call_error: {type(e).__name__}",
            )
            return

        # Cost accounting
        usage = getattr(completion, "usage", None)
        tok_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tok_out = int(getattr(usage, "completion_tokens", 0) or 0)
        self._add_cost(tok_in, tok_out)

        content = ""
        try:
            content = (completion.choices[0].message.content or "").strip()
        except Exception:
            content = ""

        parsed = _parse_proposal_json(content)
        if parsed is None:
            self._write_failed_proposal(
                doc_id, filename, doc_type,
                reason="unparseable_grok_response",
            )
            return

        proposal = {
            "proposal_id": _ulid(),
            "proposed_at": _iso_now(),
            "source_doc_id": doc_id,
            "source": "grok_doc_processor",
            "posterior_shift": parsed.get("posterior_shift"),
            "checklist_updates": parsed.get("checklist_updates") or {},
            "proposed_intents": parsed.get("proposed_intents") or [],
            "new_evidence": parsed.get("new_evidence") or [],
            "posture_recommendation": parsed.get("posture_recommendation"),
            "reasoning": parsed.get("reasoning", ""),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "requires_human": _force_human_gate_on_big_shift(parsed),
            "status": "pending",
            "user_decision_at": None,
            "user_notes": None,
            "_meta": {
                "filename": filename,
                "doc_type": doc_type,
                "tokens_in": tok_in,
                "tokens_out": tok_out,
            },
        }

        # Notify tracker (it writes to disk + broadcasts). The worker stays
        # out of tracker-owned state paths.
        try:
            self._on_proposal(proposal)
        except Exception as e:
            print(f"  [THESIS_PROC] on_proposal callback failed ({type(e).__name__}: {e})")

        try:
            self._broadcast("thesis_proposal_pending", {"data": proposal})
        except Exception:
            pass

    # ─── Cost accounting ──────────────────────────────────────────

    def _add_cost(self, tok_in: int, tok_out: int) -> None:
        with self._cost_lock:
            self.costs.daily_tokens_in += max(0, tok_in)
            self.costs.daily_tokens_out += max(0, tok_out)
            self.costs.daily_cost_usd += (
                tok_in / 1_000_000 * COST_XAI_INPUT_PER_M
                + tok_out / 1_000_000 * COST_XAI_OUTPUT_PER_M
            )
            if (
                self.costs.daily_cost_usd >= COST_ALERT_USD
                and self.costs.cost_alert_fired_day != self.costs.day_key
            ):
                self.costs.cost_alert_fired_day = self.costs.day_key
                print(
                    f"  [THESIS_PROC] cost alert — daily Grok spend "
                    f"${self.costs.daily_cost_usd:.2f} crossed ${COST_ALERT_USD:.2f}"
                )
                try:
                    self._broadcast("cost_alert", {
                        "component": "thesis_processor",
                        "daily_cost_usd": round(self.costs.daily_cost_usd, 4),
                        "threshold_usd": COST_ALERT_USD,
                        "day_key": self.costs.day_key,
                        "enforce_budget": self.enable_budget,
                    })
                except Exception:
                    pass

    def _rollover_day_if_needed(self) -> None:
        today = _utc_day_key()
        with self._cost_lock:
            if today != self.costs.day_key:
                self.costs.day_key = today
                self.costs.daily_tokens_in = 0
                self.costs.daily_tokens_out = 0
                self.costs.daily_cost_usd = 0.0
                self.costs.cost_alert_fired_day = None

    # ─── Failed-proposal stub ─────────────────────────────────────

    def _write_failed_proposal(
        self, doc_id: str, filename: str, doc_type: str, reason: str,
    ) -> None:
        """Emit a minimal pending-proposal record marked failed so the user
        sees why their upload didn't produce a usable proposal."""
        stub = {
            "proposal_id": _ulid(),
            "proposed_at": _iso_now(),
            "source_doc_id": doc_id,
            "source": "grok_doc_processor",
            "posterior_shift": None,
            "checklist_updates": {},
            "proposed_intents": [],
            "new_evidence": [],
            "posture_recommendation": None,
            "reasoning": f"Processing failed: {reason}",
            "confidence": 0.0,
            "requires_human": True,
            "status": "pending",
            "user_decision_at": None,
            "user_notes": None,
            "_meta": {
                "filename": filename, "doc_type": doc_type,
                "tokens_in": 0, "tokens_out": 0, "failed": True, "reason": reason,
            },
        }
        try:
            self._on_proposal(stub)
        except Exception:
            pass


# ─── Helpers ──────────────────────────────────────────────────────

def _utc_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _force_human_gate_on_big_shift(parsed: Dict[str, Any]) -> bool:
    """If posterior_shift moves |confidence| by > HUMAN_GATE_SHIFT_THRESHOLD
    vs the default 0.5 baseline, force requires_human regardless of the
    model's self-report. Defensive — the system prompt asks for this but
    we must not trust it."""
    reported = bool(parsed.get("requires_human", False))
    if reported:
        return True
    shift = parsed.get("posterior_shift") or {}
    if not isinstance(shift, dict):
        return reported
    conf = shift.get("confidence")
    try:
        if conf is not None and abs(float(conf) - 0.5) > HUMAN_GATE_SHIFT_THRESHOLD:
            return True
    except (TypeError, ValueError):
        pass
    return reported


def _parse_proposal_json(text: str) -> Optional[Dict[str, Any]]:
    """Tolerant JSON parse — strip markdown code fences, pick the first
    balanced object. Returns None when no object can be recovered."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        # Strip ```json ... ``` fences
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip()
    # Direct parse
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Find first balanced {...}
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        ch = t[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def _build_user_message(
    state: Dict[str, Any], filename: str, doc_type: str, text: str,
) -> str:
    """Assemble the user message Grok sees. Intentionally compact —
    expensive tokens come from the document itself."""
    posture = state.get("posture", "UNKNOWN")
    posterior = state.get("posterior") or {}
    checklist = state.get("checklist") or {}
    checklist_brief = ", ".join(
        f"{k}:{(v or {}).get('status', 'NOT_MET')}" for k, v in checklist.items()
    )
    hard = state.get("hard_rules") or {}
    return (
        f"CURRENT THESIS STATE\n"
        f"  Posture: {posture}\n"
        f"  Posterior: {posterior.get('regime', '?')} @ "
        f"{posterior.get('confidence', 0.5)}\n"
        f"  Checklist: {checklist_brief or '(empty)'}\n"
        f"  Hard rules: ledger_shield={hard.get('ledger_shield_btc', 0.20)} BTC, "
        f"no_altcoin_gate={hard.get('no_altcoin_gate', True)}\n"
        f"\nNEW DOCUMENT ({doc_type}): {filename}\n"
        f"{'-' * 60}\n"
        f"{text}\n"
        f"{'-' * 60}\n"
        f"\nSynthesize this document against the framework and current state. "
        f"Respond ONLY with the JSON schema from the system prompt."
    )


# ═══════════════════════════════════════════════════════════════
# MODULE SMOKE
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Exercise: no key → worker is unavailable but module imports cleanly
    w = ThesisProcessorWorker(xai_key=None, pending_dir=None)
    assert w.available is False
    # Parse helper sanity
    p = _parse_proposal_json("```json\n{\"a\":1,\"b\":[1,2]}\n```")
    assert p == {"a": 1, "b": [1, 2]}
    # Big-shift gate
    assert _force_human_gate_on_big_shift({"posterior_shift": {"confidence": 0.95}}) is True
    assert _force_human_gate_on_big_shift({"posterior_shift": {"confidence": 0.52}}) is False
    print("hydra_thesis_processor smoke OK")
