"""
hydra_research.py — Agentic Brain Layer (Candidate for Production Integration)

Status: FUNCTIONAL — unit-tested, not yet wired into production agent loop.

This module was developed on branch claude/evaluate-agentic-design-DgCMU and
deliberately excluded from main because the integration cost was higher than
the immediate benefit. The code is correct and all tests pass.

== What this is ==

An upgrade to HydraBrain that adds persistent cross-tick memory, goal-driven
risk posture, and multi-step conditional planning. Instead of treating each
tick as stateless, the brain:

  1. Maintains a GoalState (drawdown budget → risk posture: conservative /
     neutral / aggressive) that adjusts position-size caps automatically.

  2. Commits to BrainPlan objects that fire conditional actions across ticks
     without re-consulting the LLM — e.g., "if RSI > 70 within 20 ticks after
     this BUY, take profit at 0.8× size."

  3. Records Episodes (state digest + action + realized PnL) for future offline
     analysis and backtesting. reflect() back-fills PnL when a trade closes.

== What was removed ==

Two earlier experimental classes were deleted from this file after review:

  - RegimeSwitchingFilter (Hamilton 1989 Bayesian filter): correct
    implementation, but the production regime detector (EMA crossover + ATR
    + BB) already works for 3 pairs. Hidden Markov models are designed for
    latent-variable data (economic cycles); crypto regime is directly observed
    from price. Removed: no production benefit.

  - JointSignalSolver (QAOA-inspired Ising cost Hamiltonian): correct
    mathematics for N-spin portfolio optimisation. For N=3 pairs the 2^3
    enumeration is fast, but the covariance coupling (γ·sᵀΣs term) is
    near-zero for SOL/USDC + SOL/XBT + XBT/USDC because they share the same
    underlying assets. In practice the solver produced an empty override dict
    on most ticks. Removed: no effect observed.

== Integration checklist (when ready to wire into production) ==

  1. Add BrainMemory to HydraBrain.__init__:
       self.memory = BrainMemory()

  2. In hydra_agent.py _apply_brain(), replace:
       decision = self.brain.deliberate(state)
     with:
       decision = self.brain.step(state)

  3. In hydra_agent.py, after position fully closes, call:
       self.brain.reflect(pair, {"profit": trade.realized_pnl})

  4. In _save_snapshot() / _load_snapshot(), add brain memory serialization:
       snapshot["brain_memory"] = self.brain.memory.to_dict()
       self.brain.memory = BrainMemory.from_dict(snapshot.get("brain_memory", {}))

  5. Validate on 5 days of live candle data:
       - What % of ticks fire a plan step vs. go to the LLM pipeline?
       - Does plan-driven sizing reduce drawdown vs. baseline?
       - Are 1-minute candle horizons too short for 20-tick plans?

== Tests ==
  tests/test_brain_agent_research.py — 15 unit tests, all passing, no API keys.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Deque, Dict, List, Optional

# ═══════════════════════════════════════════════════════════════
# PART 3 — AGENTIC BRAIN LAYER (dataclasses)
# ═══════════════════════════════════════════════════════════════
#
# FUTURE_RESEARCH pointer: See hydra_brain.py STRATEGIST_THRESHOLD annotation.
#
# Design: replaces the stateless 3-agent deliberation with a persistent memory
# layer. The brain maintains goals, multi-tick plans, and an episodic log.
# GoalState → risk posture (conservative/neutral/aggressive) driven by drawdown.
# BrainPlan → multi-step conditional plan committed across ticks.
# Episode   → decision + outcome record for offline analysis / backtesting.

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
    """One decision + (eventually) its realised outcome.

    Recorded every tick as telemetry for session snapshot / offline analysis.
    Not consulted during live deliberation — kept for `--resume` continuity
    and future offline backtests.
    """
    tick: int
    timestamp: float
    pair: str
    state_digest: List[float]
    action: str
    signal: str
    confidence: float
    realized_pnl: Optional[float] = None
    closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BrainMemory:
    """Cross-tick episodic trace + current plan per pair.

    Pure-Python so it JSON-serialises into the session snapshot without
    any dependency. `episodes[pair]` is a bounded deque of past decisions
    (telemetry), `plans[pair]` holds the currently-active multi-step plan,
    and `goals` is shared across all pairs.
    """

    MAX_EPISODES = 200
    DIGEST_DIM = 7

    def __init__(self, goals: Optional[GoalState] = None):
        self.episodes: Dict[str, Deque[Episode]] = {}
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
# PART 4 — HYDRA BRAIN AGENTIC METHODS
# ═══════════════════════════════════════════════════════════════
#
# These methods extend HydraBrain with:
#   step()   — active-plan consult → 3-agent pipeline → episode record
#   reflect() — back-fill realized PnL into last matching open episode
#   _register_followup_plan() — create a 2-step guard plan after high-conviction signal
#
# To wire into production:
#   1. Add BrainMemory to HydraBrain.__init__
#   2. Replace deliberate() calls in hydra_agent.py with step()
#   3. Call brain.reflect(pair, trade_dict) on position close

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

    # User-locked size-multiplier bounds. Conservative posture further caps
    # at CONSERVATIVE_SIZE_CAP to de-risk when drawdown approaches budget.
    _SIZE_MULTIPLIER_CAP = 1.25
    _CONSERVATIVE_SIZE_CAP = 0.5

    def _apply_size_caps(self, size_mult: float) -> float:
        """Clamp size_multiplier to [0, 1.25] and enforce the conservative cap."""
        size_mult = max(0.0, min(self._SIZE_MULTIPLIER_CAP, size_mult))
        if self.memory.goals.risk_posture == "conservative":
            size_mult = min(size_mult, self._CONSERVATIVE_SIZE_CAP)
        return size_mult

    def _decision_from_plan_step(self, pair: str, state: Dict[str, Any], step: PlanStep) -> Optional[BrainDecision]:
        """Build a BrainDecision from a plan step whose condition matched."""
        then = step.then_action or {}
        final_action = then.get("final_action", state.get("signal", {}).get("action", "HOLD"))
        confidence = float(then.get("confidence", state.get("signal", {}).get("confidence", 0.0)))
        size_multiplier = self._apply_size_caps(float(then.get("size_multiplier", 1.0)))
        posture = self.memory.goals.risk_posture
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
        """Decision loop: active-plan consult → 3-agent pipeline → record.

        If an active plan's current step matches the live state, emit the
        step's `then_action` and skip the LLM pipeline. Otherwise delegate to
        `_deliberate_pipeline`, and on a high-conviction BUY/SELL result
        register a short forward plan for future tick matching. Every call
        records an Episode (closed=False, back-filled by `reflect`) and
        applies the size_multiplier caps.
        """
        pair = state.get("asset", "UNKNOWN")
        tick = int(state.get("tick", 0))

        # 1. Goal posture update
        self._update_goal_posture(state)

        # 2. Telemetry digest stored on each recorded episode
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
        decision.size_multiplier = self._apply_size_caps(decision.size_multiplier)

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
        """Back-fill realised outcome into the last matching open episode.
        Call this from the agent loop whenever a position closes."""
        dq = self.memory.episodes.get(pair)
        if not dq:
            return
        profit = float(closed_trade.get("profit") or 0.0)
        for ep in reversed(dq):
            if not ep.closed:
                ep.realized_pnl = profit
                ep.closed = True
                break

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
    # NOTE: _call_llm, _parse_brain_response, and provider-specific methods are
    # identical to the production hydra_brain.py implementation. Refer to
    # origin/claude/evaluate-agentic-design-DgCMU for the complete source.
    # The unique additions above (step, reflect, plan loop, agentic dataclasses)
    # are the research-specific contributions.

