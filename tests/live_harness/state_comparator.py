"""Engine rollback completeness comparator.

Verifies that after `engine.restore_position(pre_snap)`, all 13 engine
fields that are supposed to be rolled back exactly match their pre-trade
snapshot. Used by every Category F (failure) scenario.

The 13 fields mirror the set in hydra_engine.py:1077-1110 (snapshot_position
and restore_position), plus engine.halted as a sanity check (halt is not
part of rollback but should not have toggled during a failed order).

Field list comes from commit 4effbea which added equity_history_len,
peak_equity, and max_drawdown to the snapshot — any future additions
must be added here AND in snapshot_position/restore_position for rollback
to remain complete.
"""

from __future__ import annotations

from typing import Any


class RollbackDiff(AssertionError):
    """Raised when a rolled-back engine state does not match its snapshot."""


def capture_engine_state(engine) -> dict[str, Any]:
    """Capture the 13 fields that must be identical before/after a failed order.

    This captures MORE than `snapshot_position()` so the comparator can also
    verify that restore_position correctly restored each field.
    """
    return {
        "balance": engine.balance,
        "position_size": engine.position.size,
        "position_avg_entry": engine.position.avg_entry,
        "position_realized_pnl": engine.position.realized_pnl,
        "position_params_at_entry": _copy_params(engine.position.params_at_entry),
        "total_trades": engine.total_trades,
        "win_count": engine.win_count,
        "loss_count": engine.loss_count,
        "trades_len": len(engine.trades),
        "equity_history_len": len(engine.equity_history),
        "peak_equity": engine.peak_equity,
        "max_drawdown": engine.max_drawdown,
        "halted": engine.halted,
    }


def _copy_params(params) -> Any:
    """params_at_entry may be None or a dict; shallow copy if dict."""
    if params is None:
        return None
    if isinstance(params, dict):
        return dict(params)
    return params


def assert_rollback_complete(before: dict[str, Any], after: dict[str, Any],
                              scenario_name: str = "<unnamed>") -> None:
    """Assert that every field in `after` exactly matches the corresponding
    field in `before`. Raises RollbackDiff with a readable diff on any mismatch.
    """
    mismatches = []
    for field in before:
        if field not in after:
            mismatches.append(f"  {field}: missing from 'after'")
            continue
        b, a = before[field], after[field]
        if _values_equal(b, a):
            continue
        mismatches.append(f"  {field}: before={b!r}, after={a!r}")

    # Sanity check: 'after' should not have extra fields
    for field in after:
        if field not in before:
            mismatches.append(f"  {field}: extra in 'after' (not in 'before')")

    if mismatches:
        raise RollbackDiff(
            f"Rollback incomplete for scenario '{scenario_name}':\n"
            + "\n".join(mismatches)
        )


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values with tolerance for floating-point equality.

    Exact equality is required for ints, bools, strings, and None.
    Floats are compared with a tiny epsilon to tolerate rounding from
    repeated arithmetic (though in rollback this should never apply —
    rollback should restore exact values).
    """
    if type(a) != type(b):
        # Allow int/float comparison if values are the same
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-12
        return False
    if isinstance(a, float):
        return abs(a - b) < 1e-12
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equal(a[k], b[k]) for k in a)
    return a == b


# Convenience wrapper: capture before, run fn, capture after, assert.
def with_rollback_check(engine, scenario_name: str, fn):
    """Run `fn` between two state captures and assert the engine state matches.

    Returns fn's return value. Use this for scenarios where the harness drives
    execute_signal -> _execute_trade -> restore_position and wants a one-liner
    rollback verification.
    """
    before = capture_engine_state(engine)
    result = fn()
    after = capture_engine_state(engine)
    assert_rollback_complete(before, after, scenario_name)
    return result
