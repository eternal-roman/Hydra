"""
hydra_research.py — Experimental Research Archive

This file preserves experimental features from research branches that were
deliberately NOT promoted to main. All code here is functional (tests pass on
its origin branch) but adds complexity beyond what the production system needs.

Sources:
  - branch: claude/evaluate-agentic-design-DgCMU  (Hamilton filter, QAOA solver,
    agentic brain layer)
  - branch: claude/trading-system-audit-R5u83     (captured as FUNCTIONAL_AUDIT.md)

Nothing in this file is imported by the production code.
Use git grep "FUTURE_RESEARCH" to find the production-side annotation for each item.

Contents:
  Part 1 — Hamilton (1989) Bayesian Regime-Switching Filter
  Part 2 — QAOA-inspired Joint-Signal Ising Solver
  Part 3 — Agentic Brain Layer (GoalState, PlanStep, BrainPlan, Episode, BrainMemory)
  Part 4 — HydraBrain Agentic Methods (step, reflect, plan loop)
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Deque, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
# PART 1 — HAMILTON (1989) BAYESIAN REGIME-SWITCHING FILTER
# ═══════════════════════════════════════════════════════════════
#
# FUTURE_RESEARCH pointer: See hydra_engine.py RegimeDetector.detect() for the
# production annotation. This is the full working implementation.
#
# Design: maintains a posterior probability vector over 4 regimes and updates
# it each tick from an observation likelihood built from engine indicators.
# Pure Python — no numpy. Each update is O(16) arithmetic ops.


# ═══════════════════════════════════════════════════════════════
# HAMILTON (1989) REGIME-SWITCHING FILTER
# ═══════════════════════════════════════════════════════════════

class RegimeSwitchingFilter:
    """Hamilton (1989) Bayesian filter over hidden regime states.

    Maintains a posterior probability vector `p_t` over the 4 regimes
    (TREND_UP, TREND_DOWN, RANGING, VOLATILE) and updates it each tick
    from an observation likelihood built from engine indicators.

    Update rule (per tick):
        prior_t   = P^T · p_{t-1}          # transition matrix prediction
        lik_t     = L(obs_t | regime)      # Gaussian likelihood per regime
        p_t       = normalize(lik_t ⊙ prior_t)

    The transition matrix `P` is initialised from a flat prior (slightly
    favouring self-persistence) and can be re-seeded from an empirical
    regime history via `seed_transition_matrix`.

    Pure Python — no numpy dependency. Operates on a fixed 4-dim state
    so every update is O(16) arithmetic ops.
    """

    REGIMES = ("TREND_UP", "TREND_DOWN", "RANGING", "VOLATILE")

    # Feature means per regime (atr_pct, ema_ratio, rsi). Seeded so day-one
    # behaviour roughly matches the hard thresholds in RegimeDetector.
    _FEATURE_MEANS = {
        "TREND_UP":   (1.5, 1.010, 60.0),
        "TREND_DOWN": (1.5, 0.990, 40.0),
        "RANGING":    (1.0, 1.000, 50.0),
        "VOLATILE":   (5.0, 1.000, 50.0),
    }
    # Shared diagonal variances (atr_pct, ema_ratio, rsi).
    _FEATURE_VARS = (2.5, 0.000025, 150.0)

    def __init__(self, persistence: float = 0.85):
        self.persistence = persistence
        self.probs: List[float] = [0.25, 0.25, 0.25, 0.25]
        self.P: List[List[float]] = self._build_transition_matrix(persistence)
        self.observations: int = 0

    @staticmethod
    def _build_transition_matrix(p: float) -> List[List[float]]:
        """Flat prior with self-persistence `p` on the diagonal."""
        n = 4
        off = (1.0 - p) / (n - 1)
        return [[p if i == j else off for j in range(n)] for i in range(n)]

    def seed_transition_matrix(self, regime_history: List[str]):
        """Re-estimate P from an observed regime sequence with Laplace smoothing."""
        if len(regime_history) < 2:
            return
        counts = [[1.0] * 4 for _ in range(4)]  # Laplace-smoothed
        idx = {r: i for i, r in enumerate(self.REGIMES)}
        for a, b in zip(regime_history[:-1], regime_history[1:]):
            if a in idx and b in idx:
                counts[idx[a]][idx[b]] += 1.0
        for i in range(4):
            row_sum = sum(counts[i])
            for j in range(4):
                self.P[i][j] = counts[i][j] / row_sum if row_sum > 0 else 0.25

    @classmethod
    def _observation_likelihood(cls, features: Dict[str, float]) -> List[float]:
        """Diagonal Gaussian likelihood per regime. Returns raw (unnormalised) values."""
        atr_pct = float(features.get("atr_pct", 1.0))
        ema_ratio = float(features.get("ema_ratio", 1.0))
        rsi = float(features.get("rsi", 50.0))
        obs = (atr_pct, ema_ratio, rsi)
        var = cls._FEATURE_VARS
        liks: List[float] = []
        for regime in cls.REGIMES:
            mu = cls._FEATURE_MEANS[regime]
            # log-likelihood for numerical stability, then exp at the end
            log_l = 0.0
            for x, m, v in zip(obs, mu, var):
                log_l += -0.5 * ((x - m) ** 2) / v
            liks.append(math.exp(log_l))
        # Guarantee a positive floor so normalisation never degenerates
        liks = [max(l, 1e-12) for l in liks]
        return liks

    def update(self, features: Dict[str, float]) -> List[float]:
        """Run one filter step. `features` dict needs atr_pct, ema_ratio, rsi."""
        # Predict: prior = P^T · probs
        prior = [0.0] * 4
        for j in range(4):
            s = 0.0
            for i in range(4):
                s += self.P[i][j] * self.probs[i]
            prior[j] = s
        # Observe: likelihood per regime
        lik = self._observation_likelihood(features)
        # Posterior ∝ lik ⊙ prior
        post = [lik[i] * prior[i] for i in range(4)]
        total = sum(post)
        if total <= 0:
            post = [0.25, 0.25, 0.25, 0.25]
        else:
            post = [p / total for p in post]
        self.probs = post
        self.observations += 1
        return post

    def argmax_regime(self) -> str:
        """Return the most probable regime as a string."""
        idx = max(range(4), key=lambda i: self.probs[i])
        return self.REGIMES[idx]

    def probs_dict(self) -> Dict[str, float]:
        return {r: round(self.probs[i], 6) for i, r in enumerate(self.REGIMES)}

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable snapshot of the filter's full state."""
        return {
            "probs": list(self.probs),
            "persistence": self.persistence,
            "observations": self.observations,
            "P": [list(row) for row in self.P],
        }

    def load_dict(self, data: Dict[str, Any]):
        """Restore state from a `to_dict` payload. Re-normalises probs defensively."""
        if not data:
            return
        probs = data.get("probs")
        if isinstance(probs, list) and len(probs) == 4:
            total = sum(probs) or 1.0
            self.probs = [float(p) / total for p in probs]
        self.persistence = float(data.get("persistence", self.persistence))
        self.observations = int(data.get("observations", self.observations))
        P = data.get("P")
        if isinstance(P, list) and len(P) == 4 and all(len(row) == 4 for row in P):
            self.P = [[float(x) for x in row] for row in P]


# ═══════════════════════════════════════════════════════════════
# JOINT-SIGNAL SOLVER (QAOA-inspired Ising cost Hamiltonian)
# ═══════════════════════════════════════════════════════════════

class JointSignalSolver:
    """Cross-pair signal resolver built on an Ising-style cost Hamiltonian.

    Treats the N trading pairs as N spins (long-bias +1, short-bias -1) and
    finds the configuration that minimises

        E(s) = -h · s + γ · sᵀ Σ s

    where `h_i` combines each pair's per-engine signal with its regime-filter
    drift, and `Σ` is the rolling covariance matrix of log-returns. This is
    the classical cost operator used in QAOA/VQE for portfolio optimisation;
    for N=3 pairs the full configuration space is just 2^3 = 8 states, so we
    exact-diagonalise by enumeration in pure Python.

    Outputs per pair:
        - chosen bias (+1 = long / BUY, -1 = short / SELL, 0 = HOLD)
        - derived confidence from the energy gap to the runner-up
        - human-readable reason referencing the covariance and regime drift
    """

    WINDOW = 50                     # candles used for return series
    COVARIANCE_WEIGHT = 0.5         # γ — correlated-exposure penalty
    REGIME_DRIFT_WEIGHT = 0.5       # λ — how strongly regime probs bias h
    GAP_CONFIDENCE_SCALE = 5.0      # how sharply energy gap maps to confidence
    HOLD_GAP_THRESHOLD = 0.02       # tiny gap ⇒ HOLD

    def __init__(
        self,
        pairs: List[str],
        covariance_weight: float = COVARIANCE_WEIGHT,
        regime_drift_weight: float = REGIME_DRIFT_WEIGHT,
    ):
        self.pairs = list(pairs)
        self.covariance_weight = covariance_weight
        self.regime_drift_weight = regime_drift_weight

    # ─── Math helpers ───

    @staticmethod
    def _log_returns(prices: List[float]) -> List[float]:
        out: List[float] = []
        for i in range(1, len(prices)):
            p0, p1 = prices[i - 1], prices[i]
            if p0 > 0 and p1 > 0:
                out.append(math.log(p1 / p0))
        return out

    @staticmethod
    def _covariance(series: List[List[float]]) -> List[List[float]]:
        """Population covariance of N equal-length series. Pure Python."""
        n = len(series)
        if n == 0:
            return []
        k = min(len(s) for s in series)
        if k < 2:
            return [[0.0] * n for _ in range(n)]
        trimmed = [s[-k:] for s in series]
        means = [sum(s) / k for s in trimmed]
        cov = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i, n):
                acc = 0.0
                for t in range(k):
                    acc += (trimmed[i][t] - means[i]) * (trimmed[j][t] - means[j])
                v = acc / k
                cov[i][j] = v
                cov[j][i] = v
        return cov

    def _build_signal_vector(self, all_states: Dict[str, dict]) -> List[float]:
        """h_i = sign(action)*confidence + λ*(p_up - p_down) from the regime filter."""
        h: List[float] = []
        for pair in self.pairs:
            state = all_states.get(pair) or {}
            sig = state.get("signal") or {}
            action = sig.get("action", "HOLD")
            conf = float(sig.get("confidence", 0.0))
            base = 0.0
            if action == "BUY":
                base = conf
            elif action == "SELL":
                base = -conf
            drift = 0.0
            probs = state.get("regime_probs")
            if probs:
                p_up = float(probs.get("TREND_UP", 0.0))
                p_dn = float(probs.get("TREND_DOWN", 0.0))
                drift = p_up - p_dn
            h.append(base + self.regime_drift_weight * drift)
        return h

    def _build_returns(self, all_states: Dict[str, dict]) -> List[List[float]]:
        series: List[List[float]] = []
        for pair in self.pairs:
            state = all_states.get(pair) or {}
            candles = state.get("candles") or []
            closes = [float(c.get("c", 0.0)) for c in candles[-self.WINDOW:]]
            series.append(self._log_returns(closes))
        return series

    @staticmethod
    def _build_override(
        action_type: str,
        joint_action: str,
        confidence: float,
        reason: str,
        e_best: float,
        gap: float,
    ) -> Dict[str, Any]:
        """Shared payload constructor for OVERRIDE/ADJUST emissions."""
        return {
            "action": action_type,
            "signal": joint_action,
            "confidence_adj": confidence,
            "reason": reason,
            "joint_energy": round(e_best, 6),
            "energy_gap": round(gap, 6),
        }

    @staticmethod
    def _energy(s: List[int], h: List[float], cov: List[List[float]], gamma: float) -> float:
        n = len(s)
        lin = -sum(h[i] * s[i] for i in range(n))
        quad = 0.0
        for i in range(n):
            for j in range(n):
                quad += s[i] * cov[i][j] * s[j]
        return lin + gamma * quad

    # ─── Public API ───

    # Exact enumeration is 2^N; beyond 12 pairs this is the hard bottleneck
    # for a 1-minute tick. Guard here rather than silently hanging the tick.
    MAX_PAIRS_EXACT = 12

    def solve(self, all_states: Dict[str, dict]) -> Dict[str, dict]:
        """Run one joint-signal decision pass. Returns per-pair override dicts."""
        n = len(self.pairs)
        if n == 0:
            return {}
        if n > self.MAX_PAIRS_EXACT:
            raise ValueError(
                f"JointSignalSolver: exact 2^n enumeration infeasible for n={n} "
                f"(max {self.MAX_PAIRS_EXACT}); use a heuristic solver instead."
            )

        h = self._build_signal_vector(all_states)
        series = self._build_returns(all_states)
        cov = self._covariance(series) if series else [[0.0] * n for _ in range(n)]

        # Exact enumeration of 2^n spin configurations.
        configs: List[List[int]] = []
        for mask in range(2 ** n):
            configs.append([1 if (mask >> i) & 1 else -1 for i in range(n)])

        energies = [self._energy(s, h, cov, self.covariance_weight) for s in configs]
        order = sorted(range(len(configs)), key=lambda k: energies[k])
        best = configs[order[0]]
        runner_up = configs[order[1]] if len(order) > 1 else best
        e_best = energies[order[0]]
        e_next = energies[order[1]] if len(order) > 1 else e_best
        gap = e_next - e_best  # ≥ 0

        # Map energy gap to confidence in (0,1). Small gap ⇒ low conviction.
        joint_conf = 1.0 - math.exp(-self.GAP_CONFIDENCE_SCALE * max(gap, 0.0))
        joint_conf = max(0.0, min(1.0, joint_conf))

        # Build per-pair overrides only when the joint picture disagrees with
        # the per-pair signal or the gap is large enough to trust.
        overrides: Dict[str, dict] = {}
        for i, pair in enumerate(self.pairs):
            state = all_states.get(pair) or {}
            sig = state.get("signal") or {}
            current_action = sig.get("action", "HOLD")
            current_conf = float(sig.get("confidence", 0.0))

            spin = best[i]
            if gap < self.HOLD_GAP_THRESHOLD:
                joint_action = "HOLD"
            else:
                joint_action = "BUY" if spin > 0 else "SELL"

            # Blend local conviction (|h_i|) with joint conviction
            local = min(1.0, abs(h[i]))
            blended = round(0.5 * local + 0.5 * joint_conf, 4)

            # Covariance-derived reason string
            diag = [cov[j][j] for j in range(n)]
            reason_bits = [
                f"joint_energy={e_best:+.4f}",
                f"gap={gap:.4f}",
                f"cov_diag={[round(d, 6) for d in diag]}",
                f"h={[round(x, 3) for x in h]}",
            ]
            reason = "Joint-signal solver: " + " | ".join(reason_bits)

            # Emit an override only when (a) joint action differs from current,
            # or (b) the blended confidence meaningfully updates current.
            if joint_action != current_action:
                overrides[pair] = self._build_override(
                    "OVERRIDE", joint_action, blended, reason, e_best, gap,
                )
            elif joint_action != "HOLD" and abs(blended - current_conf) > 0.05:
                overrides[pair] = self._build_override(
                    "ADJUST", joint_action, blended, reason, e_best, gap,
                )

        # Coordinated swap detection: pair `i` goes short while pair `j` goes long
        # AND both are in the SOL/{USDC,XBT} triangle with an existing SOL position.
        sol_usdc_idx = self.pairs.index("SOL/USDC") if "SOL/USDC" in self.pairs else -1
        sol_xbt_idx = self.pairs.index("SOL/XBT") if "SOL/XBT" in self.pairs else -1
        if sol_usdc_idx >= 0 and sol_xbt_idx >= 0:
            if best[sol_usdc_idx] < 0 and best[sol_xbt_idx] > 0:
                sol_state = all_states.get("SOL/USDC") or {}
                pos = (sol_state.get("position") or {}).get("size", 0.0)
                if pos > 0 and "SOL/USDC" in overrides:
                    overrides["SOL/USDC"]["swap"] = {
                        "sell_pair": "SOL/USDC",
                        "buy_pair": "SOL/XBT",
                        "reason": "Joint-signal: SOL/USDC short-bias + SOL/XBT long-bias ground state",
                    }

        return overrides


# ═══════════════════════════════════════════════════════════════
# CROSS-PAIR REGIME COORDINATOR
# ═══════════════════════════════════════════════════════════════



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

