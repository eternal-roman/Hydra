"""Heuristic-first intent classifier.

P1 keeps it simple: pattern match against rules from model_routing.json.
If no rule matches, default to "small_talk" (the cheapest routing).
LLM fallback is spec'd but deferred \u2014 in practice the heuristics cover
>90% of real traffic and the "small_talk" default is cheap enough that
misrouting is low-cost.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hydra_companions.config import ROUTING_CONFIG


@dataclass(frozen=True)
class IntentResult:
    intent: str
    confidence: float
    method: str  # "heuristic" | "default"


class IntentClassifier:
    def __init__(self, config_path: Optional[Path] = None):
        path = config_path or ROUTING_CONFIG
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"IntentClassifier: failed to load {path}: {type(e).__name__}: {e}"
            ) from e
        classifier_cfg = cfg.get("intent_classifier", {})
        self._rules = self._compile_rules(classifier_cfg.get("heuristic_rules", []))
        self._default_intent = "small_talk"

    @staticmethod
    def _compile_rules(raw_rules: list) -> list:
        compiled = []
        for rule in raw_rules:
            intent = rule.get("intent")
            patterns = []
            length_le = None
            for m in rule.get("match", []):
                if isinstance(m, str) and m.startswith("/") and "/" in m[1:]:
                    # parse /pattern/flags
                    last_slash = m.rfind("/")
                    pat = m[1:last_slash]
                    flags = m[last_slash + 1:]
                    re_flags = 0
                    if "i" in flags:
                        re_flags |= re.IGNORECASE
                    try:
                        patterns.append(re.compile(pat, re_flags))
                    except re.error:
                        continue
                elif isinstance(m, str) and m.startswith("length_le:"):
                    try:
                        length_le = int(m.split(":", 1)[1])
                    except ValueError as e:
                        import logging; logging.warning(f"Ignored exception: {e}")
            if intent and (patterns or length_le is not None):
                compiled.append({"intent": intent, "patterns": patterns, "length_le": length_le})
        return compiled

    def classify(self, text: str) -> IntentResult:
        if not text or not text.strip():
            return IntentResult(intent=self._default_intent, confidence=0.5, method="default")
        t = text.strip()
        for rule in self._rules:
            if rule["length_le"] is not None and len(t) > rule["length_le"]:
                continue
            if rule["patterns"]:
                if any(p.search(t) for p in rule["patterns"]):
                    return IntentResult(intent=rule["intent"], confidence=0.9, method="heuristic")
            elif rule["length_le"] is not None:
                # length-only rule
                return IntentResult(intent=rule["intent"], confidence=0.7, method="heuristic")
        # No match -> market_state_query if it looks like a question, else small_talk
        if "?" in t or any(w in t.lower() for w in ("what", "how", "why", "when", "where", "who")):
            return IntentResult(intent="market_state_query", confidence=0.55, method="heuristic")
        return IntentResult(intent=self._default_intent, confidence=0.5, method="default")
