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

# Phase E (binding enforcement, opt-in): daily entry caps per posture.
# None = uncapped. PRESERVATION is the tightest — reflects user's
# "slow money is real money" stance. ACCUMULATION uncapped by default
# because by definition you're deploying capital actively.
DEFAULT_MAX_DAILY_ENTRIES_BY_POSTURE: Dict[str, Optional[int]] = {
    "PRESERVATION": 2,
    "TRANSITION": 4,
    "ACCUMULATION": None,
}
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
    # Phase E: per-posture daily entry caps. Consulted only when
    # posture_enforcement == "binding". Field is a dict (not a tuple)
    # so users can tune each posture independently from the dashboard.
    max_daily_entries_by_posture: Dict[str, Optional[int]] = field(
        default_factory=lambda: dict(DEFAULT_MAX_DAILY_ENTRIES_BY_POSTURE)
    )


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
            elif key == "max_daily_entries_by_posture":
                # Phase E knob: dict {PRESERVATION: int|None, TRANSITION: int|None, ACCUMULATION: int|None}
                if not isinstance(raw, dict):
                    skipped.append(f"{key}:shape")
                    continue
                sanitized: Dict[str, Optional[int]] = {}
                for k, val in raw.items():
                    if k not in ("PRESERVATION", "TRANSITION", "ACCUMULATION"):
                        continue
                    if val is None:
                        sanitized[k] = None
                        continue
                    try:
                        n = int(val)
                    except (TypeError, ValueError):
                        skipped.append(f"{key}.{k}:coerce")
                        continue
                    sanitized[k] = max(0, min(100, n))
                # Merge with defaults so missing keys retain their prior value
                merged = dict(knobs.get("max_daily_entries_by_posture")
                              or DEFAULT_MAX_DAILY_ENTRIES_BY_POSTURE)
                merged.update(sanitized)
                knobs[key] = merged
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

    # ─── Tick-local helpers ───────────────────────────────────────

    def on_tick(self, now_ts: float) -> None:
        """Called once per agent tick. Sweeps expired intent prompts (Phase
        B) and expired ladders (Phase D, feature-flagged). Kept lightweight
        — runs every tick."""
        if self._disabled:
            return
        self._sweep_expired_intents(now_ts)
        self._sweep_expired_ladders(now_ts)

    def _sweep_expired_intents(self, now_ts: float) -> None:
        intents = self._state.get("active_intents", []) or []
        if not intents:
            return
        kept: List[Dict[str, Any]] = []
        dropped = 0
        for it in intents:
            if not isinstance(it, dict):
                continue
            exp = it.get("expires_at")
            if not exp:
                kept.append(it)
                continue
            try:
                # ISO-8601 compare as string: YYYY-MM-DDTHH:MM:SSZ sorts lexicographically
                if exp < _iso_now():
                    dropped += 1
                    continue
            except Exception:
                pass
            kept.append(it)
        if dropped:
            self._state["active_intents"] = kept
            self.save()

    def context_for(
        self,
        pair: str,
        signal: Optional[Dict[str, Any]] = None,
    ) -> Optional[ThesisContext]:
        """Return a ThesisContext the brain can consume.

        Phase B wiring: surfaces posture, posterior summary, checklist summary,
        intent prompts scoped to this pair, and any hard-rule warnings that
        apply to the current signal. Active ladders and real-evidence summary
        arrive in Phases D and C respectively — fields present but empty.

        Returns None only when the tracker is disabled (kill switch).
        """
        if self._disabled:
            return None
        s = self._state
        posture = s.get("posture", Posture.PRESERVATION.value)
        knobs = s.get("knobs") or asdict(ThesisKnobs())
        posterior = s.get("posterior") or {}
        checklist = s.get("checklist") or {}

        # Posterior summary: "LATE_CYCLE_DIGESTION @ 0.62"
        pos_regime = posterior.get("regime", "UNKNOWN")
        pos_conf = posterior.get("confidence")
        try:
            pos_conf_f = float(pos_conf) if pos_conf is not None else 0.0
        except (TypeError, ValueError):
            pos_conf_f = 0.0
        posterior_summary = f"{pos_regime} @ {pos_conf_f:.2f}"

        # Checklist summary: "2/5 met"
        keys = list(checklist.keys())
        met = [k for k in keys if (checklist[k] or {}).get("status") == "MET"]
        checklist_summary = f"{len(met)}/{len(keys)} met" + (
            f" ({', '.join(met)})" if met else ""
        )

        # Active intents scoped to this pair or "*"
        active_intents = [
            IntentPrompt(
                intent_id=i.get("intent_id", ""),
                created_at=i.get("created_at", ""),
                prompt_text=i.get("prompt_text", ""),
                pair_scope=i.get("pair_scope") or ["*"],
                expires_at=i.get("expires_at"),
                author=i.get("author", "user"),
                priority=int(i.get("priority", 3)),
            )
            for i in self._active_intents_raw_for_pair(pair)
        ]
        # Sort by priority desc so most prominent surfaces first.
        active_intents.sort(key=lambda ip: -ip.priority)

        # Hard-rule warnings (non-blocking in Phase B — brain reads and reasons).
        warnings = self._hard_rule_warnings(pair, signal)

        return ThesisContext(
            posture=posture,
            posture_enforcement=knobs.get("posture_enforcement", DEFAULT_POSTURE_ENFORCEMENT),
            posterior_summary=posterior_summary,
            checklist_summary=checklist_summary,
            active_intents=active_intents,
            recent_evidence_summary="",  # Phase C populates from Grok processor
            active_ladder_for_pair=None,  # Phase D populates from active_ladders match
            hard_rule_warnings=warnings,
            size_hint=self.size_hint_for(pair, signal),
            conviction_floor_adjustment=float(knobs.get("conviction_floor_adjustment", 0.0)),
        )

    def size_hint_for(
        self,
        pair: str,
        signal: Optional[Dict[str, Any]] = None,
    ) -> float:
        """Multiplicative size modifier for execute_signal.

        Phase B contract: returns 1.0 when posture_enforcement is "off" or
        "advisory" (the default). Only "binding" mode (opt-in, Phase E)
        derives a real multiplier from knobs.size_hint_range + posture.

        This preserves the design stance that Phase B is pure brain context
        augmentation — it cannot alter live sizing until the user explicitly
        opts into binding enforcement.
        """
        if self._disabled:
            return 1.0
        knobs = self._state.get("knobs") or {}
        enforcement = knobs.get("posture_enforcement", DEFAULT_POSTURE_ENFORCEMENT)
        if enforcement != "binding":
            return 1.0
        # Phase E path (reserved): interpolate by posture within size_hint_range.
        rng = knobs.get("size_hint_range") or list(DEFAULT_SIZE_HINT_RANGE)
        try:
            lo, hi = float(rng[0]), float(rng[1])
        except (TypeError, ValueError, IndexError):
            lo, hi = DEFAULT_SIZE_HINT_RANGE
        mid = (lo + hi) / 2.0
        posture = self._state.get("posture", Posture.PRESERVATION.value)
        if posture == Posture.PRESERVATION.value:
            hint = lo
        elif posture == Posture.ACCUMULATION.value:
            hint = hi
        else:
            hint = mid
        hard_lo, hard_hi = SIZE_HINT_HARD_BOUNDS
        return _clamp(hint, hard_lo, hard_hi)

    def _active_intents_raw_for_pair(self, pair: str) -> List[Dict[str, Any]]:
        """Return raw intent dicts whose pair_scope covers this pair."""
        intents = self._state.get("active_intents", []) or []
        out: List[Dict[str, Any]] = []
        for it in intents:
            if not isinstance(it, dict):
                continue
            scope = it.get("pair_scope") or ["*"]
            if "*" in scope or pair in scope:
                out.append(it)
        return out

    def _hard_rule_warnings(
        self,
        pair: str,
        signal: Optional[Dict[str, Any]],
    ) -> List[str]:
        """Surface advisory messages about hard-rule exposure on this signal.
        Never blocks — brain reads and reasons. Phase E opts into binding
        enforcement; even then, only true ledger-shield violations BLOCK."""
        rules = self._state.get("hard_rules") or {}
        warnings: List[str] = []
        if pair == "BTC/USDC" and signal and signal.get("action") == "SELL":
            shield = rules.get("ledger_shield_btc", DEFAULT_LEDGER_SHIELD_BTC)
            warnings.append(f"ledger_shield: {shield} BTC is long-term hold (untouchable)")
        tax_floor = rules.get("tax_friction_min_realized_pnl_usd")
        if tax_floor and signal and signal.get("action") == "SELL":
            warnings.append(f"tax_friction: realized gains below ${tax_floor} are not worth the tax")
        return warnings

    # ─── Intent prompt CRUD (Phase B) ─────────────────────────────

    def list_intents(self) -> List[Dict[str, Any]]:
        """Return shallow copies of all active intent records."""
        if self._disabled:
            return []
        return [dict(i) for i in (self._state.get("active_intents", []) or [])]

    def add_intent(
        self,
        prompt_text: str,
        pair_scope: Optional[List[str]] = None,
        priority: int = 3,
        expires_at: Optional[str] = None,
        author: str = "user",
    ) -> Optional[Dict[str, Any]]:
        """Create a new intent prompt. Enforces knobs.intent_prompt_max_active
        — if the cap is reached, the oldest intent (by created_at) is evicted
        to make room, mirroring a FIFO circular buffer. Returns the created
        intent dict (or None when disabled / empty text)."""
        if self._disabled:
            return None
        text = (prompt_text or "").strip()
        if not text:
            return None
        intents = list(self._state.get("active_intents", []) or [])
        knobs = self._state.get("knobs") or {}
        cap = int(knobs.get("intent_prompt_max_active", DEFAULT_INTENT_PROMPT_MAX_ACTIVE))
        if cap > 0:
            while len(intents) >= cap:
                # Evict oldest
                intents.pop(0)
        # Validate pair_scope
        scope = pair_scope or ["*"]
        if not isinstance(scope, list) or not scope:
            scope = ["*"]
        # Coerce priority into [1, 5]
        try:
            prio = int(priority)
        except (TypeError, ValueError):
            prio = 3
        prio = max(1, min(5, prio))
        new_intent = {
            "intent_id": _ulid(),
            "created_at": _iso_now(),
            "prompt_text": text[:2000],  # bound context bloat
            "pair_scope": [str(p) for p in scope],
            "expires_at": expires_at if isinstance(expires_at, str) else None,
            "author": str(author or "user")[:64],
            "priority": prio,
        }
        intents.append(new_intent)
        self._state["active_intents"] = intents
        self.save()
        return dict(new_intent)

    def remove_intent(self, intent_id: str) -> bool:
        """Delete an intent by ID. Returns True on success, False when no
        match or disabled."""
        if self._disabled or not intent_id:
            return False
        intents = list(self._state.get("active_intents", []) or [])
        new_intents = [i for i in intents if (i or {}).get("intent_id") != intent_id]
        if len(new_intents) == len(intents):
            return False
        self._state["active_intents"] = new_intents
        self.save()
        return True

    # ─── Posture enforcement (Phase E, opt-in) ────────────────────
    #
    # Only binding enforcement consults these gates. The check is soft —
    # when the daily cap is reached, new entries are SKIPPED (not placed)
    # and the agent broadcasts a restriction alert. The journal gets no
    # entry for a skipped trade (there's no placed order to record).
    # This preserves the "Hydra is the flywheel" stance: hard-rule BLOCK
    # remains the only true veto; posture-cap SKIP is reversible (happens
    # tomorrow) and transparent.

    def _utc_day_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def daily_entries_for(self, pair: str) -> int:
        """Count today's recorded entries for a pair. Rolls over at UTC
        midnight; the map is kept bounded by pruning old days on access."""
        if self._disabled:
            return 0
        de = self._state.get("daily_entries") or {}
        today = self._utc_day_key()
        day_bucket = de.get(today) or {}
        return int(day_bucket.get(pair, 0))

    def record_entry(self, pair: str) -> None:
        """Increment today's entry counter for a pair. Called by the agent
        after a trade successfully places. The counter is posture-agnostic
        — the cap lookup at check time consults current posture."""
        if self._disabled:
            return
        today = self._utc_day_key()
        de = dict(self._state.get("daily_entries") or {})
        # Prune any day keys older than today (keep today's bucket only).
        # Phase E doesn't need historical counts — just "today".
        de = {today: de.get(today) or {}}
        bucket = dict(de[today])
        bucket[pair] = int(bucket.get(pair, 0)) + 1
        de[today] = bucket
        self._state["daily_entries"] = de
        self.save()

    def check_posture_restriction(
        self, pair: str, side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return {"allow": bool, "reason": str, "entries_today": int, "cap": Optional[int]}.

        Only binding enforcement restricts. Under "off" or "advisory" this
        always allows — so default installs after upgrading to v2.13.4 see
        zero behavior change.
        """
        if self._disabled:
            return {"allow": True, "reason": "", "entries_today": 0, "cap": None}
        knobs = self._state.get("knobs") or {}
        enforcement = knobs.get("posture_enforcement", DEFAULT_POSTURE_ENFORCEMENT)
        if enforcement != "binding":
            return {"allow": True, "reason": "", "entries_today": 0, "cap": None}
        posture = self._state.get("posture", Posture.PRESERVATION.value)
        caps = knobs.get("max_daily_entries_by_posture") or DEFAULT_MAX_DAILY_ENTRIES_BY_POSTURE
        cap = caps.get(posture)
        if cap is None:
            return {"allow": True, "reason": "", "entries_today": self.daily_entries_for(pair), "cap": None}
        try:
            cap_i = int(cap)
        except (TypeError, ValueError):
            return {"allow": True, "reason": "", "entries_today": self.daily_entries_for(pair), "cap": None}
        count = self.daily_entries_for(pair)
        if count >= cap_i:
            return {
                "allow": False,
                "reason": f"posture_{posture.lower()}_daily_cap",
                "entries_today": count,
                "cap": cap_i,
            }
        return {
            "allow": True, "reason": "",
            "entries_today": count, "cap": cap_i,
        }

    # ─── Ladder CRUD + journal-stamping match (Phase D) ──────────
    #
    # Phase D scope: the Ladder primitive makes *intent* first-class in the
    # journal. A user authors a multi-tick plan (total size, stop-loss,
    # predetermined rung prices) and every subsequent placed order whose
    # (pair, side, price) matches a pending rung gets stamped with
    # (ladder_id, rung_idx). Orders that don't match any rung stamp
    # adhoc=true — they stay legal (Hydra is the flywheel). The journal
    # now distinguishes planned deployment from tactical opportunism.
    #
    # Stop-loss + 24h expiry are advisory in Phase D — on breach/expire,
    # the ladder transitions to STOPPED_OUT / CANCELLED and remaining
    # pending rungs mark CANCELLED. Kraken-side cancellation of already-
    # placed rung orders is a separate concern (the agent's shutdown
    # path already cancels resting limits). Future phases may auto-sell
    # filled positions on stop breach — not in v2.13.3.

    # Match tolerance: within 0.5% of rung price counts as a hit. Tight
    # enough to preclude false positives; loose enough to catch orders
    # that round to the pair's native decimal precision.
    RUNG_PRICE_TOLERANCE_PCT = 0.005

    def _ladders_enabled(self) -> bool:
        """Phase D is feature-flagged so users can opt in via env AND the
        tracker is enabled AND the user has at least one active ladder.
        This keeps the journal schema stable for everyone who hasn't
        opted in."""
        return (
            not self._disabled
            and bool(os.environ.get("HYDRA_THESIS_LADDERS"))
        )

    def list_ladders(self) -> List[Dict[str, Any]]:
        if self._disabled:
            return []
        return [dict(l) for l in (self._state.get("active_ladders", []) or [])]

    def create_ladder(
        self,
        pair: str,
        side: str,
        total_size: float,
        rungs_spec: List[Dict[str, Any]],
        stop_loss_price: Optional[float] = None,
        expiry_hours: Optional[int] = None,
        expiry_action: str = "cancel",
        reasoning: str = "",
        creator: str = "user:dashboard",
    ) -> Optional[Dict[str, Any]]:
        """Author a new Ladder. rungs_spec is a list of {price, size} dicts.
        Sizes need not sum exactly to total_size (we surface the discrepancy
        in a warning and scale proportionally).

        Returns the Ladder dict on success. None on validation failure or
        disabled.
        """
        if self._disabled:
            return None
        if not pair or side not in ("BUY", "SELL"):
            return None
        if not rungs_spec or not isinstance(rungs_spec, list):
            return None

        knobs = self._state.get("knobs") or {}
        cap_per_pair = int(knobs.get("max_active_ladders_per_pair",
                                     DEFAULT_MAX_ACTIVE_LADDERS_PER_PAIR))
        existing = self.list_ladders()
        active_on_pair = sum(
            1 for l in existing
            if l.get("pair") == pair and l.get("status") == LadderStatus.ACTIVE.value
        )
        if active_on_pair >= cap_per_pair:
            print(f"  [THESIS] cap reached for {pair}: {active_on_pair}/{cap_per_pair}")
            return None

        hours = int(expiry_hours or knobs.get(
            "ladder_default_expiry_hours", DEFAULT_LADDER_EXPIRY_HOURS,
        ))
        hours = max(1, min(168, hours))
        created_ts = time.time()
        expires_ts = created_ts + hours * 3600
        expires_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_ts))
        created_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_ts))

        # Build rungs
        rungs: List[Dict[str, Any]] = []
        for idx, spec in enumerate(rungs_spec):
            try:
                price = float(spec.get("price"))
                size = float(spec.get("size"))
            except (TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            rungs.append(asdict(Rung(rung_idx=idx, price=price, size=size)))
        if not rungs:
            return None

        # Optional: scale rung sizes to sum to total_size if they don't
        try:
            total = float(total_size)
            sum_sz = sum(r["size"] for r in rungs)
            if total > 0 and sum_sz > 0 and abs(sum_sz - total) > 1e-9:
                factor = total / sum_sz
                for r in rungs:
                    r["size"] = r["size"] * factor
        except (TypeError, ValueError):
            total = sum(r["size"] for r in rungs)

        ladder = asdict(Ladder(
            ladder_id=_ulid(),
            created_at=created_iso,
            expires_at=expires_iso,
            pair=str(pair),
            side=side,
            total_size=float(total),
            stop_loss_price=float(stop_loss_price) if stop_loss_price else None,
            rungs=rungs,
            expiry_action=(expiry_action if expiry_action in ("cancel", "convert_to_market")
                           else "cancel"),
            posture_at_creation=self._state.get("posture", Posture.PRESERVATION.value),
            reasoning=str(reasoning or "")[:500],
            creator=str(creator or "user:dashboard")[:64],
            status=LadderStatus.ACTIVE.value,
        ))
        ladders = list(self._state.get("active_ladders", []) or [])
        ladders.append(ladder)
        self._state["active_ladders"] = ladders
        self.save()
        return dict(ladder)

    def cancel_ladder(self, ladder_id: str, reason: str = "user_request") -> bool:
        """Flip a ladder's status to CANCELLED. Remaining pending rungs
        are also flipped. No Kraken-side cancel is sent — agent shutdown
        handles resting-order cleanup."""
        if self._disabled or not ladder_id:
            return False
        ladders = list(self._state.get("active_ladders", []) or [])
        hit = False
        for l in ladders:
            if l.get("ladder_id") != ladder_id:
                continue
            if l.get("status") != LadderStatus.ACTIVE.value:
                continue
            l["status"] = LadderStatus.CANCELLED.value
            for r in l.get("rungs", []) or []:
                if r.get("status") == RungStatus.PENDING.value:
                    r["status"] = RungStatus.CANCELLED.value
            hit = True
        if hit:
            self.save()
        return hit

    def match_rung(
        self, pair: str, side: str, price: float,
    ) -> Optional[Dict[str, Any]]:
        """Return {"ladder_id": ..., "rung_idx": ...} when (pair, side, price)
        aligns with a PENDING rung of an ACTIVE ladder. None otherwise.
        Phase D match uses RUNG_PRICE_TOLERANCE_PCT as the tolerance band."""
        if not self._ladders_enabled():
            return None
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return None
        ladders = self._state.get("active_ladders", []) or []
        for l in ladders:
            if l.get("status") != LadderStatus.ACTIVE.value:
                continue
            if l.get("pair") != pair or l.get("side") != side:
                continue
            for r in l.get("rungs", []) or []:
                if r.get("status") != RungStatus.PENDING.value:
                    continue
                rp = float(r.get("price", 0) or 0)
                if rp <= 0:
                    continue
                tol = abs(rp) * self.RUNG_PRICE_TOLERANCE_PCT
                if abs(price_f - rp) <= tol:
                    return {"ladder_id": l.get("ladder_id"),
                            "rung_idx": r.get("rung_idx")}
        return None

    def record_rung_placement(
        self, ladder_id: str, rung_idx: int,
        userref: Optional[int] = None,
    ) -> bool:
        """Transition a rung PENDING → PLACED after _place_order succeeds."""
        if self._disabled or not ladder_id:
            return False
        ladders = list(self._state.get("active_ladders", []) or [])
        hit = False
        for l in ladders:
            if l.get("ladder_id") != ladder_id:
                continue
            for r in l.get("rungs", []) or []:
                if r.get("rung_idx") != rung_idx:
                    continue
                r["status"] = RungStatus.PLACED.value
                if userref is not None:
                    r["placed_as_userref"] = int(userref)
                hit = True
        if hit:
            self.save()
        return hit

    def record_rung_fill(
        self, ladder_id: str, rung_idx: int, filled_price: float,
    ) -> bool:
        """Transition a rung PLACED → FILLED and mark Ladder FILLED when
        all rungs have terminated."""
        if self._disabled or not ladder_id:
            return False
        ladders = list(self._state.get("active_ladders", []) or [])
        hit = False
        for l in ladders:
            if l.get("ladder_id") != ladder_id:
                continue
            for r in l.get("rungs", []) or []:
                if r.get("rung_idx") != rung_idx:
                    continue
                r["status"] = RungStatus.FILLED.value
                r["filled_at"] = _iso_now()
                try:
                    r["filled_price"] = float(filled_price)
                except (TypeError, ValueError):
                    pass
                hit = True
            # Advance ladder status if every rung terminated
            rungs = l.get("rungs", []) or []
            if rungs and all(
                r.get("status") in (RungStatus.FILLED.value, RungStatus.CANCELLED.value)
                for r in rungs
            ):
                if any(r.get("status") == RungStatus.FILLED.value for r in rungs):
                    l["status"] = LadderStatus.FILLED.value
                else:
                    l["status"] = LadderStatus.CANCELLED.value
        if hit:
            self.save()
        return hit

    def check_stop_loss(
        self, pair: str, price: float,
    ) -> List[str]:
        """Find ACTIVE BUY ladders on `pair` whose stop_loss_price has been
        breached by `price`. Transitions matching ladders to STOPPED_OUT
        and cancels their PENDING rungs. Returns list of ladder_ids that
        flipped. Phase D is advisory — no SELL is placed on the filled
        portion; that's a user decision surfaced via WS alert."""
        if not self._ladders_enabled():
            return []
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return []
        ladders = self._state.get("active_ladders", []) or []
        breached: List[str] = []
        for l in ladders:
            if l.get("status") != LadderStatus.ACTIVE.value:
                continue
            if l.get("pair") != pair:
                continue
            stop = l.get("stop_loss_price")
            if stop is None:
                continue
            try:
                stop_f = float(stop)
            except (TypeError, ValueError):
                continue
            side = l.get("side")
            # BUY ladder stops when price drops below stop_loss
            # SELL ladder stops when price rises above stop_loss
            breach = (side == "BUY" and price_f <= stop_f) or \
                     (side == "SELL" and price_f >= stop_f)
            if not breach:
                continue
            # Only trip if we've actually filled something (Athena's point:
            # an unfilled ladder that moves against you is fine to cancel,
            # but "stopped out" should imply capital was committed).
            any_fill = any(
                r.get("status") == RungStatus.FILLED.value
                for r in (l.get("rungs", []) or [])
            )
            l["status"] = (LadderStatus.STOPPED_OUT.value if any_fill
                           else LadderStatus.CANCELLED.value)
            for r in l.get("rungs", []) or []:
                if r.get("status") == RungStatus.PENDING.value:
                    r["status"] = RungStatus.CANCELLED.value
            breached.append(l.get("ladder_id"))
        if breached:
            self.save()
        return breached

    def _sweep_expired_ladders(self, now_ts: float) -> List[str]:
        """Flip ACTIVE ladders whose expires_at has passed. Returns list
        of ladder_ids that expired. Respects expiry_action: "cancel" is
        honored today; "convert_to_market" is parked for a future phase
        (flagged in the log and treated as cancel for safety)."""
        if not self._ladders_enabled():
            return []
        try:
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
        except (OverflowError, OSError):
            now_iso = _iso_now()
        ladders = self._state.get("active_ladders", []) or []
        expired: List[str] = []
        for l in ladders:
            if l.get("status") != LadderStatus.ACTIVE.value:
                continue
            exp = l.get("expires_at")
            if not exp:
                continue
            try:
                if exp > now_iso:
                    continue
            except Exception:
                continue
            action = l.get("expiry_action", "cancel")
            if action == "convert_to_market":
                print(f"  [THESIS] ladder {l.get('ladder_id')} expired with "
                      f"convert_to_market — Phase D treats this as cancel (future work)")
            l["status"] = LadderStatus.CANCELLED.value
            for r in l.get("rungs", []) or []:
                if r.get("status") == RungStatus.PENDING.value:
                    r["status"] = RungStatus.CANCELLED.value
            expired.append(l.get("ladder_id"))
        if expired:
            self.save()
        return expired

    # ─── Document + proposal handling (Phase C) ───────────────────

    def _pending_dir(self) -> str:
        return os.path.join(self._save_dir, PENDING_DIRNAME)

    def _documents_dir(self) -> str:
        return os.path.join(self._save_dir, DOCUMENTS_DIRNAME)

    def upload_document(
        self,
        filename: str,
        content: str,
        doc_type: str = "other",
    ) -> Optional[Dict[str, Any]]:
        """Save a document to hydra_thesis_documents/ and append a
        DocumentRef to the library. Returns the DocumentRef dict on
        success, None on failure or disabled."""
        if self._disabled:
            return None
        if not content or not isinstance(content, str):
            return None
        safe_name = (filename or "note.md").replace("/", "_").replace("\\", "_")[:120]
        doc_id = _ulid()
        # Persist the raw content to disk so a processor worker can pick
        # it up later if it's offline at upload time.
        docs_dir = self._documents_dir()
        try:
            os.makedirs(docs_dir, exist_ok=True)
        except OSError as e:
            print(f"  [THESIS] documents dir create failed: {type(e).__name__}: {e}")
            return None
        file_path = os.path.join(docs_dir, f"{doc_id}__{safe_name}")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            print(f"  [THESIS] document save failed: {type(e).__name__}: {e}")
            return None
        ref = asdict(DocumentRef(
            doc_id=doc_id,
            filename=safe_name,
            uploaded_at=_iso_now(),
            file_path=file_path,
            doc_type=str(doc_type or "other")[:64],
        ))
        library = list(self._state.get("document_library", []) or [])
        library.append(ref)
        self._state["document_library"] = library
        self.save()
        return dict(ref)

    def write_pending_proposal(self, proposal: Dict[str, Any]) -> Optional[str]:
        """Persist a ProposedThesisUpdate-shape dict to hydra_thesis_pending/.
        Called by the processor worker via its on_proposal callback. Returns
        the file path on success. Safe when disabled (no-op)."""
        if self._disabled or not isinstance(proposal, dict):
            return None
        pending_dir = self._pending_dir()
        try:
            os.makedirs(pending_dir, exist_ok=True)
        except OSError as e:
            print(f"  [THESIS] pending dir create failed: {type(e).__name__}: {e}")
            return None
        pid = proposal.get("proposal_id") or _ulid()
        proposal["proposal_id"] = pid
        path = os.path.join(pending_dir, f"{pid}.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(proposal, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            print(f"  [THESIS] pending proposal write failed: {type(e).__name__}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None
        return path

    def list_pending_proposals(self) -> List[Dict[str, Any]]:
        """Read all proposals in hydra_thesis_pending/. Returns empty when
        disabled OR no directory exists yet. Caps at MAX_PROPOSAL_RETAIN."""
        if self._disabled:
            return []
        pending_dir = self._pending_dir()
        if not os.path.isdir(pending_dir):
            return []
        out: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(pending_dir))
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(pending_dir, name), "r", encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception:
                continue
        return out[-200:]

    def approve_proposal(
        self, proposal_id: str, user_notes: Optional[str] = None,
    ) -> bool:
        """Apply a pending proposal to the thesis state and archive the file.
        Returns True on success, False when the proposal is missing,
        malformed, disabled, or violates a hard rule (see _apply_proposal)."""
        if self._disabled or not proposal_id:
            return False
        path = os.path.join(self._pending_dir(), f"{proposal_id}.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                proposal = json.load(f)
        except Exception as e:
            print(f"  [THESIS] approve load failed ({type(e).__name__}: {e})")
            return False
        applied = self._apply_proposal(proposal)
        if not applied:
            return False
        proposal["status"] = "approved"
        proposal["user_decision_at"] = _iso_now()
        proposal["user_notes"] = user_notes
        # Atomic move: write archived version, remove pending
        self._archive_proposal(proposal_id, proposal)
        self.save()
        return True

    def reject_proposal(
        self, proposal_id: str, user_notes: Optional[str] = None,
    ) -> bool:
        """Archive a pending proposal WITHOUT applying it."""
        if self._disabled or not proposal_id:
            return False
        path = os.path.join(self._pending_dir(), f"{proposal_id}.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                proposal = json.load(f)
        except Exception:
            proposal = {"proposal_id": proposal_id, "status": "rejected"}
        proposal["status"] = "rejected"
        proposal["user_decision_at"] = _iso_now()
        proposal["user_notes"] = user_notes
        self._archive_proposal(proposal_id, proposal)
        return True

    def _archive_proposal(self, proposal_id: str, proposal: Dict[str, Any]) -> None:
        """Remove from hydra_thesis_pending/ — for Phase C we keep things
        simple and just delete the pending file. A future phase can move
        it under a processed/ sibling for audit retention."""
        path = os.path.join(self._pending_dir(), f"{proposal_id}.json")
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _apply_proposal(self, proposal: Dict[str, Any]) -> bool:
        """Merge a proposal's fields into the thesis state.

        Contract:
        - posterior_shift replaces the entire Posterior object
        - checklist_updates merge by key (dict.update semantics)
        - proposed_intents each go through add_intent (which applies the
          knob cap + clamps)
        - new_evidence appended to evidence_log (bounded)
        - posture_recommendation applied ONLY when user approves — this
          approval IS the explicit human action required for transitions
        - Hard rules are NEVER mutated by a proposal — ledger_shield_btc,
          no_altcoin_gate, tax_friction_min_realized_pnl_usd are read-only
          to Grok. Any attempt is silently dropped.
        """
        if not isinstance(proposal, dict):
            return False
        try:
            shift = proposal.get("posterior_shift")
            if isinstance(shift, dict):
                reg = shift.get("regime")
                conf = shift.get("confidence")
                if reg:
                    post = self._state.get("posterior") or asdict(Posterior())
                    post["regime"] = str(reg)
                    if conf is not None:
                        try:
                            post["confidence"] = _clamp(float(conf), 0.0, 1.0)
                        except (TypeError, ValueError):
                            pass
                    self._state["posterior"] = post

            cu = proposal.get("checklist_updates") or {}
            if isinstance(cu, dict):
                cl = self._state.get("checklist") or {}
                for k, v in cu.items():
                    if not isinstance(v, dict):
                        continue
                    existing = cl.get(k) or asdict(ChecklistItem(key=str(k)))
                    status = v.get("status")
                    if status in (s.value for s in ChecklistItemStatus):
                        existing["status"] = status
                    notes = v.get("notes")
                    if isinstance(notes, str):
                        existing["notes"] = notes[:500]
                    existing["last_movement"] = _iso_now()
                    cl[k] = existing
                self._state["checklist"] = cl

            intents = proposal.get("proposed_intents") or []
            if isinstance(intents, list):
                for i in intents:
                    if not isinstance(i, dict):
                        continue
                    self.add_intent(
                        prompt_text=i.get("prompt_text", ""),
                        pair_scope=i.get("pair_scope"),
                        priority=i.get("priority", 3),
                        expires_at=i.get("expires_at"),
                        author="thesis_processor:grok",
                    )

            ev_in = proposal.get("new_evidence") or []
            if isinstance(ev_in, list):
                ev_log = list(self._state.get("evidence_log", []) or [])
                for e in ev_in:
                    if not isinstance(e, dict):
                        continue
                    ev_log.append(asdict(Evidence(
                        evidence_id=_ulid(),
                        timestamp=_iso_now(),
                        category=str(e.get("category", "MACRO"))[:32],
                        source=str(e.get("source", "grok_proposal"))[:128],
                        description=str(e.get("description", ""))[:500],
                        direction=str(e.get("direction", "neutral"))[:16],
                        magnitude=_clamp(float(e.get("magnitude", 0.0) or 0.0), 0.0, 1.0),
                    )))
                # Bound in-memory log
                if len(ev_log) > EVIDENCE_LOG_MAX_IN_MEMORY:
                    ev_log = ev_log[-EVIDENCE_LOG_MAX_IN_MEMORY:]
                self._state["evidence_log"] = ev_log

            posture_rec = proposal.get("posture_recommendation")
            if posture_rec in (p.value for p in Posture):
                self._state["posture"] = posture_rec

            return True
        except Exception as e:
            print(f"  [THESIS] _apply_proposal failed ({type(e).__name__}: {e})")
            return False

    def update_intent(self, intent_id: str, patch: Dict[str, Any]) -> bool:
        """Edit an existing intent. Only prompt_text, pair_scope, priority,
        expires_at are mutable — intent_id, created_at, author are frozen."""
        if self._disabled or not intent_id or not isinstance(patch, dict):
            return False
        intents = list(self._state.get("active_intents", []) or [])
        hit = False
        for i in intents:
            if (i or {}).get("intent_id") != intent_id:
                continue
            if "prompt_text" in patch:
                text = str(patch["prompt_text"] or "").strip()
                if text:
                    i["prompt_text"] = text[:2000]
            if "pair_scope" in patch:
                scope = patch["pair_scope"]
                if isinstance(scope, list) and scope:
                    i["pair_scope"] = [str(p) for p in scope]
            if "priority" in patch:
                try:
                    p = max(1, min(5, int(patch["priority"])))
                    i["priority"] = p
                except (TypeError, ValueError):
                    pass
            if "expires_at" in patch:
                exp = patch["expires_at"]
                i["expires_at"] = exp if isinstance(exp, str) or exp is None else None
            hit = True
            break
        if hit:
            self.save()
        return hit

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
