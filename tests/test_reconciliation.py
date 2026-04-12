"""
HYDRA Reconciliation Test Suite
Validates ExecutionStream.reconcile_restart_gap(): query-orders-based
recovery for orders that finalized while the WS stream was down.
Also validates drain_events integration and ensure_healthy triggering.
"""

import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import KrakenCLI, ExecutionStream


# ═══════════════════════════════════════════════════════════════
# Stub helpers
# ═══════════════════════════════════════════════════���═══════════

class _StubRun:
    """Records calls and returns preset response (dict | callable)."""
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


def _make_stream(paper=False) -> ExecutionStream:
    """Create an ExecutionStream with minimal state for reconciliation tests."""
    es = ExecutionStream(paper=paper)
    return es


class _DummyEngine:
    """Placeholder to satisfy engine_ref in known_orders."""
    pass


def _register_order(es, order_id, pair="SOL/USDC", side="BUY",
                     placed_amount=1.0, journal_index=0):
    """Register a known order in the ExecutionStream."""
    userref = int(time.time() * 1000) & 0x7FFFFFFF
    engine = _DummyEngine()
    pre_snap = {"balance": 100.0, "position_size": 0.0}
    es.register(
        order_id=order_id, userref=userref, journal_index=journal_index,
        pair=pair, side=side, placed_amount=placed_amount,
        engine_ref=engine, pre_trade_snapshot=pre_snap,
    )
    return engine, pre_snap


# All required keys in a terminal event from drain_events / reconcile
TERMINAL_EVENT_KEYS = {
    "order_id", "journal_index", "engine_ref", "pre_trade_snapshot",
    "placed_amount", "pair", "side", "state", "vol_exec",
    "avg_fill_price", "fee_quote", "terminal_reason", "exec_ids",
    "timestamp",
}


# ═══════════════════════════════════════════════════════════════
# TESTS: reconcile_restart_gap
# ═══════════════════════════════════════════════════════════════

class TestReconcileRestartGap:

    def test_no_known_orders_returns_empty(self):
        es = _make_stream()
        # Stub not needed — no API calls should be made
        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            events = es.reconcile_restart_gap()
            assert events == []
            assert stub.calls == [], "no API calls expected when _known_orders is empty"
        finally:
            stub.restore()

    def test_paper_mode_returns_empty(self):
        es = _make_stream(paper=True)
        _register_order(es, "TXID1")
        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            events = es.reconcile_restart_gap()
            assert events == []
            assert stub.calls == [], "paper mode should not query API"
        finally:
            stub.restore()

    def test_filled_order(self):
        """Closed order with vol_exec == placed → FILLED."""
        es = _make_stream()
        engine, snap = _register_order(es, "TX_FILL", placed_amount=1.0, journal_index=5)

        resp = {"TX_FILL": {
            "status": "closed", "vol_exec": "1.0", "price": "130.50",
            "fee": "0.065", "closetm": "2026-04-12T20:00:00Z",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            # Patch time.sleep to avoid real delays
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert len(events) == 1
        ev = events[0]
        assert ev["order_id"] == "TX_FILL"
        assert ev["state"] == "FILLED"
        assert ev["vol_exec"] == 1.0
        assert ev["avg_fill_price"] == 130.50
        assert ev["fee_quote"] == 0.065
        assert ev["journal_index"] == 5
        assert ev["engine_ref"] is engine
        assert ev["pre_trade_snapshot"] is snap
        assert "reconciled" in ev["terminal_reason"]
        assert ev["exec_ids"] == []
        # Order should be removed from _known_orders
        assert "TX_FILL" not in es._known_orders

    def test_partially_filled_closed(self):
        """Closed order with vol_exec < placed → PARTIALLY_FILLED."""
        es = _make_stream()
        _register_order(es, "TX_PART", placed_amount=2.0)

        resp = {"TX_PART": {
            "status": "closed", "vol_exec": "0.5", "price": "130.00",
            "fee": "0.032", "closetm": "...",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert len(events) == 1
        assert events[0]["state"] == "PARTIALLY_FILLED"
        assert events[0]["vol_exec"] == 0.5

    def test_cancelled_unfilled(self):
        """Canceled order with vol_exec == 0 → CANCELLED_UNFILLED."""
        es = _make_stream()
        _register_order(es, "TX_CANC", placed_amount=1.0)

        resp = {"TX_CANC": {
            "status": "canceled", "vol_exec": "0", "price": "0",
            "fee": "0", "closetm": "...",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert len(events) == 1
        assert events[0]["state"] == "CANCELLED_UNFILLED"
        assert events[0]["avg_fill_price"] is None

    def test_cancelled_partial(self):
        """Canceled order with vol_exec > 0 → PARTIALLY_FILLED."""
        es = _make_stream()
        _register_order(es, "TX_CP", placed_amount=1.0)

        resp = {"TX_CP": {
            "status": "canceled", "vol_exec": "0.3", "price": "131.00",
            "fee": "0.01",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert len(events) == 1
        assert events[0]["state"] == "PARTIALLY_FILLED"
        assert events[0]["vol_exec"] == 0.3

    def test_still_open_ignored(self):
        """Open orders should remain in _known_orders, no event emitted."""
        es = _make_stream()
        _register_order(es, "TX_OPEN", placed_amount=1.0)

        resp = {"TX_OPEN": {
            "status": "open", "vol_exec": "0", "price": "0", "fee": "0",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert events == []
        assert "TX_OPEN" in es._known_orders

    def test_unknown_order_id_skipped(self):
        """'unknown' order IDs should not be queried."""
        es = _make_stream()
        # Manually inject an "unknown" entry
        with es._lock:
            es._known_orders["unknown"] = {"placed_amount": 1.0}

        stub = _StubRun({"error": "should not be called"})
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert events == []
        assert stub.calls == [], "'unknown' IDs should not trigger API calls"

    def test_api_error_graceful(self):
        """API error should return empty list, not crash."""
        es = _make_stream()
        _register_order(es, "TX_ERR")

        stub = _StubRun({"error": "EAPI:Rate limit"})
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert events == []
        # Order remains for next reconcile attempt
        assert "TX_ERR" in es._known_orders

    def test_batching(self):
        """25 orders should produce 2 API calls (batch of 20 + 5)."""
        es = _make_stream()
        for i in range(25):
            _register_order(es, f"TX_{i:03d}", journal_index=i)

        # All orders still "open" — no terminal events
        def query_resp(args):
            result = {}
            for a in args:
                if a.startswith("TX_"):
                    result[a] = {"status": "open", "vol_exec": "0", "price": "0", "fee": "0"}
            return result

        stub = _StubRun(query_resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert events == []
        # Should have made exactly 2 query-orders calls
        query_calls = [c for c in stub.calls if c[0] == "query-orders"]
        assert len(query_calls) == 2
        # First batch: query-orders + 20 txids + --trades
        assert len(query_calls[0]) == 1 + 20 + 1  # cmd + 20 txids + --trades
        # Second batch: query-orders + 5 txids + --trades
        assert len(query_calls[1]) == 1 + 5 + 1

    def test_event_shape_matches_terminal(self):
        """Reconciled events must have all keys that drain_events events have."""
        es = _make_stream()
        _register_order(es, "TX_SHAPE", placed_amount=1.0, journal_index=0)

        resp = {"TX_SHAPE": {
            "status": "closed", "vol_exec": "1.0", "price": "130.00",
            "fee": "0.05", "closetm": "2026-04-12T20:00:00Z",
        }}
        stub = _StubRun(resp)
        stub.install()
        try:
            orig_sleep = time.sleep
            time.sleep = lambda *a, **kw: None
            try:
                events = es.reconcile_restart_gap()
            finally:
                time.sleep = orig_sleep
        finally:
            stub.restore()

        assert len(events) == 1
        ev = events[0]
        missing = TERMINAL_EVENT_KEYS - set(ev.keys())
        assert not missing, f"missing keys in reconciled event: {missing}"


# ═══════════════════════════════════════════════════════════════
# TESTS: drain_events integration with _pending_reconciliation
# ═══════════════════════════════════════════════════════════════

class TestDrainEventsReconciliation:

    def test_drain_returns_reconciled_first(self):
        """_pending_reconciliation events should come before WS queue events."""
        es = _make_stream()
        # Inject a reconciled event
        recon_event = {"order_id": "RECON_1", "state": "FILLED", "vol_exec": 1.0,
                       "journal_index": 0, "engine_ref": None, "pre_trade_snapshot": {},
                       "placed_amount": 1.0, "pair": "SOL/USDC", "side": "BUY",
                       "avg_fill_price": 130.0, "fee_quote": 0.05,
                       "terminal_reason": "reconciled", "exec_ids": [], "timestamp": "..."}
        es._pending_reconciliation.append(recon_event)

        events = es.drain_events()
        assert len(events) == 1
        assert events[0]["order_id"] == "RECON_1"
        # Buffer should be cleared
        assert es._pending_reconciliation == []

    def test_drain_clears_buffer(self):
        """After draining, _pending_reconciliation should be empty."""
        es = _make_stream()
        es._pending_reconciliation.append({"order_id": "X"})
        es._pending_reconciliation.append({"order_id": "Y"})
        events = es.drain_events()
        assert len(events) == 2
        assert es._pending_reconciliation == []

    def test_drain_empty_reconciliation_works(self):
        """Normal drain_events with no reconciliation buffer works unchanged."""
        es = _make_stream()
        events = es.drain_events()
        assert events == []


# ═══════════════════════════════════════════════════════════════
# TESTS: ensure_healthy triggers reconciliation
# ═══════════════════════════════════════════════════════════════

class _FakeProc:
    """Minimal subprocess mock."""
    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False
        self.stdout = None
        self.stderr = None
        self.pid = 99999

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


class _LiveDaemon(threading.Thread):
    """Mock thread that reports as alive."""
    def __init__(self):
        super().__init__(daemon=True)
        self._started_event = threading.Event()
        self._started_event.set()

    def is_alive(self):
        return True


class TestEnsureHealthyReconciliation:

    def test_ensure_healthy_calls_reconcile_on_restart(self):
        """After a successful restart, reconcile_restart_gap should be called."""
        es = _make_stream()
        # Set up as unhealthy (subprocess exited)
        es._proc = _FakeProc(rc=1)
        es._reader_thread = _LiveDaemon()
        es._last_heartbeat = time.monotonic()

        # Track reconcile calls
        reconcile_calls = []
        original_reconcile = es.reconcile_restart_gap

        def fake_reconcile():
            reconcile_calls.append(True)
            return []

        es.reconcile_restart_gap = fake_reconcile

        # Mock start() to succeed and make the stream healthy
        start_calls = []
        original_start = es.start

        def fake_start():
            start_calls.append(True)
            es._proc = _FakeProc(rc=None)
            es._reader_thread = _LiveDaemon()
            es._last_heartbeat = time.monotonic()
            return True

        es.start = fake_start

        # Mock stop()
        original_stop = es.stop
        es.stop = lambda: None

        try:
            healthy, reason = es.ensure_healthy()
            assert healthy
            assert len(start_calls) == 1, "start() should have been called"
            assert len(reconcile_calls) == 1, "reconcile should have been called after restart"
        finally:
            es.reconcile_restart_gap = original_reconcile
            es.start = original_start
            es.stop = original_stop

    def test_reconcile_events_available_via_drain(self):
        """Events from reconcile after restart should appear in drain_events."""
        es = _make_stream()
        es._proc = _FakeProc(rc=1)
        es._reader_thread = _LiveDaemon()
        es._last_heartbeat = time.monotonic()

        fake_event = {"order_id": "RESTART_TX", "state": "FILLED"}

        def fake_reconcile():
            return [fake_event]

        es.reconcile_restart_gap = fake_reconcile

        def fake_start():
            es._proc = _FakeProc(rc=None)
            es._reader_thread = _LiveDaemon()
            es._last_heartbeat = time.monotonic()
            return True

        es.start = fake_start
        es.stop = lambda: None

        try:
            es.ensure_healthy()
            events = es.drain_events()
            assert len(events) == 1
            assert events[0]["order_id"] == "RESTART_TX"
        finally:
            pass  # no persistent state to restore


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════��════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestReconcileRestartGap,
        TestDrainEventsReconciliation,
        TestEnsureHealthyReconciliation,
    ]

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
    print(f"  Reconciliation Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
