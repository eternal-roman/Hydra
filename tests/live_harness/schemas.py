"""Trade log entry schemas per status.

Every trade_log entry produced by _execute_trade or _execute_paper_trade
must conform to one of these schemas. validate_entry() asserts the shape
matches the expected status. Mismatches raise SchemaViolation with a
human-readable diff.

Known gap: TICKER_FAILED and VALIDATION_FAILED entries omit `reason`,
`confidence`, and `order_type` (see hydra_agent.py:1145-1170). This is
intentional — those entries are written before the trade makes it past
pre-flight checks, so those fields haven't been resolved yet. The
schemas explicitly reflect this.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class SchemaViolation(AssertionError):
    pass


# ─────────────────────────────────────────────────────────────────
# Schema definitions — what every entry must contain per status
# ─────────────────────────────────────────────────────────────────

# Field name -> type (or tuple of types). Use `(type(None), X)` for optional-typed.
# Use `NOT_NULL` to assert the value is not None regardless of type.
# Use `IS_NULL` to assert the value IS None.

NOT_NULL = object()
IS_NULL = object()

SCHEMAS: dict[str, dict[str, Any]] = {
    "TICKER_FAILED": {
        "time": str,  # ISO 8601
        "pair": str,
        "action": str,
        "amount": (int, float),
        "price": (int, float),
        "status": str,
        "error": str,
    },
    "VALIDATION_FAILED": {
        "time": str,
        "pair": str,
        "action": str,
        "amount": (int, float),
        "price": (int, float),
        "status": str,
        "error": str,
    },
    "EXECUTED": {
        "time": str,
        "pair": str,
        "action": str,
        "amount": (int, float),
        "price": (int, float),
        "order_type": str,  # must be "limit post-only"
        "reason": str,
        "confidence": (int, float, type(None)),  # may be None if trade dict didn't have it
        "status": str,
        "result": NOT_NULL,  # Kraken response dict
        "error": IS_NULL,
    },
    "FAILED": {
        "time": str,
        "pair": str,
        "action": str,
        "amount": (int, float),
        "price": (int, float),
        "order_type": str,
        "reason": str,
        "confidence": (int, float, type(None)),
        "status": str,
        "result": IS_NULL,
        "error": NOT_NULL,
    },
    "PAPER_EXECUTED": {
        "time": str,
        "pair": str,
        "action": str,
        "amount": (int, float),
        "price": (int, float),
        "order_type": str,  # must be "paper market"
        "reason": str,
        "confidence": (int, float, type(None)),
        "status": str,
        "result": NOT_NULL,
        "error": IS_NULL,
    },
    "PAPER_FAILED": {
        "time": str,
        "pair": str,
        "action": str,
        "amount": (int, float),
        "price": (int, float),
        "order_type": str,
        "reason": str,
        "confidence": (int, float, type(None)),
        "status": str,
        "result": IS_NULL,
        "error": NOT_NULL,
    },
    "COORDINATED_SWAP": {
        "time": str,
        "type": str,  # must be "COORDINATED_SWAP"
        "swap_id": (int, str),
        "sell_pair": str,
        "buy_pair": str,
        "sell_amount": (int, float),
        "buy_amount": (int, float),
        "reason": str,
    },
}

# Discriminator: how to look up the right schema for an entry.
def schema_for(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if entry.get("type") == "COORDINATED_SWAP":
        return "COORDINATED_SWAP", SCHEMAS["COORDINATED_SWAP"]
    status = entry.get("status")
    if status in SCHEMAS:
        return status, SCHEMAS[status]
    raise SchemaViolation(
        f"Cannot determine schema: entry has no recognized status or type. "
        f"Got status={status!r}, type={entry.get('type')!r}. "
        f"Known statuses: {list(SCHEMAS.keys())}"
    )


# ─────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────

def validate_entry(entry: dict[str, Any], expected_status: str = None) -> None:
    """Validate a single trade_log entry against its schema.

    If expected_status is provided, also asserts the entry's status matches.
    Raises SchemaViolation with a full diff on any mismatch.
    """
    if not isinstance(entry, dict):
        raise SchemaViolation(f"Entry is not a dict: {type(entry).__name__}")

    status, schema = schema_for(entry)

    if expected_status is not None and status != expected_status:
        raise SchemaViolation(
            f"Status mismatch: expected {expected_status!r}, got {status!r}. Entry: {entry}"
        )

    errors = []

    # Check required fields and types
    for field, spec in schema.items():
        if field not in entry:
            errors.append(f"missing required field: {field!r}")
            continue
        val = entry[field]
        if spec is NOT_NULL:
            if val is None:
                errors.append(f"{field!r} must not be None")
        elif spec is IS_NULL:
            if val is not None:
                errors.append(f"{field!r} must be None, got {val!r}")
        elif isinstance(spec, tuple):
            if not isinstance(val, spec):
                errors.append(
                    f"{field!r} type mismatch: expected {spec}, got {type(val).__name__} ({val!r})"
                )
        else:
            if not isinstance(val, spec):
                errors.append(
                    f"{field!r} type mismatch: expected {spec.__name__}, got {type(val).__name__} ({val!r})"
                )

    # Extra-field check: warn if entry has fields the schema doesn't know about.
    # This is advisory, not fatal — new fields may be added without breaking.
    # We only warn; we don't fail on extras.

    # Status-specific assertions
    if status == "EXECUTED":
        if entry.get("order_type") != "limit post-only":
            errors.append(f"order_type for EXECUTED must be 'limit post-only', got {entry.get('order_type')!r}")
    elif status == "FAILED":
        if entry.get("order_type") != "limit post-only":
            errors.append(f"order_type for FAILED must be 'limit post-only', got {entry.get('order_type')!r}")
    elif status in ("PAPER_EXECUTED", "PAPER_FAILED"):
        if entry.get("order_type") != "paper market":
            errors.append(f"order_type for {status} must be 'paper market', got {entry.get('order_type')!r}")
    elif status == "COORDINATED_SWAP":
        if entry.get("type") != "COORDINATED_SWAP":
            errors.append(f"type must be 'COORDINATED_SWAP', got {entry.get('type')!r}")

    # Timestamp format check (ISO 8601 UTC) — common to all statuses
    time_val = entry.get("time")
    if isinstance(time_val, str):
        try:
            datetime.fromisoformat(time_val.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"time {time_val!r} is not valid ISO 8601")

    if errors:
        raise SchemaViolation(
            f"Entry for status {status!r} has {len(errors)} schema violation(s):\n  "
            + "\n  ".join(errors)
            + f"\nEntry: {entry}"
        )


def validate_entries(entries: list[dict[str, Any]]) -> None:
    """Validate every entry in a list. Reports all errors at once."""
    failures = []
    for i, entry in enumerate(entries):
        try:
            validate_entry(entry)
        except SchemaViolation as e:
            failures.append(f"[{i}] {e}")
    if failures:
        raise SchemaViolation(
            f"{len(failures)} of {len(entries)} entries failed validation:\n"
            + "\n".join(failures)
        )
