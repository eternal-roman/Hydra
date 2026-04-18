"""
HYDRA Thesis Phase B — Brain integration tests.

Validates:
1. _build_analyst_prompt injects the THESIS CONTEXT block verbatim when
   state["thesis_context"] is present (intent prompts appear word-for-word,
   hard-rule warnings surface, posture is named).
2. The prompt returns to its v2.12.5 shape when state has no thesis_context.
3. _format_thesis_context handles both ThesisContext dataclasses and the
   plain-dict shape that WS replays / test doubles may feed.
4. BrainDecision carries thesis_alignment from analyst JSON.

These tests do not hit any live LLM — they inspect prompt construction and
decision field propagation directly.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_thesis import ThesisTracker
from hydra_brain import HydraBrain, BrainDecision


def _minimal_state(asset="BTC/USDC", include_thesis=False, thesis_context=None):
    state = {
        "asset": asset,
        "price": 75_000.0,
        "regime": "RANGING",
        "strategy": "MEAN_REVERSION",
        "candle_interval": 15,
        "candle_status": "closed",
        "signal": {"action": "BUY", "confidence": 0.72, "reason": "RSI<30 rebound"},
        "indicators": {"rsi": 28.5, "macd_line": 0.1, "macd_signal": 0.2,
                       "macd_histogram": -0.1, "bb_lower": 74000, "bb_middle": 75000,
                       "bb_upper": 76000, "bb_width": 0.02},
        "trend": {"ema20": 74900, "ema50": 74700},
        "volatility": {"atr": 450, "atr_pct": 0.6},
        "volume": {"current": 100, "avg_20": 95},
        "candles": [],
        "position": {"size": 0.0, "avg_entry": 0.0, "unrealized_pnl": 0.0},
        "portfolio": {"balance": 100, "equity": 100, "pnl_pct": 0, "max_drawdown_pct": 0},
    }
    if include_thesis and thesis_context is not None:
        state["thesis_context"] = thesis_context
    return state


def _make_brain_no_client():
    """Build a HydraBrain WITHOUT any LLM client — we only test prompt
    construction + decision synthesis from synthetic outputs."""
    # Passing a fake key avoids KeyError; primary_client is never called.
    b = HydraBrain(anthropic_key="sk-ant-fake-test-no-calls")
    b.primary_client = None
    return b


def test_analyst_prompt_omits_thesis_block_when_absent():
    """Without thesis_context in state, the prompt must NOT contain
    'THESIS CONTEXT' — preserving v2.12.5 byte shape for users on default."""
    brain = _make_brain_no_client()
    prompt = brain._build_analyst_prompt(_minimal_state(include_thesis=False))
    assert "THESIS CONTEXT" not in prompt, \
        "Thesis block must not appear when state has no thesis_context"


def test_analyst_prompt_includes_thesis_block_when_present():
    """With a real ThesisContext, posture, posterior, and checklist surface."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        ctx = t.context_for("BTC/USDC", {"action": "BUY"})
        brain = _make_brain_no_client()
        prompt = brain._build_analyst_prompt(_minimal_state(include_thesis=True,
                                                             thesis_context=ctx))
        assert "THESIS CONTEXT" in prompt
        assert "Posture: PRESERVATION" in prompt
        assert "LATE_CYCLE_DIGESTION" in prompt
        assert "/5 met" in prompt  # checklist summary


def test_intent_prompt_text_appears_verbatim():
    """User-authored intent must reach the analyst prompt word-for-word,
    including priority annotation. This is the Phase B value proposition."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.add_intent("lean defensive ahead of CPI release", priority=5)
        ctx = t.context_for("BTC/USDC", {"action": "BUY"})
        brain = _make_brain_no_client()
        prompt = brain._build_analyst_prompt(_minimal_state(include_thesis=True,
                                                             thesis_context=ctx))
        assert "lean defensive ahead of CPI release" in prompt, \
            "Intent prompt text must be injected verbatim"
        assert "p=5" in prompt, "Priority must surface in the context block"


def test_hard_rule_warning_surfaces_on_btc_sell():
    """Ledger-shield warning must reach the analyst on BTC/USDC SELL signals."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        ctx = t.context_for("BTC/USDC", {"action": "SELL"})
        brain = _make_brain_no_client()
        state = _minimal_state(asset="BTC/USDC", include_thesis=True, thesis_context=ctx)
        state["signal"] = {"action": "SELL", "confidence": 0.7, "reason": "overbought"}
        prompt = brain._build_analyst_prompt(state)
        assert "ledger_shield" in prompt.lower()


def test_format_thesis_accepts_plain_dict():
    """Test doubles / WS replays may feed a plain dict instead of the
    dataclass — _format_thesis_context must accept both."""
    brain = _make_brain_no_client()
    block = brain._format_thesis_context({
        "thesis_context": {
            "posture": "ACCUMULATION",
            "posture_enforcement": "binding",
            "posterior_summary": "ACCUMULATION_PHASE @ 0.80",
            "checklist_summary": "4/5 met",
            "active_intents": [
                {"priority": 4, "prompt_text": "buy the dip",
                 "pair_scope": ["BTC/USDC"], "intent_id": "x"}
            ],
            "hard_rule_warnings": [],
            "recent_evidence_summary": "FOMC dovish pivot 2026-04-10",
        }
    })
    assert "Posture: ACCUMULATION (binding)" in block
    assert "buy the dip" in block
    assert "FOMC dovish pivot 2026-04-10" in block


def test_brain_decision_carries_thesis_alignment():
    """BrainDecision dataclass must accept and preserve thesis_alignment —
    agent + journal stamping depend on this field."""
    decision = BrainDecision(
        action="CONFIRM", final_signal="BUY", confidence_adj=0.75,
        size_multiplier=1.0, analyst_reasoning="t", risk_reasoning="r",
        thesis_alignment={
            "in_thesis": True,
            "intent_prompts_consulted": ["01HKZABCD"],
            "evidence_delta": "order book imbalance confirms prior",
            "posterior_shift_request": 0.02,
        },
    )
    assert decision.thesis_alignment["in_thesis"] is True
    assert "01HKZABCD" in decision.thesis_alignment["intent_prompts_consulted"]


def test_brain_decision_thesis_alignment_defaults_to_none():
    d = BrainDecision(action="CONFIRM", final_signal="HOLD", confidence_adj=0.5,
                      size_multiplier=1.0, analyst_reasoning="", risk_reasoning="")
    assert d.thesis_alignment is None


def run_tests():
    fns = [
        test_analyst_prompt_omits_thesis_block_when_absent,
        test_analyst_prompt_includes_thesis_block_when_present,
        test_intent_prompt_text_appears_verbatim,
        test_hard_rule_warning_surfaces_on_btc_sell,
        test_format_thesis_accepts_plain_dict,
        test_brain_decision_carries_thesis_alignment,
        test_brain_decision_thesis_alignment_defaults_to_none,
    ]
    passed = 0
    failed = 0
    errors = []
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            errors.append((fn.__name__, e))
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            errors.append((fn.__name__, e))
            print(f"  ERROR {fn.__name__}: {e}")
    print(f"\n  {'=' * 60}")
    print(f"  Thesis Phase B Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'=' * 60}")
    if errors:
        print("\n  FAILURES:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
