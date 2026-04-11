"""Order journal entry schema — single shape, one state machine.

Every entry produced by _place_order / _place_paper_order / _apply_execution_event
must conform to the single schema below. validate_journal_entry() asserts
the shape, type constraints, and lifecycle state. Mismatches raise
SchemaViolation with a human-readable diff.

Entry shape (LOCKED as of feat/ws-execution-stream):

    {
      "placed_at":  ISO 8601 str,
      "pair":       str (e.g. "SOL/USDC"),
      "side":       "BUY" | "SELL",
      "intent": {
        "amount":       int|float,
        "limit_price":  int|float|None,
        "post_only":    bool,
        "order_type":   str,
        "paper":        bool,
      },
      "decision": {
        "strategy":                  str|None,
        "regime":                    str|None,
        "reason":                    str|None,
        "confidence":                int|float|None,
        "params_at_entry":           dict|None,
        "cross_pair_override":       dict|None,
        "book_confidence_modifier":  int|float|None,
        "brain_verdict":             dict|None,
        "swap_id":                   str|None,
      },
      "order_ref": {
        "order_userref":  int|None,
        "order_id":       str|None,
      },
      "lifecycle": {
        "state":            one of LIFECYCLE_STATES,
        "vol_exec":         int|float,
        "avg_fill_price":   int|float|None,
        "fee_quote":        int|float,
        "final_at":         str|None,
        "terminal_reason":  str|None,
        "exec_ids":         list[str],
      }
    }
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

# ── Sentinels ────────────────────────────────────────────────────────
NOT_NULL = object()
IS_NULL = object()

# ── Lifecycle states (must stay in sync with hydra_agent + migrator) ─
LIFECYCLE_STATES = (
    "PLACED",
    "FILLED",
    "PARTIALLY_FILLED",
    "CANCELLED_UNFILLED",
    "REJECTED",
    "PLACEMENT_FAILED",
)

# Terminal states — used by scenarios that assert an order has finished.
TERMINAL_STATES = (
    "FILLED",
    "PARTIALLY_FILLED",
    "CANCELLED_UNFILLED",
    "REJECTED",
    "PLACEMENT_FAILED",
)


class SchemaViolation(AssertionError):
    pass


# ── Section schemas ─────────────────────────────────────────────────

_INTENT_SCHEMA: dict[str, Any] = {
    "amount": (int, float),
    "limit_price": (int, float, type(None)),
    "post_only": bool,
    "order_type": str,
    "paper": bool,
}

_DECISION_SCHEMA: dict[str, Any] = {
    "strategy": (str, type(None)),
    "regime": (str, type(None)),
    "reason": (str, type(None)),
    "confidence": (int, float, type(None)),
    "params_at_entry": (dict, type(None)),
    "cross_pair_override": (dict, type(None)),
    "book_confidence_modifier": (int, float, type(None)),
    "brain_verdict": (dict, type(None)),
    "swap_id": (str, type(None)),
}

_ORDER_REF_SCHEMA: dict[str, Any] = {
    "order_userref": (int, type(None)),
    "order_id": (str, type(None)),
}

_LIFECYCLE_SCHEMA: dict[str, Any] = {
    "state": str,
    "vol_exec": (int, float),
    "avg_fill_price": (int, float, type(None)),
    "fee_quote": (int, float, type(None)),
    "final_at": (str, type(None)),
    "terminal_reason": (str, type(None)),
    "exec_ids": list,
}


def _check_section(errors: list[str], section_name: str, value: Any,
                    schema: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{section_name!r} must be a dict, got {type(value).__name__}")
        return
    for field, spec in schema.items():
        if field not in value:
            errors.append(f"{section_name}.{field} missing")
            continue
        val = value[field]
        if spec is NOT_NULL:
            if val is None:
                errors.append(f"{section_name}.{field} must not be None")
        elif spec is IS_NULL:
            if val is not None:
                errors.append(f"{section_name}.{field} must be None, got {val!r}")
        elif isinstance(spec, tuple):
            if not isinstance(val, spec):
                errors.append(
                    f"{section_name}.{field} type mismatch: expected {spec}, "
                    f"got {type(val).__name__} ({val!r})"
                )
        else:
            if not isinstance(val, spec):
                errors.append(
                    f"{section_name}.{field} type mismatch: expected {spec.__name__}, "
                    f"got {type(val).__name__} ({val!r})"
                )


def validate_journal_entry(entry: dict[str, Any],
                            expected_state: str = None,
                            *,
                            require_terminal: bool = False) -> None:
    """Validate a single order_journal entry against the new shape.

    Args:
        entry:            the dict to validate
        expected_state:   if set, assert lifecycle.state equals this exactly
        require_terminal: if True, assert lifecycle.state is in TERMINAL_STATES

    Raises:
        SchemaViolation with a full diff on any mismatch.
    """
    if not isinstance(entry, dict):
        raise SchemaViolation(f"Entry is not a dict: {type(entry).__name__}")

    errors: list[str] = []

    # ── Top-level required keys ────────────────────────────────
    for key in ("placed_at", "pair", "side", "intent", "decision",
                "order_ref", "lifecycle"):
        if key not in entry:
            errors.append(f"missing required top-level key: {key!r}")

    if errors:
        raise SchemaViolation(
            "Entry is missing required sections:\n  " + "\n  ".join(errors)
            + f"\nEntry keys: {list(entry.keys())}"
        )

    # ── Top-level types ───────────────────────────────────────
    if not isinstance(entry["placed_at"], str):
        errors.append(f"placed_at must be str, got {type(entry['placed_at']).__name__}")
    else:
        try:
            datetime.fromisoformat(entry["placed_at"].replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"placed_at {entry['placed_at']!r} is not valid ISO 8601")

    if not isinstance(entry["pair"], str):
        errors.append(f"pair must be str, got {type(entry['pair']).__name__}")

    side = entry.get("side")
    if side not in ("BUY", "SELL"):
        errors.append(f"side must be 'BUY' or 'SELL', got {side!r}")

    # ── Section schemas ───────────────────────────────────────
    _check_section(errors, "intent", entry["intent"], _INTENT_SCHEMA)
    _check_section(errors, "decision", entry["decision"], _DECISION_SCHEMA)
    _check_section(errors, "order_ref", entry["order_ref"], _ORDER_REF_SCHEMA)
    _check_section(errors, "lifecycle", entry["lifecycle"], _LIFECYCLE_SCHEMA)

    # ── Lifecycle state is one of the enumerated values ──────
    lifecycle = entry.get("lifecycle") or {}
    state = lifecycle.get("state") if isinstance(lifecycle, dict) else None
    if state not in LIFECYCLE_STATES:
        errors.append(
            f"lifecycle.state must be one of {LIFECYCLE_STATES}, got {state!r}"
        )

    if expected_state is not None and state != expected_state:
        errors.append(
            f"lifecycle.state mismatch: expected {expected_state!r}, got {state!r}"
        )

    if require_terminal and state not in TERMINAL_STATES:
        errors.append(
            f"lifecycle.state {state!r} is not terminal; expected one of {TERMINAL_STATES}"
        )

    # ── Cross-field invariants ───────────────────────────────
    if state == "FILLED":
        vol = lifecycle.get("vol_exec", 0)
        placed_amount = (entry.get("intent") or {}).get("amount", 0)
        if isinstance(vol, (int, float)) and isinstance(placed_amount, (int, float)):
            eps = max(1e-9, placed_amount * 1e-6)
            if abs(vol - placed_amount) > eps:
                errors.append(
                    f"FILLED state requires vol_exec ~= intent.amount, "
                    f"got vol_exec={vol}, amount={placed_amount}"
                )
        if lifecycle.get("avg_fill_price") is None:
            errors.append("FILLED state requires non-null avg_fill_price")

    if state == "PLACEMENT_FAILED":
        if lifecycle.get("vol_exec", 0) != 0:
            errors.append(f"PLACEMENT_FAILED requires vol_exec == 0, got {lifecycle.get('vol_exec')}")
        if not lifecycle.get("terminal_reason"):
            errors.append("PLACEMENT_FAILED requires non-empty terminal_reason")

    if state in ("CANCELLED_UNFILLED", "REJECTED"):
        if not lifecycle.get("terminal_reason"):
            errors.append(f"{state} requires non-empty terminal_reason")
        if lifecycle.get("vol_exec", 0) != 0:
            errors.append(f"{state} requires vol_exec == 0, got {lifecycle.get('vol_exec')}")

    if errors:
        raise SchemaViolation(
            f"Journal entry (state={state!r}) has {len(errors)} violation(s):\n  "
            + "\n  ".join(errors)
            + f"\nEntry: {entry}"
        )


def validate_journal_entries(entries: Iterable[dict[str, Any]]) -> None:
    """Validate every entry in an iterable. Reports all errors at once."""
    failures: list[str] = []
    for i, entry in enumerate(entries):
        try:
            validate_journal_entry(entry)
        except SchemaViolation as e:
            failures.append(f"[{i}] {e}")
    if failures:
        raise SchemaViolation(
            f"{len(failures)} entries failed validation:\n" + "\n".join(failures)
        )


# ── Back-compat alias (scenarios.py still imports the old name) ─────
validate_entry = validate_journal_entry
