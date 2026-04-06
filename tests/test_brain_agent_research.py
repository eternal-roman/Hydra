"""
HYDRA Brain Agent Test Suite

Covers the new genuine-agentic brain layer added to hydra_brain.py:
  - BrainMemory: episodic memory, belief EMA, plan storage, round-trip dict
  - GoalState & risk posture: drawdown-budget-driven posture promotion
  - PlanStep / BrainPlan: multi-tick plan follow-through
  - HydraBrain.step(): plan consultation, episode recording, fallback path
  - reflect(): back-filling realised PnL and regret on closed trades

These tests intentionally never make network calls — the brain is constructed
without Anthropic / xAI keys and uses the monkey-patched pipeline for the
step() path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_brain import (
    BrainMemory,
    BrainDecision,
    BrainPlan,
    PlanStep,
    Episode,
    GoalState,
    HydraBrain,
)


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def make_state(
    pair="SOL/USDC",
    action="BUY",
    confidence=0.8,
    regime="TREND_UP",
    rsi=55.0,
    dd_pct=1.0,
    pnl_pct=0.5,
    tick=1,
):
    return {
        "tick": tick,
        "asset": pair,
        "price": 100.0,
        "regime": regime,
        "strategy": "MOMENTUM",
        "signal": {"action": action, "confidence": confidence, "reason": "test"},
        "position": {"size": 0.0, "avg_entry": 0.0, "unrealized_pnl": 0.0},
        "portfolio": {
            "balance": 100.0, "equity": 100.0, "pnl_pct": pnl_pct,
            "max_drawdown_pct": dd_pct, "peak_equity": 100.0,
        },
        "indicators": {"rsi": rsi, "macd_line": 0.0, "macd_signal": 0.0,
                        "macd_histogram": 0.0, "bb_upper": 101, "bb_middle": 100,
                        "bb_lower": 99, "bb_width": 0.02},
        "trend": {"ema20": 100.5, "ema50": 100.0},
        "volatility": {"atr": 1.0, "atr_pct": 1.5},
        "volume": {"current": 10.0, "avg_20": 9.5},
        "candles": [],
    }


class StubBrain(HydraBrain):
    """HydraBrain subclass that bypasses constructor (no API keys) and
    replaces the pipeline call with a deterministic stub. Used to exercise
    the agentic step() loop without any network I/O."""

    def __init__(self):  # noqa: D401 — deliberately skip parent __init__
        self.memory = BrainMemory()
        self.decision_history = {}
        self.tick_counter = 0
        # Minimal attributes step() reads indirectly
        self.api_available = True

    def _deliberate_pipeline(self, state):
        # Always CONFIRM the engine signal with a fixed size multiplier
        sig = state.get("signal", {})
        return BrainDecision(
            action="CONFIRM",
            final_signal=sig.get("action", "HOLD"),
            confidence_adj=sig.get("confidence", 0.0),
            size_multiplier=1.0,
            analyst_reasoning="stub",
            risk_reasoning="stub",
            combined_summary="stub decision",
        )


# ═══════════════════════════════════════════════════════════════
# 1. BRAIN MEMORY
# ═══════════════════════════════════════════════════════════════

class TestBrainMemory:
    def test_add_and_recent_episodes(self):
        mem = BrainMemory()
        for i in range(5):
            mem.add_episode(Episode(
                tick=i, timestamp=float(i), pair="SOL/USDC",
                state_digest=[0.0] * 7, action="CONFIRM",
                signal="BUY", confidence=0.8,
            ))
        recent = mem.recent_episodes("SOL/USDC", n=3)
        assert len(recent) == 3
        assert recent[-1].tick == 4

    def test_episode_buffer_bounded(self):
        mem = BrainMemory()
        for i in range(BrainMemory.MAX_EPISODES + 50):
            mem.add_episode(Episode(
                tick=i, timestamp=float(i), pair="SOL/USDC",
                state_digest=[0.0] * 7, action="CONFIRM",
                signal="HOLD", confidence=0.1,
            ))
        assert len(mem.episodes["SOL/USDC"]) == BrainMemory.MAX_EPISODES

    def test_round_trip_serialisation(self):
        mem = BrainMemory()
        mem.add_episode(Episode(
            tick=1, timestamp=1.0, pair="SOL/USDC",
            state_digest=[0.1] * 7, action="CONFIRM",
            signal="BUY", confidence=0.6, closed=True, realized_pnl=2.5,
        ))
        mem.set_plan("SOL/USDC", BrainPlan(
            goal="test plan", horizon_ticks=10, created_at_tick=0,
            steps=[PlanStep(if_condition={"min_rsi": 70.0},
                            then_action={"final_action": "SELL"})],
        ))
        data = mem.to_dict()
        restored = BrainMemory.from_dict(data)
        assert restored.get_plan("SOL/USDC") is not None
        assert restored.get_plan("SOL/USDC").goal == "test plan"
        assert len(restored.episodes["SOL/USDC"]) == 1
        assert list(restored.episodes["SOL/USDC"])[0].realized_pnl == 2.5


# ═══════════════════════════════════════════════════════════════
# 2. GOAL STATE & RISK POSTURE
# ═══════════════════════════════════════════════════════════════

class TestGoalPosture:
    def test_conservative_when_drawdown_near_budget(self):
        brain = StubBrain()
        brain.memory.goals.drawdown_budget_pct = 10.0
        brain._update_goal_posture(make_state(dd_pct=8.0))
        assert brain.memory.goals.risk_posture == "conservative"

    def test_neutral_when_drawdown_midway(self):
        brain = StubBrain()
        brain.memory.goals.drawdown_budget_pct = 10.0
        brain._update_goal_posture(make_state(dd_pct=5.0))
        assert brain.memory.goals.risk_posture == "neutral"

    def test_aggressive_when_profitable_and_safe(self):
        brain = StubBrain()
        brain.memory.goals.drawdown_budget_pct = 10.0
        brain._update_goal_posture(make_state(dd_pct=0.5, pnl_pct=2.0))
        assert brain.memory.goals.risk_posture == "aggressive"


# ═══════════════════════════════════════════════════════════════
# 3. PLAN FOLLOW-THROUGH
# ═══════════════════════════════════════════════════════════════

class TestPlanFollowThrough:
    def test_matching_condition_fires_plan_step_without_pipeline(self):
        brain = StubBrain()
        # Install a plan whose first step matches min_rsi >= 70
        plan = BrainPlan(
            goal="take profit",
            horizon_ticks=20,
            created_at_tick=0,
            steps=[PlanStep(
                if_condition={"min_rsi": 70.0},
                then_action={"final_action": "SELL", "confidence": 0.9, "reason": "RSI exit"},
            )],
        )
        brain.memory.set_plan("SOL/USDC", plan)
        decision = brain.step(make_state(action="HOLD", rsi=72.0, tick=5))
        assert decision.action == "PLAN_STEP"
        assert decision.final_signal == "SELL"
        # Plan cleared after last step fires
        assert brain.memory.get_plan("SOL/USDC") is None

    def test_non_matching_condition_delegates_to_pipeline(self):
        brain = StubBrain()
        plan = BrainPlan(
            goal="take profit",
            horizon_ticks=20,
            created_at_tick=0,
            steps=[PlanStep(
                if_condition={"min_rsi": 80.0},
                then_action={"final_action": "SELL", "confidence": 0.9},
            )],
        )
        brain.memory.set_plan("SOL/USDC", plan)
        decision = brain.step(make_state(action="BUY", rsi=55.0, tick=5))
        # Plan stayed, pipeline ran
        assert brain.memory.get_plan("SOL/USDC") is not None
        assert decision.action == "CONFIRM"

    def test_expired_plan_cleared(self):
        brain = StubBrain()
        plan = BrainPlan(
            goal="x", horizon_ticks=5, created_at_tick=0,
            steps=[PlanStep(if_condition={"min_rsi": 999.0},
                            then_action={"final_action": "HOLD"})],
        )
        brain.memory.set_plan("SOL/USDC", plan)
        # Low-confidence state so the pipeline does NOT register a followup plan
        brain.step(make_state(tick=100, action="HOLD", confidence=0.1))
        assert brain.memory.get_plan("SOL/USDC") is None

    def test_high_conviction_signal_registers_followup_plan(self):
        brain = StubBrain()
        # Stub pipeline emits confidence 0.85 — above the 0.7 plan threshold
        def hiconf(state):
            return BrainDecision(
                action="CONFIRM", final_signal="BUY", confidence_adj=0.85,
                size_multiplier=1.0, analyst_reasoning="x", risk_reasoning="y",
            )
        brain._deliberate_pipeline = hiconf  # type: ignore
        brain.step(make_state(action="BUY", confidence=0.85))
        plan = brain.memory.get_plan("SOL/USDC")
        assert plan is not None
        assert len(plan.steps) == 2


# ═══════════════════════════════════════════════════════════════
# 4. CLAMP + EPISODE RECORDING
# ═══════════════════════════════════════════════════════════════

class TestClampAndRecording:
    def test_size_multiplier_clamped_to_1_25(self):
        brain = StubBrain()
        def huge(state):
            return BrainDecision(
                action="CONFIRM", final_signal="BUY", confidence_adj=0.6,
                size_multiplier=99.0, analyst_reasoning="x", risk_reasoning="y",
            )
        brain._deliberate_pipeline = huge  # type: ignore
        decision = brain.step(make_state())
        assert decision.size_multiplier == 1.25

    def test_size_multiplier_clamped_to_zero_lower_bound(self):
        brain = StubBrain()
        def neg(state):
            return BrainDecision(
                action="CONFIRM", final_signal="BUY", confidence_adj=0.6,
                size_multiplier=-5.0, analyst_reasoning="x", risk_reasoning="y",
            )
        brain._deliberate_pipeline = neg  # type: ignore
        decision = brain.step(make_state())
        assert decision.size_multiplier == 0.0

    def test_conservative_posture_caps_size_at_0_5(self):
        brain = StubBrain()
        brain.memory.goals.drawdown_budget_pct = 10.0
        decision = brain.step(make_state(dd_pct=9.0))
        assert brain.memory.goals.risk_posture == "conservative"
        assert decision.size_multiplier <= 0.5

    def test_step_records_episode(self):
        brain = StubBrain()
        brain.step(make_state(tick=42))
        eps = brain.memory.recent_episodes("SOL/USDC", n=1)
        assert len(eps) == 1
        assert eps[0].tick == 42


# ═══════════════════════════════════════════════════════════════
# 5. REFLECTION (back-fill outcomes)
# ═══════════════════════════════════════════════════════════════

class TestReflection:
    def test_reflect_backfills_most_recent_open_episode(self):
        brain = StubBrain()
        brain.step(make_state(tick=1))
        brain.step(make_state(tick=2))
        brain.reflect("SOL/USDC", {"profit": 3.5, "action": "SELL"})
        eps = list(brain.memory.episodes["SOL/USDC"])
        # The most recently-recorded (tick=2) should be closed now
        assert eps[-1].closed is True
        assert eps[-1].realized_pnl == 3.5

    def test_reflect_without_episodes_is_safe(self):
        brain = StubBrain()
        brain.reflect("SOL/USDC", {"profit": 1.0})  # should not raise


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    classes = [
        TestBrainMemory,
        TestGoalPosture,
        TestPlanFollowThrough,
        TestClampAndRecording,
        TestReflection,
    ]
    total, passed, failed, errors = 0, 0, 0, []
    for cls in classes:
        inst = cls()
        for m in sorted(x for x in dir(inst) if x.startswith("test_")):
            total += 1
            try:
                getattr(inst, m)()
                passed += 1
                print(f"  PASS  {cls.__name__}.{m}")
            except AssertionError as e:
                failed += 1
                errors.append((cls.__name__, m, e))
                print(f"  FAIL  {cls.__name__}.{m}: {e}")
            except Exception as e:
                failed += 1
                errors.append((cls.__name__, m, e))
                print(f"  ERROR {cls.__name__}.{m}: {e}")
    print(f"\n  {'='*60}")
    print(f"  Brain Agent Tests: {passed}/{total} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        for c, m, e in errors:
            print(f"    {c}.{m}: {e}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_tests() else 1)
