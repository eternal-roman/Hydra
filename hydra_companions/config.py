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


# Defensive: if the parent agent hasn't loaded .env yet, do it here so
# companions can reach ANTHROPIC_API_KEY / XAI_API_KEY on standalone
# imports (tests, repl, etc.). Idempotent \u2014 won't overwrite
# anything already in os.environ.
def _load_env_once():
    root = Path(__file__).resolve().parent.parent
    env = root / ".env"
    if not env.exists():
        return
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass


_load_env_once()


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
    """Phase 1 chat gate. On by default \u2014 kill switch to disable.

    The subsystem is cheap to mount (no API calls until the user
    actually talks to a companion). Making it default-on means the
    orb is visible immediately on launch; clicking it IS the
    activation. Set HYDRA_COMPANION_DISABLED=1 to turn it off.
    """
    return not is_disabled()


def proposals_enabled() -> bool:
    """Phase 2 trade-card gate. Default ON once chat is on."""
    if not is_enabled():
        return False
    # Explicit opt-out via env wins.
    if os.environ.get("HYDRA_COMPANION_PROPOSALS_ENABLED") == "0":
        return False
    return True


def live_execution_enabled() -> bool:
    """Phase 3 real-order gate. Default OFF \u2014 opt-in via env or in-app
    settings when we wire runtime settings persistence. This is the
    only flag that *stays* explicit-opt-in by default, because it
    places real money at risk."""
    if not proposals_enabled():
        return False
    return os.environ.get("HYDRA_COMPANION_LIVE_EXECUTION") == "1"


def nudges_enabled() -> bool:
    """Phase 6 proactive nudge gate. Default ON once chat is on \u2014
    opt-out via env if the user finds them noisy."""
    if not is_enabled():
        return False
    if os.environ.get("HYDRA_COMPANION_NUDGES") == "0":
        return False
    return True


def routing_mode() -> str:
    return os.environ.get("HYDRA_COMPANION_ROUTING_MODE", "balanced")


def ensure_runtime_dirs() -> None:
    """Idempotent. Called once at coordinator startup."""
    RUNTIME_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    MEMORY_DIR.mkdir(exist_ok=True)
