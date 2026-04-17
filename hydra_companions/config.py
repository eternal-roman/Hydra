"""Companion subsystem configuration + env-flag gating.

All flags are opt-in (default OFF) so the subsystem has zero effect on
v2.10.x behaviour until a user sets HYDRA_COMPANION_ENABLED=1.

Flag composition (each phase gates on the previous):
    HYDRA_COMPANION_ENABLED=1            -> Phase 1 chat available
    HYDRA_COMPANION_PROPOSALS_ENABLED=1  -> Phase 2+ (trade cards render)
    HYDRA_COMPANION_LIVE_EXECUTION=1     -> Phase 3+ (real orders placed)
    HYDRA_COMPANION_NUDGES=1             -> Phase 6+ (proactive messages)

    HYDRA_COMPANION_DISABLED=1           -> master kill switch (wins over all)
"""
from __future__ import annotations
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOULS_DIR = ROOT / "souls"
ROUTING_CONFIG = ROOT / "model_routing.json"
RUNTIME_DIR = Path.cwd() / ".hydra-companions"
TRANSCRIPTS_DIR = RUNTIME_DIR / "transcripts"
MEMORY_DIR = RUNTIME_DIR / "memory"
PROPOSALS_LOG = RUNTIME_DIR / "proposals.jsonl"
ROUTING_LOG = RUNTIME_DIR / "routing.jsonl"
COSTS_LOG = RUNTIME_DIR / "costs.jsonl"

DEFAULT_USER_ID = "local"
DEFAULT_COMPANION_ID = "apex"
COMPANION_IDS = ("athena", "apex", "broski")


def is_disabled() -> bool:
    return os.environ.get("HYDRA_COMPANION_DISABLED") == "1"


def is_enabled() -> bool:
    """Phase 1 chat gate. Off unless user opts in AND no kill switch."""
    if is_disabled():
        return False
    return os.environ.get("HYDRA_COMPANION_ENABLED") == "1"


def proposals_enabled() -> bool:
    return is_enabled() and os.environ.get("HYDRA_COMPANION_PROPOSALS_ENABLED") == "1"


def live_execution_enabled() -> bool:
    return proposals_enabled() and os.environ.get("HYDRA_COMPANION_LIVE_EXECUTION") == "1"


def nudges_enabled() -> bool:
    return is_enabled() and os.environ.get("HYDRA_COMPANION_NUDGES") == "1"


def routing_mode() -> str:
    return os.environ.get("HYDRA_COMPANION_ROUTING_MODE", "balanced")


def ensure_runtime_dirs() -> None:
    """Idempotent. Called once at coordinator startup."""
    RUNTIME_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    MEMORY_DIR.mkdir(exist_ok=True)
