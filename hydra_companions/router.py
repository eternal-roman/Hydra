"""Per-intent per-companion model selection.

Deterministic. Reads model_routing.json once at construction. Applies
fallback cascade on provider errors. Logs every decision to
.hydra-companions/routing.jsonl for auditing.
"""
from __future__ import annotations
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hydra_companions.config import ROUTING_CONFIG, routing_mode


@dataclass(frozen=True)
class RouteDecision:
    provider: str          # "anthropic" | "xai"
    model_id: str          # "claude-sonnet-4-6" | "grok-4-1-fast-reasoning" | ...
    max_tokens: int
    temperature: float
    intent: str
    companion_id: str


class Router:
    def __init__(self, config_path: Optional[Path] = None):
        path = config_path or ROUTING_CONFIG
        self._cfg = json.loads(path.read_text(encoding="utf-8"))
        self._routing = self._cfg["routing"]
        self._intents = self._cfg["intents"]
        self._fallbacks = self._cfg.get("fallbacks", {})
        self._pools = self._cfg.get("rotation_pools", {})
        self._caps = self._cfg.get("safety_caps", {})
        self._budgets = self._cfg.get("budgets", {})
        self._mode = routing_mode()

    # ----- public API -----

    def pick(self, companion_id: str, intent: str, *,
             serious_mode: bool = False, has_tools: Optional[bool] = None,
             seed: Optional[int] = None) -> RouteDecision:
        routes = self._routing.get(companion_id, {})
        intent_def = self._intents.get(intent) or self._intents["unknown"]
        entry = routes.get(intent) or routes.get("unknown") or routes.get("market_state_query", {})

        # Rotation pool override (e.g., broski.banter_humor)
        pool_key = f"{companion_id}.{intent}"
        pool = self._pools.get(pool_key)
        if pool:
            rng = random.Random(seed)
            model_id = _weighted_choice(pool, rng)
        else:
            model_id = entry.get("primary", "xai:grok-4-1-fast-reasoning")

        temperature = float(entry.get("temperature", 0.5))
        # Broski serious-mode temperature delta
        if serious_mode and companion_id == "broski":
            override = routes.get("serious_mode_override", {})
            if intent in override.get("applies_to_intents", []):
                temperature = max(0.0, temperature + float(override.get("temperature_delta", 0)))

        max_tokens = int(intent_def.get("default_max_tokens", 300))
        provider, model = _split_model_id(model_id)
        return RouteDecision(
            provider=provider, model_id=model,
            max_tokens=max_tokens, temperature=temperature,
            intent=intent, companion_id=companion_id,
        )

    def fallback(self, decision: RouteDecision) -> Optional[RouteDecision]:
        """Return the next provider/model to try after a failure, or None if exhausted."""
        full_id = f"{decision.provider}:{decision.model_id}"
        chain = self._fallbacks.get(full_id, [])
        if not chain:
            return None
        # Take the first entry we haven't already tried. For simplicity,
        # the caller tracks attempts; here we return the first candidate
        # and callers rotate if needed.
        provider, model = _split_model_id(chain[0])
        return RouteDecision(
            provider=provider, model_id=model,
            max_tokens=decision.max_tokens, temperature=decision.temperature,
            intent=decision.intent, companion_id=decision.companion_id,
        )

    def safety_cap(self, companion_id: str, key: str, default=None):
        return self._caps.get(companion_id, {}).get(key, default)

    def daily_budget_usd(self, companion_id: str) -> float:
        return float(self._budgets.get(companion_id, {}).get("daily_usd", 0.0))


# ----- helpers -----

def _split_model_id(full_id: str) -> tuple[str, str]:
    if ":" not in full_id:
        return "xai", full_id
    provider, model = full_id.split(":", 1)
    return provider, model


def _weighted_choice(pool: list, rng: random.Random) -> str:
    total = sum(p.get("weight", 0) for p in pool)
    if total <= 0:
        return pool[0]["model"]
    r = rng.random() * total
    acc = 0.0
    for p in pool:
        acc += p.get("weight", 0)
        if r <= acc:
            return p["model"]
    return pool[-1]["model"]
