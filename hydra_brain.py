#!/usr/bin/env python3
"""
HYDRA Brain — Multi-Agent AI Reasoning Layer (3-Agent Pipeline)

Agent 1: Market Analyst (Claude Sonnet) — fast technical analysis
Agent 2: Risk Manager (Claude Sonnet) — risk assessment and approval
Agent 3: Strategic Advisor (Grok 4 Reasoning) — deep analysis on contested decisions

Grok only fires on genuine disagreements: Risk Manager OVERRIDE, or analyst
disagrees with engine at low conviction (< 0.50). ADJUST does not trigger Grok.

Usage:
    brain = HydraBrain(anthropic_key="sk-ant-...", xai_key="xai-...")
    decision = brain.deliberate(engine_state)
"""

import json
import os
import time
import re
import threading
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
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
4. PORTFOLIO-AWARE — consider the aggregate portfolio P&L and cross-pair dynamics when assessing conviction

Respond ONLY with this JSON (no other text):
{
  "thesis": "1-3 sentence market thesis",
  "signal_agreement": true or false,
  "suggested_action": "BUY" or "SELL" or "HOLD",
  "conviction": 0.0 to 1.0,
  "key_factors": ["factor1", "factor2"],
  "concern": "primary risk or null"
}"""

RISK_MANAGER_PROMPT = """You are HYDRA's Risk Manager. You balance capital protection with opportunity capture. You receive the engine's signal, the analyst's thesis, and portfolio state.

Your mandate:
- NEVER allow a trade when drawdown exceeds 10% — only HOLD or SELL
- Scale down size_multiplier when multiple risk factors align
- Override to HOLD if analyst and engine disagree and conviction < 0.6
- Override to SELL if drawdown is accelerating
- CONFIRM good setups with size_multiplier 1.0 — a missed good trade is also a cost
- ADJUST by lowering size_multiplier (0.3–0.8) when cautious
- When the engine signal is strong (confidence >= 0.80) and no concrete risk flags exist, prefer CONFIRM at 1.0 over precautionary reduction
- Consider PORTFOLIO OVERVIEW when assessing aggregate risk — if portfolio is net profitable, slightly more latitude; if bleeding across multiple pairs, tighten
- Use OVERRIDE sparingly — only when you identify a specific, articulable risk, not general caution

Respond ONLY with this JSON (no other text):
{
  "decision": "CONFIRM" or "ADJUST" or "OVERRIDE",
  "final_action": "BUY" or "SELL" or "HOLD",
  "size_multiplier": 0.0 to 1.5,
  "reasoning": "1-2 sentence risk assessment",
  "risk_flags": ["flag1", "flag2"],
  "portfolio_health": "HEALTHY" or "CAUTION" or "DANGER"
}"""

STRATEGIST_PROMPT = """You are HYDRA's Strategic Advisor, called in to resolve a specific disagreement in the trading pipeline.

You receive:
- The engine's quantitative signal (rule-based)
- The Market Analyst's thesis (AI analysis)
- The Risk Manager's assessment (which triggered this escalation)

Your job: Decide whether the trade should proceed or be blocked. You are resolving a SPECIFIC disagreement — you do NOT re-evaluate sizing or confidence.

Consider:
1. Whether the Risk Manager's concern is based on a concrete, current risk or general caution
2. Whether the opportunity justifies the identified risk
3. The broader market context from the price action

Think step by step. Then respond ONLY with this JSON:
{
  "final_action": "BUY" or "SELL" or "HOLD",
  "reasoning": "2-4 sentence analysis of why you agree or disagree with the risk override",
  "decision": "CONFIRM" or "OVERRIDE"
}"""

# Appended to ANALYST_PROMPT / RISK_MANAGER_PROMPT when tool-use is enabled.
# Keeps the base prompts untouched for the no-tools path (drift-sensitive —
# existing prompt wording is load-bearing for the JSON output contract).
TOOLS_GUIDANCE = """

Tools available (Anthropic tool-use format):
- run_backtest(preset, hypothesis, overrides?, n_candles?): validate a concrete \
hypothesis against a synthetic market run. Returns sharpe, return, drawdown, trades.
- find_best(metric, min_trades): recall the historically best experiment on a metric.
- list_experiments(limit, tag?, triggered_by?): browse recent experiments.
- get_experiment(experiment_id): fetch full record.
- compare_experiments(experiment_ids): per-metric winners across 2-8 experiments.
- sweep_param(preset, param, values, hypothesis): narrow parameter sweep.
- list_presets: enumerate available presets.

RULES OF USE:
1. Use tools sparingly. A confidently-held prior doesn't need backtest evidence; \
reserve tool calls for genuinely uncertain judgments where evidence would change \
your recommendation.
2. ALWAYS pass a specific hypothesis ("does RSI<25 entry outperform default in \
RANGING regime?"). Vague hypotheses get audited and rejected.
3. Per-agent quota: 10 backtest runs/day; exceeding returns an error — fall back \
to engine-only reasoning in that case.
4. After running tools, produce the SAME JSON output format as without tools. \
Tools inform your reasoning; the output contract doesn't change."""

PORTFOLIO_STRATEGIST_PROMPT = """You are HYDRA's Portfolio Strategist, reviewing the aggregate state of a 3-pair crypto trading portfolio (SOL/USDC, SOL/BTC, BTC/USDC).

Your job: Produce a brief portfolio-level assessment that per-pair trading agents will use as advisory context. You are NOT making trade decisions — you are providing strategic guidance.

Assess:
1. Overall risk posture: should the portfolio lean AGGRESSIVE (capitalize on winners), NEUTRAL, or DEFENSIVE (protect capital)?
2. Which pairs are contributing vs bleeding, and whether exposure should shift
3. Whether the portfolio has dangerous concentration or correlation risk
4. Any specific warnings or opportunities visible only at the portfolio level

Be concise (4-6 sentences max). Focus on actionable insight, not recitation of numbers.
Do NOT output JSON. Output plain text only."""


# ═══════════════════════════════════════════════════════════════
# HYDRA BRAIN
# ═══════════════════════════════════════════════════════════════

# Cost per million tokens: (input, output)
COST_ANTHROPIC = (3.0, 15.0)
COST_OPENAI = (2.0, 8.0)
COST_XAI = (2.0, 6.0)


class HydraBrain:
    """3-agent AI reasoning: Claude Analyst + Claude Risk Manager + Grok Strategist.
    Grok only fires on genuine disagreements (OVERRIDE or analyst disagrees at low conviction)."""

    # Cross-component disclosure threshold. When daily_cost first crosses
    # this, the brain emits a `cost_alert` WS message and a log line — once
    # per UTC day. Decoupled from max_daily_cost: a caller can disable budget
    # enforcement (enforce_budget=False, e.g., backtest context) and the
    # user still gets disclosure.
    COST_ALERT_USD = 10.0

    def __init__(
        self,
        anthropic_key: str = "",
        openai_key: str = "",
        xai_key: str = "",
        max_daily_cost: float = 10.0,
        tool_dispatcher: Optional[Any] = None,
        enable_tool_use: Optional[bool] = None,
        enforce_budget: bool = True,
        broadcaster: Optional[Any] = None,
        tool_iterations_cap: int = 4,
    ):
        # Primary: Claude for Analyst + Risk Manager
        self.primary_client = None
        self.primary_provider = None
        self.primary_model = None

        if anthropic_key and HAS_ANTHROPIC:
            self.primary_client = anthropic.Anthropic(api_key=anthropic_key)
            self.primary_provider = "anthropic"
            self.primary_model = "claude-sonnet-4-6"
        elif openai_key and HAS_OPENAI:
            # OpenAI: same SDK as xAI but default base_url + gpt-4.1
            self.primary_client = openai.OpenAI(api_key=openai_key)
            self.primary_provider = "openai"
            self.primary_model = "gpt-4.1"
        elif xai_key and HAS_OPENAI:
            # Fallback: use xAI for primary if no Anthropic or OpenAI key
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
            raise ValueError("No AI provider available — need ANTHROPIC_API_KEY, OPENAI_API_KEY, or XAI_API_KEY")

        # External consumers (e.g., dashboard banner) may reference these
        self.model = self.primary_model
        self.provider = self.primary_provider
        self.max_daily_cost = max_daily_cost
        self.enforce_budget = enforce_budget
        self.broadcaster = broadcaster
        self._cost_alert_fired_date = None  # UTC date of last alert fire

        # Cost tracking
        cost_map = {"anthropic": COST_ANTHROPIC, "openai": COST_OPENAI, "xai": COST_XAI}
        primary_cost = cost_map[self.primary_provider]
        self.INPUT_COST_PER_M = primary_cost[0]
        self.OUTPUT_COST_PER_M = primary_cost[1]

        # State
        self.decision_history: Dict[str, List[Dict]] = {}
        self.daily_tokens_in = 0         # Primary provider (analyst + risk)
        self.daily_tokens_out = 0
        self._daily_strategist_tokens_in = 0   # Strategist (xAI) — tracked separately for accurate costing
        self._daily_strategist_tokens_out = 0
        self.daily_decisions = 0
        self.daily_overrides = 0
        self.daily_escalations = 0
        self.daily_portfolio_reviews = 0
        self._daily_portfolio_tokens_in = 0
        self._daily_portfolio_tokens_out = 0
        self.daily_reset_date = datetime.now(timezone.utc).date()
        self.tick_counter = 0
        self.consecutive_failures = 0
        self.api_available = True
        self.retry_at_tick = 0
        self.last_decision: Optional[BrainDecision] = None
        self._lock = threading.Lock()  # Thread safety for parallel brain calls

        # Per-pair strategist cooldown: suppress re-escalation for N deliberate()
        # calls after Grok fires.  With 3 pairs, tick_counter increments ~3×
        # per agent tick, so 9 = ~3 agent ticks (~1 candle period at 15-min candles).
        self.strategist_cooldowns: Dict[str, int] = {}
        self.strategist_cooldown_ticks = 9

        # ─── Tool-use (Phase 5) ───
        # Gated three ways so the default path is IDENTICAL to v2.9.x:
        #   1) caller passed a dispatcher instance
        #   2) enable_tool_use=True OR env HYDRA_BRAIN_TOOLS_ENABLED=1
        #   3) primary provider is anthropic (tool-use format is Anthropic-specific)
        # If any condition fails, self._tool_use_enabled is False and all
        # existing call sites take the legacy _call_llm path.
        self._tool_dispatcher = tool_dispatcher
        flag_env = os.getenv("HYDRA_BRAIN_TOOLS_ENABLED") == "1"
        flag_param = bool(enable_tool_use) if enable_tool_use is not None else flag_env
        self._tool_use_enabled = (
            tool_dispatcher is not None
            and flag_param
            and self.primary_provider == "anthropic"
            and HAS_ANTHROPIC
        )
        self._tool_iterations_cap = max(1, int(tool_iterations_cap))
        self._tool_use_calls = 0              # lifetime counter for diagnostics

    # ─── Main Entry Point ───

    def deliberate(self, state: Dict[str, Any]) -> BrainDecision:
        """Evaluate engine signal with 3-agent pipeline. Thread-safe."""
        # Pre-flight: shared state checks under lock
        with self._lock:
            self.tick_counter += 1
            self._maybe_reset_daily()

            if not self.api_available:
                if self.tick_counter >= self.retry_at_tick:
                    self.api_available = True
                else:
                    return self._fallback(state)

            # Budget cap only applies when enforce_budget=True. Backtest
            # brains run with enforce_budget=False so experiments never
            # stall behind a live-cost ceiling — disclosure is handled via
            # the $10 cost_alert broadcast instead.
            if self.enforce_budget and self._estimated_cost() >= self.max_daily_cost:
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

            # Agent 3: Strategic Advisor (Grok) — only on genuine disagreements
            strategist_output = None
            escalated = False
            pair = state.get("asset", "")
            cooldown_active = self.strategist_cooldowns.get(pair, 0) > self.tick_counter
            risk_decision = risk_output.get("decision", "CONFIRM")
            # Default signal_agreement to False (not True) so a malformed analyst
            # response that omits the key doesn't silently bypass escalation.
            analyst_agrees = analyst_output.get("signal_agreement", False)
            analyst_conviction = analyst_output.get("conviction", 0.0)
            needs_strategist = (
                self.has_strategist and not cooldown_active and (
                    risk_decision == "OVERRIDE" or
                    (not analyst_agrees and analyst_conviction < 0.50)
                )
            )
            if cooldown_active and self.has_strategist:
                would_escalate = (
                    risk_decision == "OVERRIDE" or
                    (not analyst_agrees and analyst_conviction < 0.50)
                )
                if would_escalate:
                    remaining = self.strategist_cooldowns[pair] - self.tick_counter
                    print(f"  [BRAIN] Strategist cooldown active for {pair} ({remaining} ticks remaining)")

            strategist_tokens_in = 0
            strategist_tokens_out = 0
            if needs_strategist:
                try:
                    strategist_output, s_in, s_out = self._run_strategist(state, analyst_output, risk_output)
                    strategist_tokens_in = s_in
                    strategist_tokens_out = s_out
                    escalated = True
                    # Set per-pair cooldown after Grok fires
                    self.strategist_cooldowns[pair] = self.tick_counter + self.strategist_cooldown_ticks
                except Exception as e:
                    print(f"  [BRAIN] Strategist failed (continuing with Risk Manager decision): {e}")

            # Build final decision — strategist overrides risk manager when present
            if needs_strategist and not strategist_output:
                print(f"  [BRAIN] Strategist returned no usable output — falling back to Risk Manager")
            if strategist_output:
                # Grok arbitrates the contested point only (action + decision).
                # Conviction stays from analyst; sizing stays from risk manager.
                final_action = strategist_output.get("final_action", risk_output.get("final_action", state["signal"]["action"]))
                final_decision = strategist_output.get("decision", risk_output.get("decision", "CONFIRM"))
                final_conviction = analyst_output.get("conviction", state["signal"]["confidence"])
                final_size = risk_output.get("size_multiplier", 1.0)
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
                self._maybe_fire_cost_alert()

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

        if provider in ("xai", "openai"):
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                timeout=30.0,
            )
            choice = response.choices[0] if response.choices else None
            text = choice.message.content if choice else ""
            # Parity with the Anthropic branch: surface truncation so the
            # subsequent JSON parse failure has a diagnosable root cause.
            if choice and getattr(choice, "finish_reason", None) == "length":
                print(f"  [BRAIN] Response truncated (max_tokens={max_tokens}, provider={provider}) — JSON parse likely to fail")
            usage = response.usage
            return text, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0
        else:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                timeout=30.0,
            )
            text = response.content[0].text if response.content and hasattr(response.content[0], "text") else ""
            # Detect truncated responses — if the model hit max_tokens or was
            # cut off, the JSON will be incomplete and unparseable.
            if response.stop_reason == "max_tokens":
                print(f"  [BRAIN] Response truncated (max_tokens={max_tokens}) — increasing tolerance")
            return text, response.usage.input_tokens, response.usage.output_tokens

    # ─── Tool-use loop (Phase 5) ───

    @staticmethod
    def _content_block_to_dict(block: Any) -> Dict[str, Any]:
        """Normalize an Anthropic ContentBlock (or a pre-dicted block) into a
        plain dict safe to echo back in a subsequent messages.create call.

        Handles both SDK objects (duck-typed via attributes) and dicts (used
        by tests with mock clients).
        """
        if isinstance(block, dict):
            return block
        btype = getattr(block, "type", None)
        if btype == "text":
            return {"type": "text", "text": getattr(block, "text", "")}
        if btype == "tool_use":
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}) or {},
            }
        # Unknown block type — emit a marker so it shows up in logs but
        # doesn't break messages=[...] serialization.
        return {"type": "text", "text": f"[unknown block type: {btype}]"}

    def _call_llm_with_tools(
        self,
        system_prompt: str,
        user_msg: str,
        tools: List[Dict[str, Any]],
        caller: str,
        max_tokens: int = 400,
    ) -> Tuple[str, int, int, int]:
        """Anthropic tool-use loop for the brain's agents.

        Runs up to `self._tool_iterations_cap` iterations. Each iteration:
          1) call messages.create(tools=tools)
          2) accumulate tokens
          3) if stop_reason == "end_turn" → return joined text blocks
          4) if stop_reason == "tool_use" → dispatch every tool_use block,
             append tool_result blocks, continue loop
          5) anything else → bail with whatever text we have (fail-safe)

        Returns: (final_text, input_tokens, output_tokens, tool_calls_made)

        Invariants:
          - Never raises. On unexpected exceptions returns ("", 0, 0, 0)
            so the caller falls back to engine-only reasoning (same
            pattern as _call_llm's outer try in _run_analyst).
          - Primary provider MUST be "anthropic" — caller is responsible
            for checking self._tool_use_enabled before calling.
          - Dispatcher errors are marshaled into tool_result content
            (as JSON) so the LLM can read + recover, not raised.
        """
        if self.primary_provider != "anthropic":
            # Defensive — should be gated by _tool_use_enabled, but protect
            # against a future caller that forgets.
            return "", 0, 0, 0

        messages: List[Dict[str, Any]] = [{"role": "user", "content": user_msg}]
        total_in = 0
        total_out = 0
        tool_calls = 0

        try:
            for _iter in range(self._tool_iterations_cap):
                response = self.primary_client.messages.create(
                    model=self.primary_model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                    timeout=45.0,  # tool-use can take longer than plain calls
                )
                usage = getattr(response, "usage", None)
                if usage is not None:
                    total_in += getattr(usage, "input_tokens", 0) or 0
                    total_out += getattr(usage, "output_tokens", 0) or 0

                stop = getattr(response, "stop_reason", None)
                content = getattr(response, "content", []) or []

                if stop != "tool_use":
                    # Terminal (end_turn, max_tokens, stop_sequence, or None).
                    # Concatenate all text blocks; JSON parser tolerates
                    # leading/trailing prose from the model.
                    texts = [
                        getattr(b, "text", "") if not isinstance(b, dict)
                        else b.get("text", "")
                        for b in content
                        if (getattr(b, "type", None) == "text"
                            or (isinstance(b, dict) and b.get("type") == "text"))
                    ]
                    # Edge case: the model stopped with max_tokens while still
                    # emitting tool_use blocks. We can't process those (no loop
                    # iteration left with a response to consume), but silently
                    # dropping them masks a real signal. Log, then proceed
                    # with whatever text we have.
                    if stop == "max_tokens":
                        pending_tool_uses = sum(
                            1 for b in content
                            if (getattr(b, "type", None) == "tool_use"
                                or (isinstance(b, dict) and b.get("type") == "tool_use"))
                        )
                        if pending_tool_uses:
                            print(f"  [BRAIN] max_tokens reached with "
                                  f"{pending_tool_uses} pending tool_use block(s) "
                                  f"for {caller}; treating as terminal")
                    return "\n".join(t for t in texts if t), total_in, total_out, tool_calls

                # Tool-use turn: echo the assistant response back + append tool_results
                assistant_content = [self._content_block_to_dict(b) for b in content]
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results: List[Dict[str, Any]] = []
                for block in content:
                    btype = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
                    if btype != "tool_use":
                        continue
                    if self._tool_dispatcher is None:
                        tool_name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else "?")
                        tool_id = getattr(block, "id", None) or (block.get("id") if isinstance(block, dict) else "")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps({"success": False,
                                                    "error": "no dispatcher wired"}),
                            "is_error": True,
                        })
                        continue
                    tool_name = getattr(block, "name", None) or block.get("name")
                    tool_input = getattr(block, "input", None) if not isinstance(block, dict) else block.get("input")
                    tool_id = getattr(block, "id", None) or block.get("id")
                    result = self._tool_dispatcher.execute(
                        tool_name, tool_input or {}, caller=caller
                    )
                    self._tool_use_calls += 1
                    tool_calls += 1
                    # 8 KB cap protects against a runaway tool_result
                    # flooding the model's next context. Naive slicing can
                    # split JSON mid-object, leaving the LLM parsing junk;
                    # on overflow, emit a clean JSON envelope with a
                    # truncated=True flag so the model recognizes the cut.
                    raw = json.dumps(result)
                    if len(raw) > 8000:
                        content_str = json.dumps({
                            "success": bool(result.get("success", False)),
                            "truncated": True,
                            "original_bytes": len(raw),
                            "notice": ("tool result exceeded 8 KB cap; "
                                       "full payload persisted server-side, "
                                       "summary returned here"),
                            "summary": raw[:6000],
                        })
                    else:
                        content_str = raw
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": content_str,
                        "is_error": not result.get("success", False),
                    })

                messages.append({"role": "user", "content": tool_results})

            # Ran out of iterations. Budget-exceeded fail-safe: return empty
            # so deliberate() falls back to engine-only. Print once so the
            # ops observer can see it in hydra_errors.log via tick wrap.
            print(f"  [BRAIN] tool-use hit iteration cap ({self._tool_iterations_cap}) for {caller}; engine fallback")
            return "", total_in, total_out, tool_calls

        except Exception as e:
            print(f"  [BRAIN] tool-use exception ({type(e).__name__}: {e}) for {caller}; engine fallback")
            # Don't swallow the traceback in debug envs
            if os.getenv("HYDRA_DEBUG_TOOLS") == "1":
                print(traceback.format_exc())
            return "", total_in, total_out, tool_calls

    # ─── Agent runners ───

    def _run_analyst(self, state: Dict) -> tuple:
        """Market Analyst (Claude). Returns (parsed_output, in_tokens, out_tokens)."""
        user_msg = self._build_analyst_prompt(state)
        if self._tool_use_enabled:
            from hydra_backtest_tool import BACKTEST_TOOLS
            text, tok_in, tok_out, _tool_calls = self._call_llm_with_tools(
                ANALYST_PROMPT + TOOLS_GUIDANCE,
                user_msg,
                BACKTEST_TOOLS,
                caller="brain:analyst",
                max_tokens=500,  # headroom over plain 400 for tool_result context
            )
        else:
            text, tok_in, tok_out = self._call_llm(ANALYST_PROMPT, user_msg, 400)
        return self._parse_json(text), tok_in, tok_out

    def _run_risk_manager(self, state: Dict, analyst: Dict) -> tuple:
        """Risk Manager (Claude). Returns (parsed_output, in_tokens, out_tokens)."""
        user_msg = self._build_risk_prompt(state, analyst)
        if self._tool_use_enabled:
            from hydra_backtest_tool import BACKTEST_TOOLS
            text, tok_in, tok_out, _tool_calls = self._call_llm_with_tools(
                RISK_MANAGER_PROMPT + TOOLS_GUIDANCE,
                user_msg,
                BACKTEST_TOOLS,
                caller="brain:risk_manager",
                max_tokens=450,
            )
        else:
            text, tok_in, tok_out = self._call_llm(RISK_MANAGER_PROMPT, user_msg, 350)
        parsed = self._parse_json(text)
        # Defensive clamp: the RISK_MANAGER_PROMPT documents size_multiplier in
        # [0.0, 1.5] but a model hallucination can return out-of-range or
        # non-numeric values. Downstream Kelly sizing multiplies balance by this
        # directly, so an unclamped 2.5 would oversize by 67%.
        if isinstance(parsed, dict) and "size_multiplier" in parsed:
            raw = parsed["size_multiplier"]
            try:
                clamped = max(0.0, min(1.5, float(raw)))
            except (TypeError, ValueError):
                clamped = 1.0
            if clamped != raw:
                print(f"  [BRAIN] size_multiplier clamped: {raw!r} -> {clamped}")
            parsed["size_multiplier"] = clamped
        return parsed, tok_in, tok_out

    def _run_strategist(self, state: Dict, analyst: Dict, risk: Dict) -> tuple:
        """Strategic Advisor (Grok 4 Reasoning). Only called on contested decisions."""
        user_msg = self._build_strategist_prompt(state, analyst, risk)
        text, tok_in, tok_out = self._call_llm(
            STRATEGIST_PROMPT, user_msg, 350,
            client=self.strategist_client, provider="xai", model=self.strategist_model,
        )
        return self._parse_json(text), tok_in, tok_out

    def run_portfolio_review(self, portfolio_state: Dict) -> Optional[str]:
        """Portfolio Strategist (Grok). Periodic portfolio-level assessment.
        Returns plain text guidance or None on failure."""
        if not self.has_strategist:
            return None
        with self._lock:
            if self._estimated_cost() >= self.max_daily_cost:
                return None
        try:
            user_msg = self._build_portfolio_review_prompt(portfolio_state)
            text, tok_in, tok_out = self._call_llm(
                PORTFOLIO_STRATEGIST_PROMPT, user_msg, 400,
                client=self.strategist_client, provider="xai", model=self.strategist_model,
            )
            with self._lock:
                self._daily_portfolio_tokens_in += tok_in
                self._daily_portfolio_tokens_out += tok_out
                self.daily_portfolio_reviews += 1
                self._maybe_fire_cost_alert()
            return text.strip() if text else None
        except Exception as e:
            print(f"  [BRAIN] Portfolio review failed (degrading gracefully): {e}")
            return None

    @staticmethod
    def _build_portfolio_review_prompt(ps: Dict) -> str:
        """Build user message for portfolio strategist review."""
        pair_lines = []
        for p in ps.get("pair_details", []):
            pair_lines.append(
                f"  {p['pair']}: regime={p['regime']} | signal={p['signal']}({p['confidence']:.2f}) | "
                f"pos={p['position']:.6f} | P&L=${p['pnl_usd']:+.2f} | DD={p['drawdown']:.1f}% | "
                f"W/L={p['wins']}/{p['losses']}"
            )
        recent = ps.get("recent_trades", [])
        trade_lines = []
        for t in recent[-12:]:
            line = f"  {t.get('time', '?')} {t['pair']} {t['side']} @{t['price']:.4f}"
            trade_lines.append(line)

        return (
            f"PORTFOLIO STATE:\n"
            f"Total equity: ${ps.get('total_equity_usd', 0):.2f} | "
            f"P&L: ${ps.get('total_pnl_usd', 0):+.2f} ({ps.get('total_pnl_pct', 0):+.2f}%)\n"
            f"Aggregate win rate: {ps.get('agg_win_rate_pct', 0):.0f}% across {ps.get('agg_trades', 0)} trades\n"
            f"Net USD exposure: ${ps.get('net_exposure_usd', 0):.2f}\n"
            f"Worst pair drawdown: {ps.get('worst_drawdown_pct', 0):.1f}%\n\n"
            f"PER-PAIR BREAKDOWN:\n" + "\n".join(pair_lines) + "\n\n"
            f"RECENT TRADES (all pairs, chronological):\n"
            + ("\n".join(trade_lines) if trade_lines else "  No trades yet") + "\n\n"
            f"Provide your portfolio assessment."
        )

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
        lines += f"\n  Net SOL exposure: {net_exp.get('SOL', 0):.4f} | Net BTC exposure: {net_exp.get('BTC', 0):.4f}"
        return lines

    @staticmethod
    def _format_portfolio_summary(state: Dict) -> str:
        """Format aggregate portfolio stats for prompt inclusion."""
        ps = state.get("portfolio_summary")
        if not ps:
            return ""
        pair_pnl = " | ".join(f"{p}: ${v:+.2f}" for p, v in ps.get("per_pair_pnl_usd", {}).items())
        recent = ps.get("recent_trades", [])
        trade_lines = ""
        if recent:
            trade_lines = "\n  Recent trades: " + " | ".join(
                f"{t['pair']} {t['side']}" for t in recent[-6:]
            )
        return (
            f"\nPORTFOLIO OVERVIEW (all 3 pairs):"
            f"\n  Total equity: ${ps['total_equity_usd']:.2f} | P&L: ${ps['total_pnl_usd']:+.2f} ({ps['total_pnl_pct']:+.2f}%)"
            f"\n  Win rate: {ps['agg_win_rate_pct']:.0f}% across {ps['agg_trades']} trades | Worst DD: {ps['worst_drawdown_pct']:.1f}%"
            f"\n  Net USD exposure: ${ps['net_exposure_usd']:.2f}"
            f"\n  Per-pair P&L: {pair_pnl}"
            f"{trade_lines}"
        )

    @staticmethod
    def _format_portfolio_guidance(state: Dict) -> str:
        """Format portfolio strategist guidance for prompt inclusion."""
        guidance = state.get("portfolio_guidance")
        if not guidance:
            return ""
        return f"\nPORTFOLIO STRATEGIST GUIDANCE (advisory):\n  {guidance}"

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
RECENT AI DECISIONS: {recent or 'None yet'}{self._format_spread(state)}{self._format_triangle_context(state)}{self._format_portfolio_summary(state)}{self._format_portfolio_guidance(state)}"""

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
PERFORMANCE: {perf.get('total_trades', 0)} trades | Win Rate: {perf.get('win_rate_pct', 0):.0f}% | Sharpe: {perf.get('sharpe_estimate', 0):.2f}{self._format_spread(state)}{self._format_triangle_context(state)}{self._format_portfolio_summary(state)}{self._format_portfolio_guidance(state)}"""

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
PORTFOLIO: Equity=${port.get('equity', 0):.2f} | P&L={port.get('pnl_pct', 0):.2f}% | Max DD={port.get('max_drawdown_pct', 0):.2f}%{self._format_spread(state)}{self._format_triangle_context(state)}{self._format_portfolio_summary(state)}{self._format_portfolio_guidance(state)}

Make the final call. Think carefully, then respond with JSON only."""

    # ─── Helpers ───

    def _parse_json(self, text: str) -> Optional[Dict]:
        """Lenient JSON parser — finds JSON in response text."""
        if not text:
            return None
        cleaned = text.strip()
        # Strip markdown code fences (```json ... ``` or ``` ... ```)
        fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
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
        # Split on ". " (period + whitespace) so decimal numbers like
        # "RSI at 30.5" don't truncate mid-number. Falls back to the full
        # string when no sentence boundary is found.
        match = re.search(r'\.\s', reasoning)
        if match:
            first_sentence = reasoning[:match.end()].rstrip()
        elif reasoning:
            first_sentence = reasoning if reasoning.endswith(".") else reasoning + "."
        else:
            first_sentence = ""
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
        portfolio_cost = (self._daily_portfolio_tokens_in / 1_000_000 * COST_XAI[0] +
                          self._daily_portfolio_tokens_out / 1_000_000 * COST_XAI[1])
        return primary_cost + strategist_cost + portfolio_cost

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
            self.daily_portfolio_reviews = 0
            self._daily_portfolio_tokens_in = 0
            self._daily_portfolio_tokens_out = 0
            self.daily_reset_date = today
            self._cost_alert_fired_date = None   # re-arm $10 alert

    def _maybe_fire_cost_alert(self):
        """Emit one-shot disclosure when brain+strategist daily cost crosses
        COST_ALERT_USD. Called under self._lock by token-accounting paths.

        Independent of enforce_budget — the user gets told regardless of
        whether we're also capping. Fires at most once per UTC day.
        """
        cost = self._estimated_cost()
        today = self.daily_reset_date
        if cost < self.COST_ALERT_USD:
            return
        if self._cost_alert_fired_date == today:
            return
        self._cost_alert_fired_date = today
        msg = (f"[BRAIN] daily cost ${cost:.2f} has crossed the "
               f"${self.COST_ALERT_USD:.2f}/day disclosure threshold "
               f"(enforce_budget={self.enforce_budget})")
        try:
            print(msg, flush=True)
        except Exception:
            pass
        if self.broadcaster is not None and hasattr(self.broadcaster, "broadcast_message"):
            try:
                self.broadcaster.broadcast_message("cost_alert", {
                    "component": "brain",
                    "daily_cost_usd": round(cost, 4),
                    "threshold_usd": self.COST_ALERT_USD,
                    "day_key": today.isoformat() if today else "",
                    "enforce_budget": self.enforce_budget,
                })
            except Exception:
                pass

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
            "portfolio_reviews_today": self.daily_portfolio_reviews,
            "tokens_today": (self.daily_tokens_in + self.daily_tokens_out +
                            self._daily_strategist_tokens_in + self._daily_strategist_tokens_out +
                            self._daily_portfolio_tokens_in + self._daily_portfolio_tokens_out),
            "avg_latency_ms": round(
                self.last_decision.latency_ms if self.last_decision and not self.last_decision.fallback else 0, 0
            ),
            "model": self.primary_model,
            "strategist_model": self.strategist_model if self.has_strategist else None,
            "consecutive_failures": self.consecutive_failures,
        }
