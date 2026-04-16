"""
HYDRA Partial-Fill Reconciliation Test Suite (Fix 3)

Validates HydraEngine.reconcile_partial_fill — the method that corrects
engine state after a PARTIALLY_FILLED execution event. At execute_signal
time the engine optimistically commits the full placed_amount; the
exchange may only fill part of it. Without reconciliation the engine
holds phantom inventory, oversizes the next signal, and/or tries to sell
more than it owns.

Two reconciliation paths are tested:
 - SNAPSHOT PATH (current-session): pre_trade_snapshot available; we
   restore then replay only the filled portion — exact match to a world
   where execute_signal had been called with vol_exec.
 - FALLBACK PATH (resume-path): pre_trade_snapshot is None; arithmetic
   reversal of the un-filled delta. Accepts minor avg_entry drift on
   average-in trades.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_engine import HydraEngine, SIZING_COMPETITION


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_engine(balance: float = 1000.0, asset: str = "SOL/USDC") -> HydraEngine:
    e = HydraEngine(initial_balance=balance, asset=asset, sizing=SIZING_COMPETITION)
    # Seed a single "current price" so execute_signal has something to work with
    e.prices = [100.0]
    return e


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) < tol


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

class TestPartialFillReconcile:
    """Fix 3: reconcile_partial_fill corrects optimistic commitments."""

    # ---- BUY, no prior position (fresh entry) ----

    def test_buy_full_fill_is_noop(self):
        """vol_exec == placed → no state change."""
        e = _make_engine(balance=1000.0)
        snap = e.snapshot_position()
        # Simulate execute_signal BUY for 2 @ 100: balance -200, position +2
        e.balance = 800.0
        e.position.size = 2.0
        e.position.avg_entry = 100.0
        before = e.snapshot_position()
        e.reconcile_partial_fill(
            side="BUY", placed_amount=2.0, vol_exec=2.0, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        assert _approx(e.balance, 800.0), f"balance moved: {e.balance}"
        assert _approx(e.position.size, 2.0), f"size moved: {e.position.size}"
        # Nothing changed vs before
        after = e.snapshot_position()
        assert before["balance"] == after["balance"]
        assert before["position_size"] == after["position_size"]

    def test_buy_zero_fill_full_rollback(self):
        """vol_exec == 0 → equivalent to CANCELLED_UNFILLED: restore to snap."""
        e = _make_engine(balance=1000.0)
        snap = e.snapshot_position()
        # Simulate optimistic commit of 2 @ 100
        e.balance = 800.0
        e.position.size = 2.0
        e.position.avg_entry = 100.0
        e.reconcile_partial_fill(
            side="BUY", placed_amount=2.0, vol_exec=0.0, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        assert _approx(e.balance, 1000.0)
        assert _approx(e.position.size, 0.0)
        assert _approx(e.position.avg_entry, 0.0)

    def test_buy_partial_fill_snapshot_path(self):
        """BUY for 2 @ 100, only 0.5 filled. Engine should hold 0.5 @ 100,
        balance = 1000 - 50 = 950."""
        e = _make_engine(balance=1000.0)
        snap = e.snapshot_position()
        e.balance = 800.0
        e.position.size = 2.0
        e.position.avg_entry = 100.0
        e.reconcile_partial_fill(
            side="BUY", placed_amount=2.0, vol_exec=0.5, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        assert _approx(e.balance, 950.0), f"balance={e.balance}"
        assert _approx(e.position.size, 0.5), f"size={e.position.size}"
        assert _approx(e.position.avg_entry, 100.0)

    # ---- BUY, average-in (position already existed) ----

    def test_buy_partial_fill_average_in_snapshot_path(self):
        """Pre-existing position 1 @ 90. BUY for 2 @ 100 (optimistic avg becomes
        (90 + 200)/3 ≈ 96.67). Only 0.5 actually fills. Engine should end up
        with 1.5 @ (90 + 50)/1.5 ≈ 93.33."""
        e = _make_engine(balance=1000.0)
        # Seed a prior position
        e.balance = 910.0
        e.position.size = 1.0
        e.position.avg_entry = 90.0
        snap = e.snapshot_position()
        # Optimistic BUY for 2 @ 100
        e.balance = 710.0
        e.position.size = 3.0
        e.position.avg_entry = (90.0 * 1.0 + 100.0 * 2.0) / 3.0
        # vol_exec = 0.5
        e.reconcile_partial_fill(
            side="BUY", placed_amount=2.0, vol_exec=0.5, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        assert _approx(e.balance, 910.0 - 50.0), f"balance={e.balance}"
        assert _approx(e.position.size, 1.5), f"size={e.position.size}"
        expected_avg = (90.0 * 1.0 + 100.0 * 0.5) / 1.5
        assert _approx(e.position.avg_entry, expected_avg), (
            f"avg_entry={e.position.avg_entry} expected={expected_avg}"
        )

    # ---- BUY, fallback path (no snapshot) ----

    def test_buy_partial_fill_fallback_no_snapshot(self):
        """Arithmetic fallback: refund unfilled × price, reduce size by unfilled.
        avg_entry drift accepted for average-ins — here we use a fresh entry
        so the result is exact."""
        e = _make_engine(balance=1000.0)
        # Simulate post-execute_signal state for a BUY 2 @ 100 that fully
        # optimistically committed (fresh entry)
        e.balance = 800.0
        e.position.size = 2.0
        e.position.avg_entry = 100.0
        e.reconcile_partial_fill(
            side="BUY", placed_amount=2.0, vol_exec=0.5, limit_price=100.0,
            pre_trade_snapshot=None,  # fallback path
        )
        assert _approx(e.balance, 800.0 + 1.5 * 100.0), f"balance={e.balance}"
        assert _approx(e.position.size, 0.5)

    # ---- SELL ----

    def test_sell_full_fill_is_noop(self):
        """SELL for 1 @ 100 fully fills → reconcile does nothing."""
        e = _make_engine(balance=1000.0)
        # Seed a position
        e.balance = 910.0
        e.position.size = 1.0
        e.position.avg_entry = 90.0
        snap = e.snapshot_position()
        # Optimistic SELL for 1 @ 100 — simulated post-commit state
        e.balance = 1010.0
        e.position.size = 0.0
        e.position.avg_entry = 0.0
        before = e.snapshot_position()
        e.reconcile_partial_fill(
            side="SELL", placed_amount=1.0, vol_exec=1.0, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        after = e.snapshot_position()
        assert before["balance"] == after["balance"]
        assert before["position_size"] == after["position_size"]

    def test_sell_zero_fill_full_rollback(self):
        """SELL for 1 @ 100 with vol_exec=0 → restore fully to snapshot."""
        e = _make_engine(balance=1000.0)
        e.balance = 910.0
        e.position.size = 1.0
        e.position.avg_entry = 90.0
        snap = e.snapshot_position()
        # Optimistic SELL commit
        e.balance = 1010.0
        e.position.size = 0.0
        e.position.avg_entry = 0.0
        e.reconcile_partial_fill(
            side="SELL", placed_amount=1.0, vol_exec=0.0, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        assert _approx(e.balance, 910.0)
        assert _approx(e.position.size, 1.0)
        assert _approx(e.position.avg_entry, 90.0)

    def test_sell_partial_fill_snapshot_path(self):
        """Position 2 @ 90. SELL for 2 @ 100 (full close optimistic). Only 0.5
        fills. Engine should end up holding 1.5 @ 90, balance = 910+50=960."""
        e = _make_engine(balance=1000.0)
        e.balance = 820.0
        e.position.size = 2.0
        e.position.avg_entry = 90.0
        snap = e.snapshot_position()
        # Optimistic SELL for 2 @ 100 (profit (100-90)*2 = 20, revenue 200)
        e.balance = 1020.0
        e.position.size = 0.0
        e.position.avg_entry = 0.0
        e.position.realized_pnl = 20.0
        e.reconcile_partial_fill(
            side="SELL", placed_amount=2.0, vol_exec=0.5, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        # Expected: restored to snap (2 @ 90, balance 820), then SELL of 0.5
        # applied: revenue 50, profit 5, size → 1.5.
        assert _approx(e.balance, 820.0 + 50.0), f"balance={e.balance}"
        assert _approx(e.position.size, 1.5), f"size={e.position.size}"
        assert _approx(e.position.avg_entry, 90.0)
        # Realized pnl accumulates for partial closes (cleared only on full close)
        assert _approx(e.position.realized_pnl, 5.0)

    def test_sell_partial_fill_fallback_no_snapshot(self):
        """SELL for 2 @ 100 optimistically committed. vol_exec=0.5 with no
        snapshot. Arithmetic fallback undoes 1.5 worth of the SELL: balance
        -= 150, position += 1.5."""
        e = _make_engine(balance=1000.0)
        # Simulate post-commit: was 2 @ 90, SELL 2 @ 100 fully committed
        e.balance = 1020.0
        e.position.size = 0.0
        e.position.avg_entry = 0.0
        e.reconcile_partial_fill(
            side="SELL", placed_amount=2.0, vol_exec=0.5, limit_price=100.0,
            pre_trade_snapshot=None,
        )
        assert _approx(e.balance, 1020.0 - 150.0)
        assert _approx(e.position.size, 1.5)

    # ---- Degenerate / defensive ----

    def test_placed_zero_is_noop(self):
        e = _make_engine(balance=1000.0)
        before = e.snapshot_position()
        e.reconcile_partial_fill(
            side="BUY", placed_amount=0.0, vol_exec=0.0, limit_price=100.0,
            pre_trade_snapshot=before,
        )
        after = e.snapshot_position()
        assert before == after

    def test_negative_vol_exec_treated_as_zero(self):
        """Defensive: malformed event with vol_exec<0 → treat as zero-fill."""
        e = _make_engine(balance=1000.0)
        snap = e.snapshot_position()
        # Optimistic commit
        e.balance = 800.0
        e.position.size = 2.0
        e.position.avg_entry = 100.0
        e.reconcile_partial_fill(
            side="BUY", placed_amount=2.0, vol_exec=-0.5, limit_price=100.0,
            pre_trade_snapshot=snap,
        )
        assert _approx(e.balance, 1000.0)
        assert _approx(e.position.size, 0.0)


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    passed, failed, errors = 0, 0, []
    test_classes = [TestPartialFillReconcile]
    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                import traceback
                print(f"  FAIL  {test_name} (error): {e}")
                traceback.print_exc()

    print(f"\n  {'='*60}")
    print(f"  Partial-Fill Reconcile Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
