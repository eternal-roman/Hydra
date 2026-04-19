"""Unit tests for Phase 5: HydraBrain tool-use loop.

We test the tool-use infrastructure with a mock Anthropic client (no API
calls). Covers:
  - Gating: tool-use disabled by default; env flag + param flag both work
  - Content-block normalization (SDK object or dict)
  - Single-shot end_turn (no tool calls)
  - Single tool call then end_turn
  - Multiple tool calls in one turn
  - Exception isolation: dispatcher raise → returns empty-text fallback
  - Iteration cap: runaway loop is bounded
  - Non-anthropic provider + tool-use returns empty (defensive)
  - Token accounting accumulates across iterations

Brain is constructed with a fake anthropic_key; the real anthropic SDK will
accept any string at __init__ time (it doesn't validate until a network
call) — we never make a network call here because we monkey-patch the
client.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Ensure the env flag isn't leaked in from a shell session
os.environ.pop("HYDRA_BRAIN_TOOLS_ENABLED", None)
os.environ.pop("HYDRA_DEBUG_TOOLS", None)

import hydra_brain  # noqa: E402
from hydra_brain import HydraBrain  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# Fake Anthropic response builders
# ═══════════════════════════════════════════════════════════════

@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: Dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeResponse:
    content: List[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _FakeUsage = field(default_factory=_FakeUsage)


class _ScriptedMessages:
    """Replaces Anthropic messages API with a deterministic script.

    scripted_responses: consumed in order per create() call. Test authors
    queue [resp1, resp2, ...] and each create() pops from the front.
    last_request captures the kwargs for assertion.
    """
    def __init__(self, scripted_responses: List[_FakeResponse]):
        self._queue = list(scripted_responses)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("scripted response queue exhausted")
        return self._queue.pop(0)


class _FakeClient:
    def __init__(self, scripted_responses: List[_FakeResponse]):
        self.messages = _ScriptedMessages(scripted_responses)


# ═══════════════════════════════════════════════════════════════
# Brain builder: strip the real anthropic client
# ═══════════════════════════════════════════════════════════════

def _make_brain_with_fake_client(
    fake_client: _FakeClient,
    dispatcher: Any = None,
    enable: bool = True,
) -> HydraBrain:
    # Construct with a dummy key, then swap the client
    brain = HydraBrain(
        anthropic_key="sk-ant-fake-test",
        tool_dispatcher=dispatcher,
        enable_tool_use=enable,
    )
    brain.primary_client = fake_client
    return brain


# ═══════════════════════════════════════════════════════════════
# Gating
# ═══════════════════════════════════════════════════════════════

class TestGating(unittest.TestCase):
    def test_default_is_disabled(self):
        # No dispatcher + no flag → tool-use off
        brain = HydraBrain(anthropic_key="sk-ant-fake")
        self.assertFalse(brain._tool_use_enabled)

    def test_dispatcher_alone_not_enough(self):
        dispatcher = MagicMock()
        brain = HydraBrain(anthropic_key="sk-ant-fake", tool_dispatcher=dispatcher)
        self.assertFalse(brain._tool_use_enabled)

    def test_param_enables(self):
        dispatcher = MagicMock()
        brain = HydraBrain(
            anthropic_key="sk-ant-fake",
            tool_dispatcher=dispatcher, enable_tool_use=True,
        )
        self.assertTrue(brain._tool_use_enabled)

    def test_env_flag_enables(self):
        dispatcher = MagicMock()
        os.environ["HYDRA_BRAIN_TOOLS_ENABLED"] = "1"
        try:
            brain = HydraBrain(
                anthropic_key="sk-ant-fake", tool_dispatcher=dispatcher,
            )
            self.assertTrue(brain._tool_use_enabled)
        finally:
            os.environ.pop("HYDRA_BRAIN_TOOLS_ENABLED", None)

    def test_param_overrides_env_to_disable(self):
        dispatcher = MagicMock()
        os.environ["HYDRA_BRAIN_TOOLS_ENABLED"] = "1"
        try:
            brain = HydraBrain(
                anthropic_key="sk-ant-fake", tool_dispatcher=dispatcher,
                enable_tool_use=False,
            )
            self.assertFalse(brain._tool_use_enabled)
        finally:
            os.environ.pop("HYDRA_BRAIN_TOOLS_ENABLED", None)

    def test_non_anthropic_provider_not_enabled(self):
        # Primary xAI — tool-use schema is anthropic-only, so enabled must be False
        dispatcher = MagicMock()
        if not hydra_brain.HAS_OPENAI:
            self.skipTest("openai SDK not installed")
        brain = HydraBrain(
            xai_key="xai-fake", tool_dispatcher=dispatcher, enable_tool_use=True,
        )
        self.assertEqual(brain.primary_provider, "xai")
        self.assertFalse(brain._tool_use_enabled)


# ═══════════════════════════════════════════════════════════════
# Content block normalization
# ═══════════════════════════════════════════════════════════════

class TestContentBlockToDict(unittest.TestCase):
    def test_text_block_object(self):
        b = _FakeTextBlock(text="hello")
        out = HydraBrain._content_block_to_dict(b)
        self.assertEqual(out, {"type": "text", "text": "hello"})

    def test_tool_use_block_object(self):
        b = _FakeToolUseBlock(id="tu1", name="run_backtest", input={"preset": "default"})
        out = HydraBrain._content_block_to_dict(b)
        self.assertEqual(out, {
            "type": "tool_use",
            "id": "tu1",
            "name": "run_backtest",
            "input": {"preset": "default"},
        })

    def test_dict_passthrough(self):
        d = {"type": "text", "text": "hi"}
        self.assertIs(HydraBrain._content_block_to_dict(d), d)

    def test_unknown_type_marker(self):
        out = HydraBrain._content_block_to_dict(MagicMock(type="bogus"))
        self.assertEqual(out["type"], "text")
        self.assertIn("bogus", out["text"])


# ═══════════════════════════════════════════════════════════════
# Tool-use loop semantics
# ═══════════════════════════════════════════════════════════════

class TestToolUseLoop(unittest.TestCase):
    def _fake_response_text(self, text: str, tokens=(10, 5)) -> _FakeResponse:
        return _FakeResponse(
            content=[_FakeTextBlock(text=text)],
            stop_reason="end_turn",
            usage=_FakeUsage(tokens[0], tokens[1]),
        )

    def _fake_response_tool_use(self, tool_id: str, tool_name: str, tool_input: Dict,
                                 tokens=(8, 4)) -> _FakeResponse:
        return _FakeResponse(
            content=[_FakeToolUseBlock(id=tool_id, name=tool_name, input=tool_input)],
            stop_reason="tool_use",
            usage=_FakeUsage(tokens[0], tokens[1]),
        )

    def test_end_turn_no_tools(self):
        # Simplest case: LLM answers immediately without calling a tool
        fake = _FakeClient([self._fake_response_text('{"thesis": "ok"}', tokens=(20, 10))])
        dispatcher = MagicMock()
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)

        text, tok_in, tok_out, calls = brain._call_llm_with_tools(
            "system", "user message", tools=[], caller="brain:analyst",
        )
        self.assertEqual(text, '{"thesis": "ok"}')
        self.assertEqual(tok_in, 20)
        self.assertEqual(tok_out, 10)
        self.assertEqual(calls, 0)
        dispatcher.execute.assert_not_called()

    def test_single_tool_use_then_end(self):
        fake = _FakeClient([
            self._fake_response_tool_use("tu1", "list_presets", {}, tokens=(30, 8)),
            self._fake_response_text('{"thesis": "used tools"}', tokens=(45, 12)),
        ])
        dispatcher = MagicMock()
        dispatcher.execute.return_value = {"success": True,
                                           "data": [{"name": "default"}]}
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)

        text, tok_in, tok_out, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        self.assertEqual(text, '{"thesis": "used tools"}')
        self.assertEqual(tok_in, 30 + 45)
        self.assertEqual(tok_out, 8 + 12)
        self.assertEqual(calls, 1)
        dispatcher.execute.assert_called_once_with("list_presets", {},
                                                   caller="brain:analyst")

    def test_multiple_tools_in_one_turn(self):
        # Anthropic can emit several tool_use blocks in a single response
        fake = _FakeClient([
            _FakeResponse(
                content=[
                    _FakeToolUseBlock(id="tu1", name="list_presets", input={}),
                    _FakeToolUseBlock(id="tu2", name="find_best",
                                      input={"metric": "sharpe"}),
                ],
                stop_reason="tool_use",
                usage=_FakeUsage(40, 15),
            ),
            self._fake_response_text('{"thesis": "both ran"}'),
        ])
        dispatcher = MagicMock()
        dispatcher.execute.side_effect = [
            {"success": True, "data": [{"name": "default"}]},
            {"success": True, "data": {"id": "abc", "metrics": {"sharpe": 1.5}}},
        ]
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)

        text, _tin, _tout, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        self.assertEqual(text, '{"thesis": "both ran"}')
        self.assertEqual(calls, 2)
        self.assertEqual(dispatcher.execute.call_count, 2)

    def test_iteration_cap_bounds_runaway(self):
        # LLM keeps calling tools forever — loop must bail at the cap
        forever_tool_calls = [
            self._fake_response_tool_use(f"tu{i}", "list_presets", {},
                                         tokens=(5, 2))
            for i in range(10)
        ]
        fake = _FakeClient(forever_tool_calls)
        dispatcher = MagicMock()
        dispatcher.execute.return_value = {"success": True, "data": []}
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)
        brain._tool_iterations_cap = 3

        text, _tin, _tout, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        # Hit the cap → returns empty text; dispatcher invoked exactly cap times
        self.assertEqual(text, "")
        self.assertEqual(calls, 3)

    def test_exception_fallback_empty_text(self):
        # API call raises — loop catches, returns (""\, accumulated tokens so far, 0)
        fake = MagicMock()
        fake.messages.create.side_effect = RuntimeError("api dead")
        dispatcher = MagicMock()
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)

        text, tok_in, tok_out, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        self.assertEqual(text, "")
        self.assertEqual(calls, 0)

    def test_non_anthropic_returns_empty_defensively(self):
        # If someone calls _call_llm_with_tools on a non-anthropic brain,
        # it should bail gracefully, not crash.
        dispatcher = MagicMock()
        if not hydra_brain.HAS_OPENAI:
            self.skipTest("openai SDK not installed")
        brain = HydraBrain(xai_key="xai-fake",
                           tool_dispatcher=dispatcher, enable_tool_use=True)
        text, tok_in, tok_out, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        self.assertEqual(text, "")
        self.assertEqual(tok_in, 0)
        self.assertEqual(tok_out, 0)

    def test_token_accumulation_across_iterations(self):
        fake = _FakeClient([
            self._fake_response_tool_use("tu1", "list_presets", {}, tokens=(10, 3)),
            self._fake_response_tool_use("tu2", "list_presets", {}, tokens=(11, 4)),
            self._fake_response_text('{"thesis": "done"}', tokens=(12, 5)),
        ])
        dispatcher = MagicMock()
        dispatcher.execute.return_value = {"success": True, "data": []}
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)
        _text, tok_in, tok_out, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        self.assertEqual(tok_in, 10 + 11 + 12)
        self.assertEqual(tok_out, 3 + 4 + 5)
        self.assertEqual(calls, 2)

    def test_dispatcher_error_result_surfaced_as_is_error(self):
        # When dispatcher returns {success: False}, the tool_result block
        # must carry is_error=True so the LLM knows it wasn't actionable data
        fake = _FakeClient([
            self._fake_response_tool_use("tu1", "run_backtest", {"preset": "x"}),
            self._fake_response_text('{"thesis": "error handled"}'),
        ])
        dispatcher = MagicMock()
        dispatcher.execute.return_value = {"success": False,
                                           "error": "unknown preset"}
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)
        brain._call_llm_with_tools("system", "user", tools=[],
                                   caller="brain:analyst")
        # Round 2 messages[-1] should contain the tool_result with is_error=True
        second_call = fake.messages.calls[1]
        user_msg = second_call["messages"][-1]
        self.assertEqual(user_msg["role"], "user")
        result_block = user_msg["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertTrue(result_block["is_error"])

    def test_lifetime_counter_increments(self):
        fake = _FakeClient([
            self._fake_response_tool_use("tu1", "list_presets", {}),
            self._fake_response_text('{"thesis": "ok"}'),
        ])
        dispatcher = MagicMock()
        dispatcher.execute.return_value = {"success": True, "data": []}
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)
        self.assertEqual(brain._tool_use_calls, 0)
        brain._call_llm_with_tools("system", "user", tools=[],
                                   caller="brain:analyst")
        self.assertEqual(brain._tool_use_calls, 1)

    def test_missing_dispatcher_returns_error_tool_result(self):
        # If somehow we enter the loop without a dispatcher, we still must
        # produce valid tool_result blocks (with is_error=True) — crashing
        # here would corrupt deliberate()'s token accounting.
        fake = _FakeClient([
            self._fake_response_tool_use("tu1", "list_presets", {}),
            self._fake_response_text('{"thesis": "no tool"}'),
        ])
        brain = _make_brain_with_fake_client(fake, dispatcher=None, enable=False)
        # Bypass the gating to exercise the defensive path
        brain._tool_dispatcher = None
        brain._tool_use_enabled = True  # force the path
        brain.primary_provider = "anthropic"
        text, _tin, _tout, calls = brain._call_llm_with_tools(
            "system", "user", tools=[], caller="brain:analyst",
        )
        self.assertEqual(text, '{"thesis": "no tool"}')
        self.assertEqual(calls, 0)  # nothing dispatched


# ═══════════════════════════════════════════════════════════════
# Analyst / risk branching
# ═══════════════════════════════════════════════════════════════

class TestAnalystRiskBranching(unittest.TestCase):
    def test_analyst_takes_tool_path_when_enabled(self):
        fake = _FakeClient([
            _FakeResponse(content=[_FakeTextBlock(
                text='{"thesis": "t", "signal_agreement": true, '
                     '"suggested_action": "BUY", "conviction": 0.7}')],
                stop_reason="end_turn", usage=_FakeUsage(20, 15)),
        ])
        dispatcher = MagicMock()
        brain = _make_brain_with_fake_client(fake, dispatcher=dispatcher)
        self.assertTrue(brain._tool_use_enabled)
        state = {
            "pair": "SOL/USDC", "price": 100.0,
            "signal": {"action": "BUY", "confidence": 0.8,
                       "reason": "test"},
            "regime": "TREND_UP", "strategy": "MOMENTUM",
            "indicators": {},
            "portfolio": {"equity": 100, "balance": 100, "pnl_pct": 0.0,
                          "max_drawdown_pct": 0.0},
            "performance": {"total_trades": 0, "win_count": 0, "loss_count": 0,
                            "fee_paid": 0.0, "realized_pnl": 0.0},
            "position": {"size": 0, "avg_entry": 0, "unrealized_pnl": 0},
            "portfolio_overview": {},
        }
        parsed, _tin, _tout = brain._run_quant(state)
        self.assertIsNotNone(parsed)
        self.assertIn("thesis", parsed)

    def test_analyst_takes_legacy_path_when_disabled(self):
        # Use a single-response fake (the legacy path calls once, no loop)
        fake = _FakeClient([
            _FakeResponse(content=[_FakeTextBlock(
                text='{"thesis": "legacy", "signal_agreement": true, '
                     '"suggested_action": "BUY", "conviction": 0.7}')],
                stop_reason="end_turn", usage=_FakeUsage(15, 8)),
        ])
        # No dispatcher + no enable → tool-use disabled, _call_llm gets called
        brain = _make_brain_with_fake_client(fake, dispatcher=None, enable=False)
        self.assertFalse(brain._tool_use_enabled)

        state = {
            "pair": "SOL/USDC", "price": 100.0,
            "signal": {"action": "BUY", "confidence": 0.8, "reason": "x"},
            "regime": "TREND_UP", "strategy": "MOMENTUM",
            "indicators": {},
            "portfolio": {"equity": 100, "balance": 100, "pnl_pct": 0.0,
                          "max_drawdown_pct": 0.0},
            "performance": {"total_trades": 0, "win_count": 0, "loss_count": 0,
                            "fee_paid": 0.0, "realized_pnl": 0.0},
            "position": {"size": 0, "avg_entry": 0, "unrealized_pnl": 0},
            "portfolio_overview": {},
        }
        parsed, _tin, _tout = brain._run_quant(state)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["thesis"], "legacy")
        # Legacy path: the request has NO `tools` kwarg
        first_call = fake.messages.calls[0]
        self.assertNotIn("tools", first_call)


# ═══════════════════════════════════════════════════════════════
# Risk Manager size_multiplier clamp
# ═══════════════════════════════════════════════════════════════

class TestRiskManagerSizeMultiplierClamp(unittest.TestCase):
    """A model hallucination can emit size_multiplier outside [0.0, 1.5]
    or as a non-numeric. Downstream Kelly sizing multiplies balance by this
    directly, so the clamp in _run_risk_manager is load-bearing."""

    def _state(self):
        return {
            "asset": "SOL/USDC",
            "signal": {"action": "BUY", "confidence": 0.72, "reason": "test"},
            "regime": "TREND_UP", "strategy": "MOMENTUM",
            "indicators": {}, "portfolio": {"equity": 100, "balance": 100,
                                            "pnl_pct": 0.0, "max_drawdown_pct": 0.0},
            "performance": {"total_trades": 0, "win_count": 0, "loss_count": 0,
                            "fee_paid": 0.0, "realized_pnl": 0.0},
            "position": {"size": 0, "avg_entry": 0, "unrealized_pnl": 0},
            "portfolio_overview": {},
        }

    def _brain_returning(self, raw_value) -> HydraBrain:
        """Make a brain whose Risk Manager returns the given size_multiplier."""
        # Build a full, otherwise-valid risk response so _parse_json succeeds.
        payload = {
            "decision": "CONFIRM",
            "final_action": "BUY",
            "size_multiplier": raw_value,
            "reasoning": "test",
            "risk_flags": [],
            "portfolio_health": "HEALTHY",
        }
        fake = _FakeClient([
            _FakeResponse(
                content=[_FakeTextBlock(text=json.dumps(payload))],
                stop_reason="end_turn",
                usage=_FakeUsage(10, 5),
            )
        ])
        # Tool-use disabled so _run_risk_manager takes the legacy _call_llm path
        return _make_brain_with_fake_client(fake, enable=False)

    def test_clamp_above_max(self):
        brain = self._brain_returning(2.5)
        parsed, _tin, _tout = brain._run_risk_manager(self._state(), {"thesis": "t"})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["size_multiplier"], 1.5)

    def test_clamp_below_min(self):
        brain = self._brain_returning(-0.3)
        parsed, _tin, _tout = brain._run_risk_manager(self._state(), {"thesis": "t"})
        self.assertEqual(parsed["size_multiplier"], 0.0)

    def test_non_numeric_defaults_to_one(self):
        brain = self._brain_returning("abc")
        parsed, _tin, _tout = brain._run_risk_manager(self._state(), {"thesis": "t"})
        self.assertEqual(parsed["size_multiplier"], 1.0)

    def test_in_range_passes_through(self):
        brain = self._brain_returning(0.75)
        parsed, _tin, _tout = brain._run_risk_manager(self._state(), {"thesis": "t"})
        self.assertEqual(parsed["size_multiplier"], 0.75)

    def test_boundary_values_pass_through(self):
        # 0.0 and 1.5 are valid; must not be altered
        brain_low = self._brain_returning(0.0)
        parsed_low, _i, _o = brain_low._run_risk_manager(self._state(), {"thesis": "t"})
        self.assertEqual(parsed_low["size_multiplier"], 0.0)

        brain_high = self._brain_returning(1.5)
        parsed_high, _i, _o = brain_high._run_risk_manager(self._state(), {"thesis": "t"})
        self.assertEqual(parsed_high["size_multiplier"], 1.5)


if __name__ == "__main__":
    unittest.main()
