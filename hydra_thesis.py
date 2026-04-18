#!/usr/bin/env python3
"""
HYDRA Thesis Layer — persistent worldview + user-authored intent

A slow-moving layer that sits *above* the per-tick engine and the stateless
3-agent brain. Carries the user's macro thesis (posture, posterior, checklist),
authored intent prompts, active multi-tick ladders, uploaded research
documents, and evidence log across ticks and restarts.

Phase A scope (this release): dataclasses + ThesisTracker load/save/snapshot,
hydra_thesis.json persistence with atomic writes, fail-soft defaults, knob
updates from dashboard. Brain integration, Grok document processing, ladder
placement, and posture enforcement all land in subsequent phases.

Kill switch: HYDRA_THESIS_DISABLED=1 — tracker returns a no-op default that
matches v2.12.5 behavior bit-for-bit.

Design philosophy (see feedback_hydra_design_philosophy.md in agent memory):
Hydra is the flywheel, not the shield. Thesis augments brain reasoning and
surfaces intent — it does not throttle trading. BLOCK is reserved for the
small set of hard rules (ledger shield, tax floor, no-altcoin).

Usage:
    from hydra_thesis import ThesisTracker
    thesis = ThesisTracker.load_or_default(save_dir="/path/to/hydra")
    snap = thesis.snapshot()              # for session snapshot embed
    thesis.restore(snap)                  # on --resume
    thesis.update_knobs({"conviction_floor_adjustment": 0.05})
    state = thesis.current_state()        # dict for WS broadcast
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


THESIS_SCHEMA_VERSION = "1.0.0"

STATE_FILENAME = "hydra_thesis.json"

# Subdirectories created lazily on first use. Gitignored per .gitignore.
DOCUMENTS_DIRNAME = "hydra_thesis_documents"
PROCESSED_DIRNAME = "hydra_thesis_processed"
PENDING_DIRNAME = "hydra_thesis_pending"
EVIDENCE_ARCHIVE_DIRNAME = "hydra_thesis_evidence_archive"

# Append-only log caps — older entries archived, in-memory bounded.
EVIDENCE_LOG_MAX_IN_MEMORY = 500

# Hard rules defaults — see docs/THESIS_SPEC.md for rationale.
DEFAULT_LEDGER_SHIELD_BTC = 0.20
DEFAULT_TAX_FRICTION_FLOOR_USD = 50.0

# Knob defaults — every one is user-adjustable via the Thesis tab.
DEFAULT_CONVICTION_FLOOR_ADJUSTMENT = 0.0
CONVICTION_FLOOR_ADJUSTMENT_RANGE = (-0.10, 0.15)
DEFAULT_SIZE_HINT_RANGE = (0.85, 1.15)
SIZE_HINT_HARD_BOUNDS = (0.50, 1.50)
DEFAULT_POSTURE_ENFORCEMENT = "advisory"  # off | advisory | binding
DEFAULT_MAX_ACTIVE_LADDERS_PER_PAIR = 3
DEFAULT_LADDER_EXPIRY_HOURS = 24
DEFAULT_LADDER_OFFSET_PCT = 0.003
DEFAULT_GROK_BUDGET_USD_PER_DAY = 5.0
DEFAULT_INTENT_PROMPT_MAX_ACTIVE = 5

# Near + far accumulation horizon per user's stated thesis.
DEFAULT_NEAR_DEADLINE_ISO = "2027-06-30"
DEFAULT_NEAR_BTC_TARGET = 1.0
DEFAULT_FAR_DEADLINE_ISO = "2030-12-31"
DEFAULT_FAR_BTC_TARGET = 10.0


# ═══════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════

class Posture(str, Enum):
    PRESERVATION = "PRESERVATION"
    TRANSITION = "TRANSITION"
    ACCUMULATION = "ACCUMULATION"


class MacroRegime(str, Enum):
    LATE_CYCLE_DIGESTION = "LATE_CYCLE_DIGESTION"
    STAGE_5_ONSET_IMMINENT = "STAGE_5_ONSET_IMMINENT"
    ACCUMULATION_PHASE = "ACCUMULATION_PHASE"
    UNKNOWN = "UNKNOWN"


class ChecklistItemStatus(str, Enum):
    NOT_MET = "NOT_MET"
    PARTIAL = "PARTIAL"
    MET = "MET"


class EvidenceCategory(str, Enum):
    MACRO = "MACRO"
    ON_CHAIN = "ON_CHAIN"
    STRUCTURAL = "STRUCTURAL"
    TACTICAL = "TACTICAL"


class DocumentType(str, Enum):
    COWEN_MEMO = "cowen_memo"
    FOMC_MINUTES = "fomc_minutes"
    RESEARCH_REPORT = "research_report"
    SCREENSHOT = "screenshot"
    USER_NOTE = "user_note"
    OTHER = "other"


class ProcessingStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class LadderStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    CONVERTED_TO_MARKET = "CONVERTED_TO_MARKET"
    STOPPED_OUT = "STOPPED_OUT"


class RungStatus(str, Enum):
    PENDING = "PENDING"
    PLACED = "PLACED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


# Default checklist items — the 5 ITC regime-change criteria.
DEFAULT_CHECKLIST_KEYS = (
    "advance_decline_broadening",
    "onchain_reset",
    "vol_reexpansion",
    "macro_liquidity_shift",
    "labor_feedback_loop",
)


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class Posterior:
    regime: str = MacroRegime.LATE_CYCLE_DIGESTION.value
    confidence: float = 0.50
    competing_hypotheses: List[Tuple[str, float]] = field(default_factory=list)


@dataclass
class ChecklistItem:
    key: str
    status: str = ChecklistItemStatus.NOT_MET.value
    evidence_refs: List[str] = field(default_factory=list)
    last_movement: Optional[str] = None
    notes: str = ""


@dataclass
class HardRules:
    ledger_shield_btc: float = DEFAULT_LEDGER_SHIELD_BTC
    no_altcoin_gate: bool = True
    tax_friction_min_realized_pnl_usd: float = DEFAULT_TAX_FRICTION_FLOOR_USD
    locked_kraken_btc_qty: float = 0.0


@dataclass
class ThesisKnobs:
    conviction_floor_adjustment: float = DEFAULT_CONVICTION_FLOOR_ADJUSTMENT
    size_hint_range: Tuple[float, float] = DEFAULT_SIZE_HINT_RANGE
    posture_enforcement: str = DEFAULT_POSTURE_ENFORCEMENT
    max_active_ladders_per_pair: int = DEFAULT_MAX_ACTIVE_LADDERS_PER_PAIR
    ladder_default_expiry_hours: int = DEFAULT_LADDER_EXPIRY_HOURS
    ladder_default_offset_pct: float = DEFAULT_LADDER_OFFSET_PCT
    auto_apply_proposed_updates: bool = False
    grok_processing_budget_usd_per_day: float = DEFAULT_GROK_BUDGET_USD_PER_DAY
    intent_prompt_max_active: int = DEFAULT_INTENT_PROMPT_MAX_ACTIVE


@dataclass
class Deadline:
    near_iso: str = DEFAULT_NEAR_DEADLINE_ISO
    near_btc_target: float = DEFAULT_NEAR_BTC_TARGET
    far_iso: str = DEFAULT_FAR_DEADLINE_ISO
    far_btc_target: float = DEFAULT_FAR_BTC_TARGET


@dataclass
class FomcWindow:
    next_date: Optional[str] = None
    phase: str = "INTER"  # PRE | POST | INTER
    pre_post_reserve_split: Tuple[float, float, float] = (0.25, 0.60, 0.15)


@dataclass
class CowenMemoRef:
    date: Optional[str] = None
    stage: Optional[str] = None
    summary_path: Optional[str] = None
    key_deltas_vs_prior_memo: str = ""


@dataclass
class IntentPrompt:
    intent_id: str
    created_at: str
    prompt_text: str
    pair_scope: List[str] = field(default_factory=lambda: ["*"])
    expires_at: Optional[str] = None
    author: str = "user"
    priority: int = 3


@dataclass
class DocumentRef:
    doc_id: str
    filename: str
    uploaded_at: str
    file_path: str
    doc_type: str = DocumentType.OTHER.value
    processing_status: str = ProcessingStatus.QUEUED.value
    processed_artifact_path: Optional[str] = None
    processed_at: Optional[str] = None
    proposed_updates: List[str] = field(default_factory=list)


@dataclass
class Evidence:
    evidence_id: str
    timestamp: str
    category: str
    source: str
    description: str
    direction: str = "neutral"  # bullish | bearish | neutral
    reliability: float = 0.5
    recency_weight: float = 1.0
    independence_weight: float = 1.0
    magnitude: float = 0.0
    applied_shift: Optional[float] = None
    checklist_impact: Dict[str, str] = field(default_factory=dict)


@dataclass
class Rung:
    rung_idx: int
    price: float
    size: float
    placed_as_userref: Optional[int] = None
    filled_at: Optional[str] = None
    filled_price: Optional[float] = None
    status: str = RungStatus.PENDING.value


@dataclass
class Ladder:
    ladder_id: str
    created_at: str
    expires_at: str
    pair: str
    side: str  # BUY | SELL
    total_size: float
    stop_loss_price: Optional[float]
    rungs: List[Rung] = field(default_factory=list)
    expiry_action: str = "cancel"  # cancel | convert_to_market
    posture_at_creation: str = Posture.PRESERVATION.value
    reasoning: str = ""
    creator: str = "user:dashboard"
    status: str = LadderStatus.ACTIVE.value
    placed_orders: List[str] = field(default_factory=list)


@dataclass
class ProposedThesisUpdate:
    proposal_id: str
    proposed_at: str
    source_doc_id: Optional[str] = None
    source: str = "grok_doc_processor"
    posterior_shift: Optional[Dict[str, Any]] = None
    checklist_updates: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    proposed_intents: List[Dict[str, Any]] = field(default_factory=list)
    new_evidence: List[Dict[str, Any]] = field(default_factory=list)
    posture_recommendation: Optional[str] = None
    reasoning: str = ""
    confidence: float = 0.0
    requires_human: bool = False
    status: str = "pending"
    user_decision_at: Optional[str] = None
    user_notes: Optional[str] = None


@dataclass
class ThesisContext:
    """What the brain sees on every deliberate() call when thesis is active.
    Pure context — no decisions, no overrides."""
    posture: str
    posture_enforcement: str
    posterior_summary: str
    checklist_summary: str
    active_intents: List[IntentPrompt] = field(default_factory=list)
    recent_evidence_summary: str = ""
    active_ladder_for_pair: Optional[Ladder] = None
    hard_rule_warnings: List[str] = field(default_factory=list)
    size_hint: float = 1.0
    conviction_floor_adjustment: float = 0.0


# ═══════════════════════════════════════════════════════════════
# THESIS TRACKER
# ═══════════════════════════════════════════════════════════════

def _ulid() -> str:
    """Cheap ULID-ish identifier. Not a real ULID; good enough for file IDs."""
    return uuid.uuid4().hex[:20]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class ThesisTracker:
    """Slow-moving persistent thesis state for Hydra.

    Phase A wiring: load/save/snapshot/restore + knob updates + current_state
    for WS broadcast. Context injection, ladder matching, size_hint computation,
    and hard-rule gates land in Phases B–E.

    `disabled=True` mode returns a completely inert tracker that matches
    v2.12.5 behavior bit-for-bit. Honors HYDRA_THESIS_DISABLED=1 env flag.
    """

    def __init__(
        self,
        save_dir: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        disabled: bool = False,
    ):
        self._save_dir = save_dir or os.path.dirname(os.path.abspath(__file__))
        self.save_path = os.path.join(self._save_dir, STATE_FILENAME)
        self._disabled = disabled
        if disabled:
            # Inert default — no state, no writes.
            self._state = self._default_state()
            return
        if state is not None:
            self._state = state
        else:
            self._state = self._load_or_default()

    # ─── Construction helpers ─────────────────────────────────────

    @classmethod
    def load_or_default(
        cls,
        save_dir: Optional[str] = None,
        disabled: Optional[bool] = None,
    ) -> "ThesisTracker":
        if disabled is None:
            disabled = bool(os.environ.get("HYDRA_THESIS_DISABLED"))
        return cls(save_dir=save_dir, disabled=disabled)

    @staticmethod
    def _default_state() -> Dict[str, Any]:
        checklist = {
            key: asdict(ChecklistItem(key=key))
            for key in DEFAULT_CHECKLIST_KEYS
        }
        return {
            "version": THESIS_SCHEMA_VERSION,
            "updated_at": _iso_now(),
            "posterior": asdict(Posterior()),
            "checklist": checklist,
            "posture": Posture.PRESERVATION.value,
            "knobs": asdict(ThesisKnobs()),
            "hard_rules": asdict(HardRules()),
            "deadline": asdict(Deadline()),
            "active_intents": [],
            "active_ladders": [],
            "document_library": [],
            "evidence_log": [],
            "fomc_window": asdict(FomcWindow()),
            "cowen_memo": asdict(CowenMemoRef()),
        }

    def _load_or_default(self) -> Dict[str, Any]:
        """Load state from disk; on missing/corrupt → fail-soft to defaults."""
        try:
            if os.path.exists(self.save_path):
                with open(self.save_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._migrate_if_needed(data)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"  [THESIS] load failed ({type(e).__name__}: {e}); using defaults")
        except Exception as e:
            # Last-ditch catch so a corrupt thesis file never crashes the agent.
            print(f"  [THESIS] unexpected load error ({type(e).__name__}: {e}); using defaults")
        return self._default_state()

    @staticmethod
    def _migrate_if_needed(data: Dict[str, Any]) -> Dict[str, Any]:
        """Forward-compat stub. Today only 1.0.0 exists; future schema bumps
        add migration branches here. Missing keys are merged from defaults so
        a partially-written file never crashes the tracker."""
        merged = ThesisTracker._default_state()
        if not isinstance(data, dict):
            return merged
        for k, v in data.items():
            merged[k] = v
        merged["version"] = THESIS_SCHEMA_VERSION
        return merged

    # ─── Persistence ──────────────────────────────────────────────

    def save(self) -> None:
        """Atomic write: .tmp → os.replace. No-op when disabled."""
        if self._disabled:
            return
        self._state["updated_at"] = _iso_now()
        self._state["version"] = THESIS_SCHEMA_VERSION
        tmp_path = self.save_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.save_path)
        except OSError as e:
            # Mirror ParameterTracker: surface the failure so the outer
            # tick-body try/except in hydra_agent.py logs traceback.
            print(f"  [THESIS] save failed: {type(e).__name__}: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def snapshot(self) -> Dict[str, Any]:
        """Return a deep-copyable dict of current state for session snapshot."""
        if self._disabled:
            return {}
        return json.loads(json.dumps(self._state))

    def restore(self, snap: Optional[Dict[str, Any]]) -> None:
        """Restore from a session-snapshot dict. Fail-soft on malformed input."""
        if self._disabled or not snap:
            return
        try:
            self._state = self._migrate_if_needed(snap)
        except Exception as e:
            print(f"  [THESIS] restore failed ({type(e).__name__}: {e}); keeping current state")

    # ─── Knob updates (from dashboard) ────────────────────────────

    def update_knobs(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Merge knob patch into current state, clamp values, persist.

        Returns the applied knob dict (post-clamp). Unknown keys are silently
        ignored for forward-compat. Values that fail type coercion are skipped
        and reported in the `skipped` list of the returned dict's `_meta`.
        """
        if self._disabled:
            return {"_meta": {"disabled": True}}

        knobs = dict(self._state.get("knobs") or asdict(ThesisKnobs()))
        skipped: List[str] = []

        def _coerce(key: str, raw: Any, target_type: type) -> Optional[Any]:
            try:
                if target_type is bool:
                    # Accept "true"/"false" strings from JSON payloads too.
                    if isinstance(raw, str):
                        return raw.strip().lower() in ("1", "true", "yes", "on")
                    return bool(raw)
                if target_type is int:
                    return int(raw)
                if target_type is float:
                    return float(raw)
                return raw
            except (TypeError, ValueError):
                skipped.append(f"{key}:coerce")
                return None

        for key, raw in (patch or {}).items():
            if key == "conviction_floor_adjustment":
                v = _coerce(key, raw, float)
                if v is None:
                    continue
                lo, hi = CONVICTION_FLOOR_ADJUSTMENT_RANGE
                knobs[key] = _clamp(v, lo, hi)
            elif key == "size_hint_range":
                # Expect a [min, max] pair. Reject anything that can't be parsed.
                if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                    skipped.append(f"{key}:shape")
                    continue
                try:
                    lo, hi = float(raw[0]), float(raw[1])
                except (TypeError, ValueError):
                    skipped.append(f"{key}:coerce")
                    continue
                if lo > hi:
                    lo, hi = hi, lo
                hard_lo, hard_hi = SIZE_HINT_HARD_BOUNDS
                knobs[key] = [_clamp(lo, hard_lo, hard_hi), _clamp(hi, hard_lo, hard_hi)]
            elif key == "posture_enforcement":
                if raw in ("off", "advisory", "binding"):
                    knobs[key] = raw
                else:
                    skipped.append(f"{key}:enum")
            elif key == "max_active_ladders_per_pair":
                v = _coerce(key, raw, int)
                if v is None:
                    continue
                knobs[key] = max(0, min(20, v))
            elif key == "ladder_default_expiry_hours":
                v = _coerce(key, raw, int)
                if v is None:
                    continue
                knobs[key] = max(1, min(168, v))  # 1h .. 1 week
            elif key == "ladder_default_offset_pct":
                v = _coerce(key, raw, float)
                if v is None:
                    continue
                knobs[key] = _clamp(v, 0.0, 0.05)
            elif key == "auto_apply_proposed_updates":
                v = _coerce(key, raw, bool)
                if v is None:
                    continue
                knobs[key] = v
            elif key == "grok_processing_budget_usd_per_day":
                v = _coerce(key, raw, float)
                if v is None:
                    continue
                knobs[key] = max(0.0, min(100.0, v))
            elif key == "intent_prompt_max_active":
                v = _coerce(key, raw, int)
                if v is None:
                    continue
                knobs[key] = max(0, min(20, v))
            # Unknown keys: silently dropped (forward-compat).

        self._state["knobs"] = knobs
        self.save()
        return {"knobs": knobs, "_meta": {"skipped": skipped}}

    def update_posture(self, posture: str) -> bool:
        """Set posture. User-driven only in Phase A — no automatic transitions."""
        if self._disabled:
            return False
        if posture not in (p.value for p in Posture):
            return False
        self._state["posture"] = posture
        self.save()
        return True

    def update_hard_rules(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Adjust hard-rule thresholds. The ledger_shield_btc floor cannot
        be reduced below its default (0.20 BTC) via this API — protecting
        the user's stated hard rule."""
        if self._disabled:
            return {"_meta": {"disabled": True}}
        rules = dict(self._state.get("hard_rules") or asdict(HardRules()))
        skipped: List[str] = []
        for key, raw in (patch or {}).items():
            if key == "ledger_shield_btc":
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    skipped.append(f"{key}:coerce")
                    continue
                # Enforce the floor — user can raise but not lower below 0.20.
                rules[key] = max(DEFAULT_LEDGER_SHIELD_BTC, v)
            elif key == "no_altcoin_gate":
                rules[key] = bool(raw)
            elif key == "tax_friction_min_realized_pnl_usd":
                try:
                    rules[key] = max(0.0, float(raw))
                except (TypeError, ValueError):
                    skipped.append(f"{key}:coerce")
            elif key == "locked_kraken_btc_qty":
                try:
                    rules[key] = max(0.0, float(raw))
                except (TypeError, ValueError):
                    skipped.append(f"{key}:coerce")
        self._state["hard_rules"] = rules
        self.save()
        return {"hard_rules": rules, "_meta": {"skipped": skipped}}

    # ─── Tick-local helpers (Phase A: no-op; Phases B–E extend) ───

    def on_tick(self, now_ts: float) -> None:
        """Called once per agent tick. Phase A: no-op. Phase D adds ladder
        expiry sweeps; Phase C drains Grok processor result queue."""
        if self._disabled:
            return
        # Reserved for future phases. Keep lightweight — runs every tick.
        return

    def context_for(self, pair: str, signal: Optional[Dict[str, Any]] = None) -> Optional[ThesisContext]:
        """Return a ThesisContext the brain can consume. Phase A returns None
        unconditionally — Phase B wires real context into ANALYST_PROMPT."""
        if self._disabled:
            return None
        # Phase A: brain not yet wired to thesis context; keep inert.
        return None

    def size_hint_for(self, pair: str, signal: Optional[Dict[str, Any]] = None) -> float:
        """Multiplicative size modifier for execute_signal. Phase A always
        returns 1.0 (no sizing change) regardless of posture — Phase B wires
        the real hint bounded by knobs.size_hint_range."""
        if self._disabled:
            return 1.0
        return 1.0

    # ─── State snapshots for dashboard broadcast ──────────────────

    def current_state(self) -> Dict[str, Any]:
        """Shape consumed by the dashboard THESIS tab via WS. Omits heavy
        evidence-log internals; surfaces a compact summary instead."""
        if self._disabled:
            return {"disabled": True, "version": THESIS_SCHEMA_VERSION}
        s = self._state
        evidence = s.get("evidence_log", []) or []
        return {
            "version": s.get("version", THESIS_SCHEMA_VERSION),
            "updated_at": s.get("updated_at"),
            "posture": s.get("posture"),
            "posterior": s.get("posterior"),
            "checklist": s.get("checklist"),
            "knobs": s.get("knobs"),
            "hard_rules": s.get("hard_rules"),
            "deadline": s.get("deadline"),
            "fomc_window": s.get("fomc_window"),
            "cowen_memo": s.get("cowen_memo"),
            "active_intents": s.get("active_intents", []),
            "active_ladders": s.get("active_ladders", []),
            "document_library_count": len(s.get("document_library", []) or []),
            "evidence_log_count": len(evidence),
            "recent_evidence": list(evidence[-10:]) if evidence else [],
            "disabled": False,
        }

    # ─── Introspection helpers (used by tests + dashboard) ────────

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def posture(self) -> str:
        return self._state.get("posture", Posture.PRESERVATION.value)

    @property
    def knobs(self) -> Dict[str, Any]:
        return dict(self._state.get("knobs") or asdict(ThesisKnobs()))

    @property
    def hard_rules(self) -> Dict[str, Any]:
        return dict(self._state.get("hard_rules") or asdict(HardRules()))


# ═══════════════════════════════════════════════════════════════
# MODULE SMOKE (CI invokes `python -c "import hydra_thesis"`)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        assert t.posture == Posture.PRESERVATION.value
        t.update_knobs({"conviction_floor_adjustment": 0.05})
        assert abs(t.knobs["conviction_floor_adjustment"] - 0.05) < 1e-9
        snap = t.snapshot()
        t2 = ThesisTracker(save_dir=d, state=None)
        t2.restore(snap)
        assert abs(t2.knobs["conviction_floor_adjustment"] - 0.05) < 1e-9
        print("hydra_thesis smoke OK")
