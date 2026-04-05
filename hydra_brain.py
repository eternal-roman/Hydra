#!/usr/bin/env python3
"""
HYDRA Brain — Multi-Agent AI Reasoning Layer (3-Agent Pipeline)

Agent 1: Market Analyst (Claude Sonnet) — fast technical analysis
Agent 2: Risk Manager (Claude Sonnet) — risk assessment and approval
Agent 3: Strategic Advisor (Grok 4 Reasoning) — deep analysis on contested decisions

Grok only fires when the pipeline disagrees (ADJUST/OVERRIDE) or conviction is low.
Clear CONFIRM signals skip Grok entirely to save cost.

Usage:
    brain = HydraBrain(anthropic_key="sk-ant-...", xai_key="xai-...")
    decision = brain.deliberate(engine_state)
"""

import json
import math
import time
import os
import re
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Deque
from datetime import datetime, timezone

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════

@dataclass
class GoalState:
    """Explicit, trackable objectives the brain reasons against every tick.

    Unlike a one-shot risk gate, the brain uses this to plan multi-tick
    trajectories: when `risk_posture` shifts to `conservative` because
    realised drawdown is approaching `drawdown_budget_pct`, planned steps
    that would add exposure are invalidated and re-planned.
    """
    target_sharpe: float = 1.0
    drawdown_budget_pct: float = 10.0
    session_pnl_target_pct: float = 5.0
    risk_posture: str = "neutral"  # conservative | neutral | aggressive

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanStep:
    """A single step in a multi-tick plan.

    `if_condition` is a small dict of thresholds checked against the live
    state. `then_action` is what the brain wants to do when the condition
    matches. `success_metric` is the signal used by the reflection loop to
    mark the plan step as successful or failed after the fact.
    """
    if_condition: Dict[str, Any]
    then_action: Dict[str, Any]
    success_metric: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | fired | succeeded | failed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrainPlan:
    """Multi-step plan the brain commits to across ticks."""
    goal: str
    horizon_ticks: int
    created_at_tick: int
    steps: List[PlanStep] = field(default_factory=list)
    step_idx: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "horizon_ticks": self.horizon_ticks,
            "created_at_tick": self.created_at_tick,
            "step_idx": self.step_idx,
            "steps": [s.to_dict() for s in self.steps],
        }

    def is_expired(self, current_tick: int) -> bool:
        return (current_tick - self.created_at_tick) >= self.horizon_ticks

    def current_step(self) -> Optional[PlanStep]:
        if 0 <= self.step_idx < len(self.steps):
            return self.steps[self.step_idx]
        return None


@dataclass
class Episode:
    """One decision + (eventually) its realised outcome."""
    tick: int
    timestamp: float
    pair: str
    state_digest: List[float]   # small indicator vector for similarity search
    action: str
    signal: str
    confidence: float
    realized_pnl: Optional[float] = None
    regret: Optional[float] = None  # counterfactual: HOLD return over K candles
    closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BrainMemory:
    """Cross-tick episodic memory + beliefs + current plan per pair.

    The memory is intentionally small and pure-Python so it can be JSON-
    serialised into the session snapshot without touching any dependency.

    - `episodes[pair]` holds the last `MAX_EPISODES` decisions for each pair,
      which the agent loop retrieves via a cheap cosine similarity over the
      state digest vector (7 dims).
    - `beliefs` are free-form scalar convictions the brain updates during
      reflection (e.g. "sol_usdc_mean_reversion_edge" ∈ [-1, 1]).
    - `plans[pair]` holds the currently-active multi-step plan for a pair.
    - `goals` is shared across all pairs.
    """

    MAX_EPISODES = 200
    DIGEST_DIM = 7

    def __init__(self, goals: Optional[GoalState] = None):
        self.episodes: Dict[str, Deque[Episode]] = {}
        self.beliefs: Dict[str, Dict[str, float]] = {}   # {name: {value, n}}
        self.plans: Dict[str, BrainPlan] = {}
        self.goals: GoalState = goals or GoalState()

    # ─── episodes ───

    def add_episode(self, episode: Episode):
        dq = self.episodes.setdefault(
            episode.pair, deque(maxlen=self.MAX_EPISODES)
        )
        dq.append(episode)

    def recent_episodes(self, pair: str, n: int = 10) -> List[Episode]:
        dq = self.episodes.get(pair)
        if not dq:
            return []
        return list(dq)[-n:]

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def retrieve_similar(self, pair: str, digest: List[float], k: int = 5) -> List[Episode]:
        dq = self.episodes.get(pair)
        if not dq:
            return []
        closed = [e for e in dq if e.closed]
        if not closed:
            return []
        scored = sorted(
            closed, key=lambda e: self._cosine(digest, e.state_digest), reverse=True
        )
        return scored[:k]

    # ─── beliefs ───

    def update_belief(self, name: str, observation: float, alpha: float = 0.2):
        """Exponential-moving-average belief update bounded to [-1, 1]."""
        rec = self.beliefs.setdefault(name, {"value": 0.0, "n": 0})
        rec["value"] = (1 - alpha) * rec["value"] + alpha * max(-1.0, min(1.0, observation))
        rec["n"] += 1

    def get_belief(self, name: str) -> float:
        return self.beliefs.get(name, {}).get("value", 0.0)

    # ─── plan ───

    def set_plan(self, pair: str, plan: BrainPlan):
        self.plans[pair] = plan

    def get_plan(self, pair: str) -> Optional[BrainPlan]:
        return self.plans.get(pair)

    def clear_plan(self, pair: str):
        self.plans.pop(pair, None)

    # ─── (de)serialisation for session snapshot ───

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episodes": {
                pair: [e.to_dict() for e in dq]
                for pair, dq in self.episodes.items()
            },
            "beliefs": self.beliefs,
            "plans": {p: plan.to_dict() for p, plan in self.plans.items()},
            "goals": self.goals.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrainMemory":
        goals_data = data.get("goals") or {}
        mem = cls(goals=GoalState(**{k: v for k, v in goals_data.items() if k in GoalState.__dataclass_fields__}))
        for pair, eps in (data.get("episodes") or {}).items():
            dq: Deque[Episode] = deque(maxlen=cls.MAX_EPISODES)
            for e in eps:
                dq.append(Episode(**{
                    k: v for k, v in e.items()
                    if k in Episode.__dataclass_fields__
                }))
            mem.episodes[pair] = dq
        mem.beliefs = dict(data.get("beliefs") or {})
        for pair, plan in (data.get("plans") or {}).items():
            steps = [PlanStep(**s) for s in plan.get("steps", [])]
            mem.plans[pair] = BrainPlan(
                goal=plan.get("goal", ""),
                horizon_ticks=int(plan.get("horizon_ticks", 20)),
                created_at_tick=int(plan.get("created_at_tick", 0)),
                steps=steps,
                step_idx=int(plan.get("step_idx", 0)),
            )
        return mem


@dataclass
class BrainDecision:
    action: str                    # "CONFIRM", "ADJUST", "OVERRIDE"
    final_signal: str              # "BUY", "SELL", "HOLD"
    confidence_adj: float          # adjusted confidence 0-1
    size_multiplier: float         # 0.0–1.5
    analyst_reasoning: str         # Claude analyst thesis
    risk_reasoning: str            # Claude risk assessment
    strategist_reasoning: str = "" # Grok strategic analysis (if escalated)
    combined_summary: str = ""     # one-line for trade log
    risk_flags: List[str] = field(default_factory=list)
    portfolio_health: str = "HEALTHY"
    tokens_used: int = 0
    latency_ms: float = 0.0
    fallback: bool = False
    escalated: bool = False        # True if Grok was consulted


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ═══════════════════════════════════════════════════════════════

ANALYST_PROMPT = """You are HYDRA's Market Analyst, an expert crypto technical analyst inside an autonomous trading system. Analyze the market snapshot and evaluate the engine's signal.

Your analysis must be:
1. CONCISE — max 3 sentences for the thesis
2. ACTIONABLE — agree or disagree with the engine signal, with specific reasons
3. QUANTITATIVE — reference actual indicator values

Respond ONLY with this JSON (no other text):
{
  "thesis": "1-3 sentence market thesis",
  "signal_agreement": true or false,
  "suggested_action": "BUY" or "SELL" or "HOLD",
  "conviction": 0.0 to 1.0,
  "key_factors": ["factor1", "factor2"],
  "concern": "primary risk or null"
}"""

RISK_MANAGER_PROMPT = """You are HYDRA's Risk Manager. You protect capital above all else. You receive the engine's signal, the analyst's thesis, and portfolio state.

Your mandate:
- NEVER allow a trade when drawdown exceeds 10% — only HOLD or SELL
- Scale down size_multiplier when multiple risk factors align
- Override to HOLD if analyst and engine disagree and conviction < 0.6
- Override to SELL if drawdown is accelerating
- CONFIRM good setups with size_multiplier 1.0
- ADJUST by lowering size_multiplier (0.3–0.8) when cautious

Respond ONLY with this JSON (no other text):
{
  "decision": "CONFIRM" or "ADJUST" or "OVERRIDE",
  "final_action": "BUY" or "SELL" or "HOLD",
  "size_multiplier": 0.0 to 1.5,
  "reasoning": "1-2 sentence risk assessment",
  "risk_flags": ["flag1", "flag2"],
  "portfolio_health": "HEALTHY" or "CAUTION" or "DANGER"
}"""

STRATEGIST_PROMPT = """You are HYDRA's Strategic Advisor, a senior portfolio strategist called in when the trading pipeline has a disagreement. The Market Analyst and Risk Manager could not reach a clear consensus, so you are the final decision-maker.

You receive:
- The engine's quantitative signal (rule-based)
- The Market Analyst's thesis (AI analysis)
- The Risk Manager's assessment (AI risk evaluation)

Your job: Make the final call. You have the deepest reasoning ability and should consider:
1. Whether the disagreement is meaningful or noise
2. The broader market context from the price action
3. Whether the risk concerns outweigh the opportunity
4. Position sizing — if the opportunity is real but risky, reduce size rather than reject

Think step by step. Then respond ONLY with this JSON:
{
  "final_action": "BUY" or "SELL" or "HOLD",
  "conviction": 0.0 to 1.0,
  "size_multiplier": 0.0 to 1.5,
  "reasoning": "2-4 sentence strategic analysis explaining your final decision",
  "decision": "CONFIRM" or "ADJUST" or "OVERRIDE"
}"""


# ═══════════════════════════════════════════════════════════════
# HYDRA BRAIN
# ═══════════════════════════════════════════════════════════════

# Cost per million tokens: (input, output)
COST_ANTHROPIC = (3.0, 15.0)
COST_XAI = (2.0, 10.0)


class HydraBrain:
    """3-agent AI reasoning: Claude Analyst + Claude Risk Manager + Grok Strategist.
    Grok only fires on contested decisions (ADJUST/OVERRIDE or low conviction)."""

    STRATEGIST_THRESHOLD = 0.65  # escalate if conviction below this

    def __init__(
        self,
        anthropic_key: str = "",
        xai_key: str = "",
        max_daily_cost: float = 10.0,
        call_interval: int = 1,
        strategist_threshold: float = 0.65,
    ):
        # Primary: Claude for Analyst + Risk Manager
        self.primary_client = None
        self.primary_provider = None
        self.primary_model = None

        if anthropic_key and HAS_ANTHROPIC:
            self.primary_client = anthropic.Anthropic(api_key=anthropic_key)
            self.primary_provider = "anthropic"
            self.primary_model = "claude-sonnet-4-6"
        elif xai_key and HAS_OPENAI:
            # Fallback: use xAI for primary if no Anthropic key
            self.primary_client = openai.OpenAI(api_key=xai_key, base_url="https://api.x.ai/v1")
            self.primary_provider = "xai"
            self.primary_model = "grok-4.20-0309-reasoning"

        # Strategist: Grok for deep reasoning on contested decisions
        self.strategist_client = None
        self.strategist_model = "grok-4.20-0309-reasoning"
        self.has_strategist = False
        if xai_key and HAS_OPENAI:
            self.strategist_client = openai.OpenAI(api_key=xai_key, base_url="https://api.x.ai/v1")
            self.has_strategist = True

        if not self.primary_client:
            raise ValueError("No AI provider available — need ANTHROPIC_API_KEY or XAI_API_KEY")

        self.model = self.primary_model  # for get_stats display
        self.provider = self.primary_provider
        self.max_daily_cost = max_daily_cost
        self.call_interval = call_interval
        self.strategist_threshold = strategist_threshold

        # Cost tracking
        self.INPUT_COST_PER_M = COST_ANTHROPIC[0] if self.primary_provider == "anthropic" else COST_XAI[0]
        self.OUTPUT_COST_PER_M = COST_ANTHROPIC[1] if self.primary_provider == "anthropic" else COST_XAI[1]

        # Agentic state — cross-tick memory, goals, multi-step plans
        self.memory: BrainMemory = BrainMemory()

        # State
        self.decision_history: Dict[str, List[Dict]] = {}
        self.daily_tokens_in = 0         # Primary provider (analyst + risk)
        self.daily_tokens_out = 0
        self._daily_strategist_tokens_in = 0   # Strategist (xAI) — tracked separately for accurate costing
        self._daily_strategist_tokens_out = 0
        self.daily_decisions = 0
        self.daily_overrides = 0
        self.daily_escalations = 0
        self.daily_reset_date = datetime.now(timezone.utc).date()
        self.tick_counter = 0
        self.consecutive_failures = 0
        self.api_available = True
        self.retry_at_tick = 0
        self.last_decision: Optional[BrainDecision] = None
        self._lock = threading.Lock()  # Thread safety for parallel brain calls

    # ─── Agentic loop ───

    @staticmethod
    def _state_digest(state: Dict[str, Any]) -> List[float]:
        """Small dense vector used for similarity retrieval over episodes.

        Keeps the scale roughly in [-3, 3] per component so cosine similarity
        is meaningful. Pure Python, 7 dimensions.
        """
        ind = state.get("indicators") or {}
        trend = state.get("trend") or {}
        vol = state.get("volatility") or {}
        sig = state.get("signal") or {}
        port = state.get("portfolio") or {}
        rsi = float(ind.get("rsi", 50.0) or 50.0)
        macd_hist = float(ind.get("macd_histogram", 0.0) or 0.0)
        bb_width = float(ind.get("bb_width", 0.0) or 0.0)
        atr_pct = float(vol.get("atr_pct", 0.0) or 0.0)
        ema20 = float(trend.get("ema20", 0.0) or 0.0)
        ema50 = float(trend.get("ema50", 0.0) or 0.0)
        ratio = (ema20 / ema50 - 1.0) if ema50 > 0 else 0.0
        conf = float(sig.get("confidence", 0.0) or 0.0)
        pnl_pct = float(port.get("pnl_pct", 0.0) or 0.0) / 100.0
        return [
            (rsi - 50.0) / 50.0,   # centered ∈ [-1, 1]
            max(-3.0, min(3.0, macd_hist)),
            bb_width,
            atr_pct / 10.0,
            ratio * 100.0,
            conf,
            max(-1.0, min(1.0, pnl_pct)),
        ]

    def _plan_condition_matches(self, cond: Dict[str, Any], state: Dict[str, Any]) -> bool:
        """Tiny DSL for plan-step conditions. Supported keys:
        - regime: string → must equal state['regime']
        - min_confidence, max_confidence: threshold on signal.confidence
        - min_rsi, max_rsi: threshold on indicators.rsi
        - min_drawdown_pct, max_drawdown_pct: threshold on portfolio.max_drawdown_pct
        """
        if not cond:
            return True
        sig = state.get("signal") or {}
        ind = state.get("indicators") or {}
        port = state.get("portfolio") or {}
        conf = float(sig.get("confidence", 0.0) or 0.0)
        rsi = float(ind.get("rsi", 50.0) or 50.0)
        dd = float(port.get("max_drawdown_pct", 0.0) or 0.0)
        if "regime" in cond and state.get("regime") != cond["regime"]:
            return False
        if "min_confidence" in cond and conf < float(cond["min_confidence"]):
            return False
        if "max_confidence" in cond and conf > float(cond["max_confidence"]):
            return False
        if "min_rsi" in cond and rsi < float(cond["min_rsi"]):
            return False
        if "max_rsi" in cond and rsi > float(cond["max_rsi"]):
            return False
        if "min_drawdown_pct" in cond and dd < float(cond["min_drawdown_pct"]):
            return False
        if "max_drawdown_pct" in cond and dd > float(cond["max_drawdown_pct"]):
            return False
        return True

    def _update_goal_posture(self, state: Dict[str, Any]):
        """Promote `risk_posture` based on realised drawdown vs budget."""
        port = state.get("portfolio") or {}
        dd = float(port.get("max_drawdown_pct", 0.0) or 0.0)
        budget = self.memory.goals.drawdown_budget_pct
        if dd >= 0.75 * budget:
            self.memory.goals.risk_posture = "conservative"
        elif dd >= 0.4 * budget:
            self.memory.goals.risk_posture = "neutral"
        else:
            # Aggressive only if PnL trajectory is positive
            pnl_pct = float(port.get("pnl_pct", 0.0) or 0.0)
            self.memory.goals.risk_posture = "aggressive" if pnl_pct > 1.0 else "neutral"

    def _decision_from_plan_step(self, pair: str, state: Dict[str, Any], step: PlanStep) -> Optional[BrainDecision]:
        """Build a BrainDecision from a plan step whose condition matched."""
        then = step.then_action or {}
        final_action = then.get("final_action", state.get("signal", {}).get("action", "HOLD"))
        confidence = float(then.get("confidence", state.get("signal", {}).get("confidence", 0.0)))
        size_multiplier = float(then.get("size_multiplier", 1.0))
        # Clamp size multiplier per user-locked decision [0.0, 1.25]
        size_multiplier = max(0.0, min(1.25, size_multiplier))
        posture = self.memory.goals.risk_posture
        if posture == "conservative":
            size_multiplier = min(size_multiplier, 0.5)
        step.status = "fired"
        return BrainDecision(
            action="PLAN_STEP",
            final_signal=final_action,
            confidence_adj=confidence,
            size_multiplier=size_multiplier,
            analyst_reasoning=f"Plan step {step.status}: {then.get('reason', '')}",
            risk_reasoning=f"Posture={posture}, drawdown budget guarded",
            combined_summary=f"[PLAN] {final_action} per step {step.to_dict()}",
            portfolio_health="HEALTHY" if posture != "conservative" else "CAUTION",
            fallback=False,
        )

    def step(self, state: Dict[str, Any]) -> BrainDecision:
        """Genuine agentic decision loop with memory + goals + planning.

        Flow per tick:
          1. Update risk posture from drawdown vs goal budget.
          2. Build state digest, retrieve similar past episodes.
          3. If a current plan has a step whose `if_condition` matches now,
             emit the step's `then_action` and advance the plan without an
             API call.
          4. Otherwise delegate to the 3-agent Claude/Grok pipeline
             (`_deliberate_pipeline`). On CONFIRM with strong conviction,
             optionally register a short forward plan from the decision.
          5. Record an Episode in memory (closed=False — outcome back-filled
             later by `reflect`).
          6. Enforce the user-locked size_multiplier clamp [0.0, 1.25] and
             the `risk_posture=conservative` guardrail before returning.

        The old `deliberate(state)` entrypoint delegates here for backward
        compatibility — see below.
        """
        pair = state.get("asset", "UNKNOWN")
        tick = int(state.get("tick", 0))

        # 1. Goal posture update
        self._update_goal_posture(state)

        # 2. Digest + retrieval (retrieval results are fed into the prompt
        # implicitly via the decision_history field; richer RAG is future work).
        digest = self._state_digest(state)

        # 3. Plan consultation
        plan = self.memory.get_plan(pair)
        if plan is not None:
            if plan.is_expired(tick):
                self.memory.clear_plan(pair)
                plan = None
            else:
                step_obj = plan.current_step()
                if step_obj is not None and step_obj.status == "pending" \
                        and self._plan_condition_matches(step_obj.if_condition, state):
                    decision = self._decision_from_plan_step(pair, state, step_obj)
                    plan.step_idx += 1
                    if plan.step_idx >= len(plan.steps):
                        self.memory.clear_plan(pair)
                    # Record as episode (digest stored for later retrieval)
                    self.memory.add_episode(Episode(
                        tick=tick, timestamp=time.time(), pair=pair,
                        state_digest=digest,
                        action=decision.action,
                        signal=decision.final_signal,
                        confidence=decision.confidence_adj,
                    ))
                    return decision

        # 4. Delegate to the 3-agent pipeline for genuine reasoning
        decision = self._deliberate_pipeline(state)

        # 4a. Enforce size_multiplier clamp per locked decision
        decision.size_multiplier = max(0.0, min(1.25, decision.size_multiplier))
        if self.memory.goals.risk_posture == "conservative":
            decision.size_multiplier = min(decision.size_multiplier, 0.5)

        # 4b. Optionally register a short plan if the pipeline returned a
        # strong directional view — exposes the brain as genuinely forward-
        # looking rather than tick-local.
        if (not decision.fallback
                and decision.final_signal in ("BUY", "SELL")
                and decision.confidence_adj >= 0.7
                and self.memory.get_plan(pair) is None):
            self._register_followup_plan(pair, tick, state, decision)

        # 5. Record episode (outcome back-filled on close)
        self.memory.add_episode(Episode(
            tick=tick, timestamp=time.time(), pair=pair,
            state_digest=digest,
            action=decision.action,
            signal=decision.final_signal,
            confidence=decision.confidence_adj,
        ))

        return decision

    def _register_followup_plan(self, pair: str, tick: int, state: Dict[str, Any], decision: BrainDecision):
        """Create a small 2-step follow-up plan after a high-conviction signal.

        Step 1: If drawdown starts to bite within N ticks, de-risk.
        Step 2: If the move plays out (opposite-direction RSI regime), take profit.
        """
        opposite = "SELL" if decision.final_signal == "BUY" else "BUY"
        steps: List[PlanStep] = [
            PlanStep(
                if_condition={"min_drawdown_pct": 3.0},
                then_action={
                    "final_action": "HOLD",
                    "confidence": 0.4,
                    "size_multiplier": 0.3,
                    "reason": "Plan guard: drawdown >3% after high-conviction entry — de-risk",
                },
            ),
            PlanStep(
                if_condition=(
                    {"min_rsi": 70.0} if decision.final_signal == "BUY" else {"max_rsi": 30.0}
                ),
                then_action={
                    "final_action": opposite,
                    "confidence": 0.7,
                    "size_multiplier": 0.8,
                    "reason": "Plan target: take profit on overbought/oversold mirror",
                },
            ),
        ]
        self.memory.set_plan(pair, BrainPlan(
            goal=f"Ride {decision.final_signal} move initiated at tick {tick}",
            horizon_ticks=20,
            created_at_tick=tick,
            steps=steps,
        ))

    def reflect(self, pair: str, closed_trade: Dict[str, Any]):
        """Back-fill realised outcome into the last matching open episode and
        update named beliefs. Call this from the agent loop whenever a
        position closes."""
        dq = self.memory.episodes.get(pair)
        if not dq:
            return
        profit = float(closed_trade.get("profit") or 0.0)
        # Back-fill the most recent open (non-closed) episode
        for ep in reversed(dq):
            if not ep.closed:
                ep.realized_pnl = profit
                ep.closed = True
                # Counterfactual regret: if we held instead, what would have happened?
                # For now we use -profit as a crude proxy (losing ⇒ regret is positive).
                ep.regret = -profit if ep.signal != "HOLD" else 0.0
                break
        # Belief update — simple EMA of normalised profit
        normalised = max(-1.0, min(1.0, profit / 10.0))
        self.memory.update_belief(f"{pair}_trade_edge", normalised)

    # ─── Main Entry Point (backward-compatible wrapper) ───

    def deliberate(self, state: Dict[str, Any]) -> BrainDecision:
        """Backward-compatible entry. Delegates to the agentic `step()` loop."""
        return self.step(state)

    def _deliberate_pipeline(self, state: Dict[str, Any]) -> BrainDecision:
        """Evaluate engine signal with 3-agent pipeline. Thread-safe."""
        # Pre-flight: shared state checks under lock
        with self._lock:
            self.tick_counter += 1
            self._maybe_reset_daily()

            if self.call_interval > 1 and self.tick_counter % self.call_interval != 0:
                return self._fallback(state, reason="Non-AI tick")

            if not self.api_available:
                if self.tick_counter >= self.retry_at_tick:
                    self.api_available = True
                else:
                    return self._fallback(state)

            if self._estimated_cost() >= self.max_daily_cost:
                return self._fallback(state, reason="Daily budget exceeded")

        # API calls run OUTSIDE lock (I/O bound, each creates independent HTTP request)
        start = time.time()
        total_tokens_in = 0
        total_tokens_out = 0

        try:
            # Agent 1: Market Analyst (Claude)
            analyst_output, a_in, a_out = self._run_analyst(state)
            total_tokens_in += a_in
            total_tokens_out += a_out
            if analyst_output is None:
                raise ValueError("Analyst returned no output")

            # Agent 2: Risk Manager (Claude)
            risk_output, r_in, r_out = self._run_risk_manager(state, analyst_output)
            total_tokens_in += r_in
            total_tokens_out += r_out
            if risk_output is None:
                raise ValueError("Risk Manager returned no output")

            # Agent 3: Strategic Advisor (Grok) — only on contested decisions
            strategist_output = None
            escalated = False
            needs_strategist = (
                self.has_strategist and (
                    risk_output.get("decision") != "CONFIRM" or
                    analyst_output.get("conviction", 1.0) < self.strategist_threshold
                )
            )

            strategist_tokens_in = 0
            strategist_tokens_out = 0
            if needs_strategist:
                try:
                    strategist_output, s_in, s_out = self._run_strategist(state, analyst_output, risk_output)
                    strategist_tokens_in = s_in
                    strategist_tokens_out = s_out
                    escalated = True
                except Exception as e:
                    print(f"  [BRAIN] Strategist failed (continuing with Risk Manager decision): {e}")

            # Build final decision — strategist overrides risk manager when present
            if strategist_output:
                final_action = strategist_output.get("final_action", risk_output.get("final_action", state["signal"]["action"]))
                final_decision = strategist_output.get("decision", risk_output.get("decision", "CONFIRM"))
                final_conviction = strategist_output.get("conviction", analyst_output.get("conviction", state["signal"]["confidence"]))
                final_size = strategist_output.get("size_multiplier", risk_output.get("size_multiplier", 1.0))
                strategist_reasoning = strategist_output.get("reasoning", "")
            else:
                final_action = risk_output.get("final_action", state["signal"]["action"])
                final_decision = risk_output.get("decision", "CONFIRM")
                final_conviction = analyst_output.get("conviction", state["signal"]["confidence"])
                final_size = risk_output.get("size_multiplier", 1.0)
                strategist_reasoning = ""

            all_tokens = total_tokens_in + total_tokens_out + strategist_tokens_in + strategist_tokens_out
            decision = BrainDecision(
                action=final_decision,
                final_signal=final_action,
                confidence_adj=final_conviction,
                size_multiplier=final_size,
                analyst_reasoning=analyst_output.get("thesis", ""),
                risk_reasoning=risk_output.get("reasoning", ""),
                strategist_reasoning=strategist_reasoning,
                combined_summary=self._build_summary(analyst_output, risk_output, strategist_output),
                risk_flags=risk_output.get("risk_flags", []),
                portfolio_health=risk_output.get("portfolio_health", "HEALTHY"),
                tokens_used=all_tokens,
                latency_ms=(time.time() - start) * 1000,
                fallback=False,
                escalated=escalated,
            )

            # Bookkeeping: shared state writes under lock
            with self._lock:
                self.daily_tokens_in += total_tokens_in
                self.daily_tokens_out += total_tokens_out
                self._daily_strategist_tokens_in += strategist_tokens_in
                self._daily_strategist_tokens_out += strategist_tokens_out
                self.daily_decisions += 1
                if escalated:
                    self.daily_escalations += 1
                if decision.action == "OVERRIDE":
                    self.daily_overrides += 1
                self.consecutive_failures = 0
                self.last_decision = decision

                asset = state.get("asset", "UNKNOWN")
                if asset not in self.decision_history:
                    self.decision_history[asset] = []
                self.decision_history[asset].append({
                    "tick": state.get("tick", 0),
                    "action": decision.action,
                    "signal": decision.final_signal,
                    "conviction": decision.confidence_adj,
                    "escalated": escalated,
                })
                if len(self.decision_history[asset]) > 20:
                    self.decision_history[asset] = self.decision_history[asset][-20:]

            return decision

        except Exception as e:
            with self._lock:
                self.consecutive_failures += 1
                if self.consecutive_failures >= 3:
                    self.api_available = False
                    self.retry_at_tick = self.tick_counter + 60
                    print(f"  [BRAIN] API disabled after {self.consecutive_failures} failures. Retry at tick {self.retry_at_tick}")
            return self._fallback(state, reason=str(e))

    # ─── LLM Calls ───

    def _call_llm(self, system_prompt: str, user_msg: str, max_tokens: int = 300,
                  client=None, provider=None, model=None) -> tuple:
        """Call an LLM provider. Returns (text, input_tokens, output_tokens)."""
        client = client or self.primary_client
        provider = provider or self.primary_provider
        model = model or self.primary_model

        if provider == "xai":
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                timeout=30.0,
            )
            text = response.choices[0].message.content if response.choices else ""
            usage = response.usage
            return text, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0
        else:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                timeout=10.0,
            )
            text = response.content[0].text if response.content and hasattr(response.content[0], "text") else ""
            return text, response.usage.input_tokens, response.usage.output_tokens

    def _run_analyst(self, state: Dict) -> tuple:
        """Market Analyst (Claude). Returns (parsed_output, in_tokens, out_tokens)."""
        user_msg = self._build_analyst_prompt(state)
        text, tok_in, tok_out = self._call_llm(ANALYST_PROMPT, user_msg, 300)
        return self._parse_json(text), tok_in, tok_out

    def _run_risk_manager(self, state: Dict, analyst: Dict) -> tuple:
        """Risk Manager (Claude). Returns (parsed_output, in_tokens, out_tokens)."""
        user_msg = self._build_risk_prompt(state, analyst)
        text, tok_in, tok_out = self._call_llm(RISK_MANAGER_PROMPT, user_msg, 250)
        return self._parse_json(text), tok_in, tok_out

    def _run_strategist(self, state: Dict, analyst: Dict, risk: Dict) -> tuple:
        """Strategic Advisor (Grok 4 Reasoning). Only called on contested decisions."""
        user_msg = self._build_strategist_prompt(state, analyst, risk)
        text, tok_in, tok_out = self._call_llm(
            STRATEGIST_PROMPT, user_msg, 500,
            client=self.strategist_client, provider="xai", model=self.strategist_model,
        )
        return self._parse_json(text), tok_in, tok_out

    # ─── Prompt Builders ───

    @staticmethod
    def _format_recent_closes(candles: List[Dict]) -> str:
        """Format last 10 candle closes for prompt inclusion."""
        if not candles:
            return ""
        return ", ".join(f"{c['c']:.4f}" for c in candles[-10:])

    @staticmethod
    def _format_spread(state: Dict) -> str:
        """Format bid/ask spread for prompt inclusion."""
        spread = state.get("spread", {})
        if not spread:
            return ""
        return f"\nSPREAD: bid={spread['bid']} | ask={spread['ask']} | spread={spread['spread_bps']} bps"

    def _format_triangle_context(self, state: Dict) -> str:
        """Format cross-pair triangle context for prompt inclusion."""
        triangle = state.get("triangle_context", {})
        sibling_pairs = triangle.get("pairs", {})
        net_exp = triangle.get("net_exposure", {})
        if not sibling_pairs:
            return ""
        parts = [f"  {p}: regime={d['regime']}, signal={d['signal']}({d['confidence']:.2f}), pos={d['position_size']:.4f}"
                 for p, d in sibling_pairs.items()]
        lines = "\nCROSS-PAIR CONTEXT:\n" + "\n".join(parts)
        lines += f"\n  Net SOL exposure: {net_exp.get('SOL', 0):.4f} | Net XBT exposure: {net_exp.get('XBT', 0):.4f}"
        return lines

    def _build_analyst_prompt(self, state: Dict) -> str:
        sig = state.get("signal", {})
        ind = state.get("indicators", {})
        pos = state.get("position", {})
        port = state.get("portfolio", {})
        trend = state.get("trend", {})
        volatility = state.get("volatility", {})
        vol = state.get("volume", {})
        candles = state.get("candles", [])
        recent_closes = self._format_recent_closes(candles)
        asset = state.get("asset", "UNKNOWN")
        pair_history = self.decision_history.get(asset, [])
        recent = ""
        if pair_history:
            recent = " | ".join(
                f"{d['action']} {d['signal']} ({d['conviction']:.0%})"
                for d in pair_history[-5:]
            )

        return f"""PAIR: {state.get('asset', '?')} | PRICE: {state.get('price', 0)} | REGIME: {state.get('regime', '?')} | STRATEGY: {state.get('strategy', '?')} | TIMEFRAME: {state.get('candle_interval', '?')}m | CANDLE: {state.get('candle_status', 'unknown')}
ENGINE SIGNAL: {sig.get('action', '?')} @ {sig.get('confidence', 0):.2f} confidence
REASON: {sig.get('reason', '')}

INDICATORS: RSI={ind.get('rsi', '?')} | MACD=[line={ind.get('macd_line', '?')}, signal={ind.get('macd_signal', '?')}, hist={ind.get('macd_histogram', '?')}] | BB=[{ind.get('bb_lower', '?')}, {ind.get('bb_middle', '?')}, {ind.get('bb_upper', '?')}] | BB_WIDTH={ind.get('bb_width', 0):.4f}
TREND: EMA20={trend.get('ema20', '?')} | EMA50={trend.get('ema50', '?')} | ATR={volatility.get('atr', '?')} ({volatility.get('atr_pct', '?')}%)
VOLUME: current={vol.get('current', '?')} | avg_20={vol.get('avg_20', '?')}

RECENT CLOSES: {recent_closes}

POSITION: {pos.get('size', 0):.6f} @ avg {pos.get('avg_entry', 0)} | Unrealized: {pos.get('unrealized_pnl', 0)}
PORTFOLIO: Balance=${port.get('balance', 0):.2f} | Equity=${port.get('equity', 0):.2f} | P&L={port.get('pnl_pct', 0):.2f}% | Max DD={port.get('max_drawdown_pct', 0):.2f}%
RECENT AI DECISIONS: {recent or 'None yet'}{self._format_spread(state)}{self._format_triangle_context(state)}"""

    def _build_risk_prompt(self, state: Dict, analyst: Dict) -> str:
        sig = state.get("signal", {})
        ind = state.get("indicators", {})
        pos = state.get("position", {})
        port = state.get("portfolio", {})
        perf = state.get("performance", {})
        volatility = state.get("volatility", {})
        vol = state.get("volume", {})

        return f"""PAIR: {state.get('asset', '?')} | PRICE: {state.get('price', 0)} | REGIME: {state.get('regime', '?')} | TIMEFRAME: {state.get('candle_interval', '?')}m | CANDLE: {state.get('candle_status', 'unknown')}
ENGINE SIGNAL: {sig.get('action', '?')} @ {sig.get('confidence', 0):.2f}
ANALYST THESIS: {analyst.get('thesis', 'N/A')}
ANALYST CONVICTION: {analyst.get('conviction', 0):.2f}
ANALYST AGREES WITH ENGINE: {analyst.get('signal_agreement', '?')}
ANALYST CONCERN: {analyst.get('concern', 'None')}

KEY RISK INDICATORS: RSI={ind.get('rsi', '?')} | ATR={volatility.get('atr_pct', '?')}% | BB_WIDTH={ind.get('bb_width', '?')}
VOLUME: current={vol.get('current', '?')} | avg_20={vol.get('avg_20', '?')}

POSITION: {pos.get('size', 0):.6f} @ avg {pos.get('avg_entry', 0)} | Unrealized P&L: {pos.get('unrealized_pnl', 0)}
PORTFOLIO: Balance=${port.get('balance', 0):.2f} | Equity=${port.get('equity', 0):.2f} | Peak=${port.get('peak_equity', 0):.2f} | P&L={port.get('pnl_pct', 0):.2f}% | Max DD={port.get('max_drawdown_pct', 0):.2f}%
PERFORMANCE: {perf.get('total_trades', 0)} trades | Win Rate: {perf.get('win_rate_pct', 0):.0f}% | Sharpe: {perf.get('sharpe_estimate', 0):.2f}{self._format_spread(state)}{self._format_triangle_context(state)}"""

    def _build_strategist_prompt(self, state: Dict, analyst: Dict, risk: Dict) -> str:
        sig = state.get("signal", {})
        ind = state.get("indicators", {})
        pos = state.get("position", {})
        port = state.get("portfolio", {})
        trend = state.get("trend", {})
        volatility = state.get("volatility", {})
        vol = state.get("volume", {})
        candles = state.get("candles", [])
        recent_closes = self._format_recent_closes(candles)

        return f"""CONTESTED DECISION — Your strategic analysis is needed.
PAIR: {state.get('asset', '?')} | TIMEFRAME: {state.get('candle_interval', '?')}m | CANDLE: {state.get('candle_status', 'unknown')}

ENGINE SIGNAL: {sig.get('action', '?')} @ {sig.get('confidence', 0):.2f} confidence
ENGINE REASON: {sig.get('reason', '')}

MARKET ANALYST (Claude):
  Thesis: {analyst.get('thesis', 'N/A')}
  Conviction: {analyst.get('conviction', 0):.2f}
  Agrees with engine: {analyst.get('signal_agreement', '?')}
  Concern: {analyst.get('concern', 'None')}
  Key factors: {', '.join(analyst.get('key_factors', []))}

RISK MANAGER (Claude):
  Decision: {risk.get('decision', '?')}
  Final action: {risk.get('final_action', '?')}
  Size multiplier: {risk.get('size_multiplier', '?')}
  Reasoning: {risk.get('reasoning', 'N/A')}
  Risk flags: {', '.join(risk.get('risk_flags', []))}
  Portfolio health: {risk.get('portfolio_health', '?')}

INDICATORS: RSI={ind.get('rsi', '?')} | MACD=[line={ind.get('macd_line', '?')}, signal={ind.get('macd_signal', '?')}, hist={ind.get('macd_histogram', '?')}] | BB=[{ind.get('bb_lower', '?')}, {ind.get('bb_middle', '?')}, {ind.get('bb_upper', '?')}]
TREND: EMA20={trend.get('ema20', '?')} | EMA50={trend.get('ema50', '?')} | ATR={volatility.get('atr', '?')} ({volatility.get('atr_pct', '?')}%)
VOLUME: current={vol.get('current', '?')} | avg_20={vol.get('avg_20', '?')}
RECENT CLOSES: {recent_closes}
POSITION: {pos.get('size', 0):.6f} @ avg {pos.get('avg_entry', 0)} | Unrealized: {pos.get('unrealized_pnl', 0)}
PORTFOLIO: Equity=${port.get('equity', 0):.2f} | P&L={port.get('pnl_pct', 0):.2f}% | Max DD={port.get('max_drawdown_pct', 0):.2f}%{self._format_spread(state)}{self._format_triangle_context(state)}

Make the final call. Think carefully, then respond with JSON only."""

    # ─── Helpers ───

    def _parse_json(self, text: str) -> Optional[Dict]:
        """Lenient JSON parser — finds JSON in response text."""
        if not text:
            return None
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"  [BRAIN] Failed to parse JSON: {text[:100]}")
        return None

    def _build_summary(self, analyst: Dict, risk: Dict, strategist: Optional[Dict] = None) -> str:
        """One-line combined summary for trade log."""
        if strategist:
            decision = strategist.get("decision", "CONFIRM")
            action = strategist.get("final_action", "HOLD")
            reasoning = strategist.get("reasoning", "")
            prefix = "[GROK]"
        else:
            decision = risk.get("decision", "CONFIRM")
            action = risk.get("final_action", "HOLD")
            reasoning = analyst.get("thesis", "")
            prefix = ""
        first_sentence = reasoning.split(".")[0] + "." if "." in reasoning else reasoning
        if len(first_sentence) > 70:
            first_sentence = first_sentence[:67] + "..."
        return f"{prefix} {decision} {action}: {first_sentence}".strip()

    def _fallback(self, state: Dict, reason: str = "") -> BrainDecision:
        """Return engine signal unchanged when AI is unavailable."""
        sig = state.get("signal", {})
        return BrainDecision(
            action="CONFIRM",
            final_signal=sig.get("action", "HOLD"),
            confidence_adj=sig.get("confidence", 0),
            size_multiplier=1.0,
            analyst_reasoning=f"Engine-only (AI unavailable: {reason})" if reason else "Engine-only mode",
            risk_reasoning="Passthrough — no AI risk assessment",
            combined_summary=f"ENGINE ONLY: {sig.get('reason', '')}",
            fallback=True,
        )

    def _estimated_cost(self) -> float:
        """Estimate daily API cost from token usage (primary + strategist)."""
        primary_cost = (self.daily_tokens_in / 1_000_000 * self.INPUT_COST_PER_M +
                        self.daily_tokens_out / 1_000_000 * self.OUTPUT_COST_PER_M)
        strategist_cost = (self._daily_strategist_tokens_in / 1_000_000 * COST_XAI[0] +
                           self._daily_strategist_tokens_out / 1_000_000 * COST_XAI[1])
        return primary_cost + strategist_cost

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).date()
        if today != self.daily_reset_date:
            self.daily_tokens_in = 0
            self.daily_tokens_out = 0
            self._daily_strategist_tokens_in = 0
            self._daily_strategist_tokens_out = 0
            self.daily_decisions = 0
            self.daily_overrides = 0
            self.daily_escalations = 0
            self.daily_reset_date = today

    def get_stats(self) -> Dict:
        """Return brain statistics for dashboard."""
        return {
            "active": self.api_available,
            "provider": self.primary_provider,
            "decisions_today": self.daily_decisions,
            "overrides_today": self.daily_overrides,
            "escalations_today": self.daily_escalations,
            "has_strategist": self.has_strategist,
            "cost_today": round(self._estimated_cost(), 4),
            "max_daily_cost": self.max_daily_cost,
            "tokens_today": (self.daily_tokens_in + self.daily_tokens_out +
                            self._daily_strategist_tokens_in + self._daily_strategist_tokens_out),
            "avg_latency_ms": round(
                self.last_decision.latency_ms if self.last_decision and not self.last_decision.fallback else 0, 0
            ),
            "model": self.primary_model,
            "strategist_model": self.strategist_model if self.has_strategist else None,
            "consecutive_failures": self.consecutive_failures,
        }
