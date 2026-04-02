#!/usr/bin/env python3
"""
HYDRA Brain — Multi-Agent AI Reasoning Layer

Two-agent system: Market Analyst + Risk Manager.
Analyst evaluates the engine signal, Risk Manager approves or overrides.
Uses Claude Sonnet via the Anthropic SDK. Falls back to engine-only on failure.

Usage:
    brain = HydraBrain(api_key="sk-ant-...")
    decision = brain.deliberate(engine_state)
    # decision.action: "CONFIRM", "ADJUST", "OVERRIDE"
    # decision.analyst_reasoning: "RSI oversold with MACD turning..."
    # decision.risk_reasoning: "Drawdown within limits, confirming..."
"""

import json
import time
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ═══════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════

@dataclass
class BrainDecision:
    action: str                    # "CONFIRM", "ADJUST", "OVERRIDE"
    final_signal: str              # "BUY", "SELL", "HOLD"
    confidence_adj: float          # adjusted confidence 0-1
    size_multiplier: float         # 0.0–1.5
    analyst_reasoning: str         # natural language thesis
    risk_reasoning: str            # natural language risk assessment
    combined_summary: str          # one-line for trade log
    risk_flags: List[str] = field(default_factory=list)
    portfolio_health: str = "HEALTHY"
    tokens_used: int = 0
    latency_ms: float = 0.0
    fallback: bool = False


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


# ═══════════════════════════════════════════════════════════════
# HYDRA BRAIN
# ═══════════════════════════════════════════════════════════════

class HydraBrain:
    """Two-agent AI reasoning layer for HYDRA trading decisions."""

    # Sonnet pricing per million tokens
    INPUT_COST_PER_M = 3.0
    OUTPUT_COST_PER_M = 15.0

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_daily_cost: float = 5.0,
        call_interval: int = 1,
    ):
        if not HAS_ANTHROPIC:
            raise ImportError("anthropic package not installed: pip install anthropic")

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_daily_cost = max_daily_cost
        self.call_interval = call_interval

        # State
        self.decision_history: List[Dict] = []
        self.daily_tokens_in = 0
        self.daily_tokens_out = 0
        self.daily_decisions = 0
        self.daily_overrides = 0
        self.daily_reset_date = datetime.now(timezone.utc).date()
        self.tick_counter = 0
        self.consecutive_failures = 0
        self.api_available = True
        self.retry_at_tick = 0
        self.last_decision: Optional[BrainDecision] = None

    # ─── Main Entry Point ───

    def deliberate(self, state: Dict[str, Any]) -> BrainDecision:
        """Evaluate engine signal with AI analyst + risk manager."""
        self.tick_counter += 1
        self._maybe_reset_daily()

        # Skip if not an AI tick (cost control)
        if self.call_interval > 1 and self.tick_counter % self.call_interval != 0:
            if self.last_decision and not self.last_decision.fallback:
                return self.last_decision
            return self._fallback(state)

        # Skip if API is down (with periodic retry)
        if not self.api_available:
            if self.tick_counter >= self.retry_at_tick:
                self.api_available = True  # retry this tick
            else:
                return self._fallback(state)

        # Skip if budget exceeded
        if self._estimated_cost() >= self.max_daily_cost:
            return self._fallback(state, reason="Daily budget exceeded")

        start = time.time()
        total_tokens_in = 0
        total_tokens_out = 0

        try:
            # Agent 1: Market Analyst
            analyst_output, a_in, a_out = self._run_analyst(state)
            total_tokens_in += a_in
            total_tokens_out += a_out

            if analyst_output is None:
                raise ValueError("Analyst returned no output")

            # Agent 2: Risk Manager
            risk_output, r_in, r_out = self._run_risk_manager(state, analyst_output)
            total_tokens_in += r_in
            total_tokens_out += r_out

            if risk_output is None:
                raise ValueError("Risk Manager returned no output")

            # Build decision
            decision = BrainDecision(
                action=risk_output.get("decision", "CONFIRM"),
                final_signal=risk_output.get("final_action", state["signal"]["action"]),
                confidence_adj=analyst_output.get("conviction", state["signal"]["confidence"]),
                size_multiplier=risk_output.get("size_multiplier", 1.0),
                analyst_reasoning=analyst_output.get("thesis", ""),
                risk_reasoning=risk_output.get("reasoning", ""),
                combined_summary=self._build_summary(analyst_output, risk_output),
                risk_flags=risk_output.get("risk_flags", []),
                portfolio_health=risk_output.get("portfolio_health", "HEALTHY"),
                tokens_used=total_tokens_in + total_tokens_out,
                latency_ms=(time.time() - start) * 1000,
                fallback=False,
            )

            # Track stats
            self.daily_tokens_in += total_tokens_in
            self.daily_tokens_out += total_tokens_out
            self.daily_decisions += 1
            if decision.action == "OVERRIDE":
                self.daily_overrides += 1
            self.consecutive_failures = 0
            self.last_decision = decision

            # Track for context in future calls
            self.decision_history.append({
                "tick": state.get("tick", 0),
                "action": decision.action,
                "signal": decision.final_signal,
                "conviction": decision.confidence_adj,
            })
            if len(self.decision_history) > 20:
                self.decision_history = self.decision_history[-20:]

            return decision

        except Exception as e:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self.api_available = False
                self.retry_at_tick = self.tick_counter + 60  # retry in ~30 min
                print(f"  [BRAIN] API disabled after {self.consecutive_failures} failures. Retry at tick {self.retry_at_tick}")
            return self._fallback(state, reason=str(e))

    # ─── Agent Calls ───

    def _run_analyst(self, state: Dict) -> tuple:
        """Call Market Analyst agent. Returns (parsed_output, input_tokens, output_tokens)."""
        user_msg = self._build_analyst_prompt(state)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            system=ANALYST_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout=10.0,
        )

        text = response.content[0].text if response.content and hasattr(response.content[0], "text") else ""
        parsed = self._parse_json(text)
        return parsed, response.usage.input_tokens, response.usage.output_tokens

    def _run_risk_manager(self, state: Dict, analyst: Dict) -> tuple:
        """Call Risk Manager agent. Returns (parsed_output, input_tokens, output_tokens)."""
        user_msg = self._build_risk_prompt(state, analyst)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=250,
            system=RISK_MANAGER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout=10.0,
        )

        text = response.content[0].text if response.content and hasattr(response.content[0], "text") else ""
        parsed = self._parse_json(text)
        return parsed, response.usage.input_tokens, response.usage.output_tokens

    # ─── Prompt Builders ───

    def _build_analyst_prompt(self, state: Dict) -> str:
        sig = state.get("signal", {})
        ind = state.get("indicators", {})
        pos = state.get("position", {})
        port = state.get("portfolio", {})
        candles = state.get("candles", [])

        # Last 10 closes for price action context
        recent_closes = [f"{c['c']:.4f}" for c in candles[-10:]] if candles else []

        # Recent AI decisions for context
        recent = ""
        if self.decision_history:
            recent = " | ".join(
                f"{d['action']} {d['signal']} ({d['conviction']:.0%})"
                for d in self.decision_history[-5:]
            )

        return f"""PAIR: {state.get('asset', '?')} | PRICE: {state.get('price', 0)} | REGIME: {state.get('regime', '?')} | STRATEGY: {state.get('strategy', '?')}
ENGINE SIGNAL: {sig.get('action', '?')} @ {sig.get('confidence', 0):.2f} confidence
REASON: {sig.get('reason', '')}

INDICATORS: RSI={ind.get('rsi', '?')} | MACD_HIST={ind.get('macd_histogram', '?')} | BB=[{ind.get('bb_lower', '?')}, {ind.get('bb_middle', '?')}, {ind.get('bb_upper', '?')}] | BB_WIDTH={ind.get('bb_width', 0):.4f}

RECENT CLOSES: {', '.join(recent_closes)}

POSITION: {pos.get('size', 0):.6f} @ avg {pos.get('avg_entry', 0)} | Unrealized: {pos.get('unrealized_pnl', 0)}
PORTFOLIO: Balance=${port.get('balance', 0):.2f} | Equity=${port.get('equity', 0):.2f} | P&L={port.get('pnl_pct', 0):.2f}% | Max DD={port.get('max_drawdown_pct', 0):.2f}%
RECENT AI DECISIONS: {recent or 'None yet'}"""

    def _build_risk_prompt(self, state: Dict, analyst: Dict) -> str:
        sig = state.get("signal", {})
        pos = state.get("position", {})
        port = state.get("portfolio", {})
        perf = state.get("performance", {})

        return f"""ENGINE SIGNAL: {sig.get('action', '?')} @ {sig.get('confidence', 0):.2f}
ANALYST THESIS: {analyst.get('thesis', 'N/A')}
ANALYST CONVICTION: {analyst.get('conviction', 0):.2f}
ANALYST AGREES WITH ENGINE: {analyst.get('signal_agreement', '?')}
ANALYST CONCERN: {analyst.get('concern', 'None')}

POSITION: {pos.get('size', 0):.6f} | Unrealized P&L: {pos.get('unrealized_pnl', 0)}
PORTFOLIO: Equity=${port.get('equity', 0):.2f} | P&L={port.get('pnl_pct', 0):.2f}% | Max DD={port.get('max_drawdown_pct', 0):.2f}%
PERFORMANCE: {perf.get('total_trades', 0)} trades | Win Rate: {perf.get('win_rate_pct', 0):.0f}% | Sharpe: {perf.get('sharpe_estimate', 0):.2f}"""

    # ─── Helpers ───

    def _parse_json(self, text: str) -> Optional[Dict]:
        """Lenient JSON parser — finds JSON in Claude's response."""
        if not text:
            return None
        # Try direct parse first
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        # Find JSON block in text (handles markdown wrapping)
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # Try finding nested JSON (with arrays)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"  [BRAIN] Failed to parse JSON: {text[:100]}")
        return None

    def _build_summary(self, analyst: Dict, risk: Dict) -> str:
        """One-line combined summary for trade log."""
        decision = risk.get("decision", "CONFIRM")
        action = risk.get("final_action", "HOLD")
        thesis = analyst.get("thesis", "")
        # Truncate thesis to first sentence
        first_sentence = thesis.split(".")[0] + "." if "." in thesis else thesis
        if len(first_sentence) > 80:
            first_sentence = first_sentence[:77] + "..."
        return f"{decision} {action}: {first_sentence}"

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
        """Estimate daily API cost from token usage."""
        return (self.daily_tokens_in / 1_000_000 * self.INPUT_COST_PER_M +
                self.daily_tokens_out / 1_000_000 * self.OUTPUT_COST_PER_M)

    def _maybe_reset_daily(self):
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).date()
        if today != self.daily_reset_date:
            self.daily_tokens_in = 0
            self.daily_tokens_out = 0
            self.daily_decisions = 0
            self.daily_overrides = 0
            self.daily_reset_date = today

    def get_stats(self) -> Dict:
        """Return brain statistics for dashboard."""
        return {
            "active": self.api_available,
            "decisions_today": self.daily_decisions,
            "overrides_today": self.daily_overrides,
            "cost_today": round(self._estimated_cost(), 4),
            "max_daily_cost": self.max_daily_cost,
            "tokens_today": self.daily_tokens_in + self.daily_tokens_out,
            "avg_latency_ms": round(
                self.last_decision.latency_ms if self.last_decision and not self.last_decision.fallback else 0, 0
            ),
            "model": self.model,
            "consecutive_failures": self.consecutive_failures,
        }
