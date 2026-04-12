"""
HYDRA P&L Reconciliation Test Suite
Validates _reconcile_pnl(): compares journal fill data against Kraken
trades-history to detect volume/fee discrepancies.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import KrakenCLI, HydraAgent


# ═══════════════════════════════════════════════════════════════
# Stub helpers
# ═══════════════════════════════════════════════════════════════

class _StubRun:
    def __init__(self, response):
        self._response = response
        self.calls = []
        self._original = None

    def install(self):
        self._original = KrakenCLI._run
        outer = self

        def fake(args, timeout=20):
            outer.calls.append(list(args))
            if callable(outer._response):
                return outer._response(args)
            return outer._response

        KrakenCLI._run = staticmethod(fake)

    def restore(self):
        if self._original is not None:
            KrakenCLI._run = staticmethod(self._original)
            self._original = None


def _make_agent(journal=None):
    agent = object.__new__(HydraAgent)
    agent.order_journal = journal if journal is not None else []
    return agent


def _make_filled_entry(order_id="TX_A", vol_exec=1.0, fee_quote=0.05):
    return {
        "pair": "SOL/USDC", "side": "BUY",
        "order_ref": {"order_id": order_id, "order_userref": 123},
        "lifecycle": {
            "state": "FILLED",
            "vol_exec": vol_exec,
            "avg_fill_price": 130.0,
            "fee_quote": fee_quote,
            "final_at": "2026-04-12T20:00:00Z",
            "terminal_reason": None,
            "exec_ids": [],
        },
    }


def _make_placed_entry(order_id="TX_P"):
    return {
        "pair": "SOL/USDC", "side": "BUY",
        "order_ref": {"order_id": order_id},
        "lifecycle": {"state": "PLACED", "vol_exec": 0.0},
    }


def _kraken_trade(ordertxid, vol, cost, fee):
    return {
        "ordertxid": ordertxid,
        "pair": "SOLUSDC", "type": "buy",
        "vol": str(vol), "cost": str(cost), "fee": str(fee),
        "price": "130.0",
    }


# ═══════════════════════════════════════════════════════════════
# TESTS
# ═══════════════════════════════════════════════════════════════

class TestPnlReconcile:

    def test_empty_journal_returns_zero(self):
        agent = _make_agent(journal=[])
        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            result = agent._reconcile_pnl()
            assert result["checked"] == 0
            assert stub.calls == []
        finally:
            stub.restore()

    def test_no_filled_entries_returns_zero(self):
        agent = _make_agent(journal=[_make_placed_entry()])
        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            result = agent._reconcile_pnl()
            assert result["checked"] == 0
        finally:
            stub.restore()

    def test_perfect_match(self):
        entry = _make_filled_entry(order_id="TX_OK", vol_exec=1.0, fee_quote=0.05)
        agent = _make_agent(journal=[entry])

        kraken_resp = {
            "count": 1,
            "trades": {"T1": _kraken_trade("TX_OK", 1.0, 130.0, 0.05)},
        }
        stub = _StubRun(kraken_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["checked"] == 1
        assert result["matched"] == 1
        assert result["mismatched"] == 0
        assert result["missing"] == 0

    def test_volume_mismatch(self):
        entry = _make_filled_entry(order_id="TX_VM", vol_exec=1.0, fee_quote=0.05)
        agent = _make_agent(journal=[entry])

        kraken_resp = {
            "count": 1,
            "trades": {"T1": _kraken_trade("TX_VM", 0.8, 104.0, 0.05)},
        }
        stub = _StubRun(kraken_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["mismatched"] == 1
        assert result["details"][0]["status"] == "mismatch"

    def test_fee_mismatch(self):
        entry = _make_filled_entry(order_id="TX_FM", vol_exec=1.0, fee_quote=0.05)
        agent = _make_agent(journal=[entry])

        kraken_resp = {
            "count": 1,
            "trades": {"T1": _kraken_trade("TX_FM", 1.0, 130.0, 0.10)},
        }
        stub = _StubRun(kraken_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["mismatched"] == 1

    def test_missing_from_kraken(self):
        entry = _make_filled_entry(order_id="TX_GONE", vol_exec=1.0)
        agent = _make_agent(journal=[entry])

        kraken_resp = {"count": 0, "trades": {}}
        stub = _StubRun(kraken_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["missing"] == 1
        assert result["details"][0]["status"] == "missing_from_kraken"

    def test_multi_fill_aggregation(self):
        """Two Kraken trades for the same order should be aggregated."""
        entry = _make_filled_entry(order_id="TX_MULTI", vol_exec=2.0, fee_quote=0.10)
        agent = _make_agent(journal=[entry])

        kraken_resp = {
            "count": 2,
            "trades": {
                "T1": _kraken_trade("TX_MULTI", 1.0, 130.0, 0.05),
                "T2": _kraken_trade("TX_MULTI", 1.0, 130.0, 0.05),
            },
        }
        stub = _StubRun(kraken_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["matched"] == 1

    def test_api_error_graceful(self):
        entry = _make_filled_entry(order_id="TX_ERR")
        agent = _make_agent(journal=[entry])

        stub = _StubRun({"error": "EAPI:Rate limit"})
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["checked"] == 0
        assert "error" in result

    def test_unknown_order_id_skipped(self):
        entry = _make_filled_entry(order_id="unknown")
        agent = _make_agent(journal=[entry])

        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            result = agent._reconcile_pnl()
            assert result["checked"] == 0
            assert stub.calls == []
        finally:
            stub.restore()

    def test_mixed_entries(self):
        """Mix of FILLED, PLACED, and CANCELLED — only FILLED checked."""
        filled = _make_filled_entry(order_id="TX_F", vol_exec=1.0, fee_quote=0.05)
        placed = _make_placed_entry(order_id="TX_P")
        cancelled = {
            "pair": "SOL/USDC", "side": "BUY",
            "order_ref": {"order_id": "TX_C"},
            "lifecycle": {"state": "CANCELLED_UNFILLED", "vol_exec": 0.0},
        }
        agent = _make_agent(journal=[filled, placed, cancelled])

        kraken_resp = {
            "count": 1,
            "trades": {"T1": _kraken_trade("TX_F", 1.0, 130.0, 0.05)},
        }
        stub = _StubRun(kraken_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                result = agent._reconcile_pnl()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert result["checked"] == 1
        assert result["matched"] == 1


# ═���═════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [TestPnlReconcile]

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
                print(f"  FAIL  {test_name} (error): {e}")

    print(f"\n  {'='*60}")
    print(f"  P&L Reconcile Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
