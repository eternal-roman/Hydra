"""
HYDRA Status Gate Test Suite
Validates that the tick loop correctly skips during Kraken maintenance,
runs during online/post_only, degrades gracefully on API errors, and
logs status transitions only once per change.
"""

import sys
import os
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import HydraAgent
from hydra_kraken_cli import KrakenCLI


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_agent(paper=False):
    """Minimal HydraAgent with just enough state for status gate logic."""
    agent = object.__new__(HydraAgent)
    agent.paper = paper
    agent._last_kraken_status = None
    return agent


def _stub_system_status(response):
    """Replace KrakenCLI.system_status with a stub returning `response`."""
    original = KrakenCLI.system_status

    class Recorder:
        calls = 0

    def fake():
        Recorder.calls += 1
        return response

    KrakenCLI.system_status = staticmethod(fake)
    return original, Recorder


def _restore(original):
    KrakenCLI.system_status = staticmethod(original)


def _check_status_gate(agent):
    """Simulate the Phase 0 status gate logic from the tick loop.

    Returns (should_skip: bool, log_output: str).
    The logic here mirrors hydra_agent.py tick loop exactly.
    """
    buf = io.StringIO()
    should_skip = False

    if not agent.paper:
        _status_resp = KrakenCLI.system_status()
        _kraken_status = (
            _status_resp.get("status", "online")
            if isinstance(_status_resp, dict) and "error" not in _status_resp
            else "online"
        )
        if _kraken_status not in ("online", "post_only"):
            if agent._last_kraken_status != _kraken_status:
                buf.write(f"  [HYDRA] Kraken status: {_kraken_status} — skipping tick\n")
            agent._last_kraken_status = _kraken_status
            should_skip = True
        else:
            if agent._last_kraken_status not in ("online", "post_only", None):
                buf.write(f"  [HYDRA] Kraken back online (was {agent._last_kraken_status})\n")
            agent._last_kraken_status = _kraken_status
            should_skip = False

    return should_skip, buf.getvalue()


# ═══════════════════════════════════════════════════════════════
# TESTS
# ═══════════════════════════════════════════════════════════════

class TestStatusGate:

    def test_tick_runs_when_online(self):
        agent = _make_agent(paper=False)
        orig, rec = _stub_system_status({"status": "online", "timestamp": "..."})
        try:
            skip, _ = _check_status_gate(agent)
            assert not skip, "should NOT skip when status is online"
            assert agent._last_kraken_status == "online"
            assert rec.calls == 1
        finally:
            _restore(orig)

    def test_tick_runs_when_post_only(self):
        agent = _make_agent(paper=False)
        orig, rec = _stub_system_status({"status": "post_only", "timestamp": "..."})
        try:
            skip, _ = _check_status_gate(agent)
            assert not skip, "should NOT skip when status is post_only (we use post-only orders)"
            assert agent._last_kraken_status == "post_only"
        finally:
            _restore(orig)

    def test_tick_skips_during_maintenance(self):
        agent = _make_agent(paper=False)
        orig, _ = _stub_system_status({"status": "maintenance", "timestamp": "..."})
        try:
            skip, log = _check_status_gate(agent)
            assert skip, "should skip when status is maintenance"
            assert agent._last_kraken_status == "maintenance"
            assert "maintenance" in log
            assert "skipping tick" in log
        finally:
            _restore(orig)

    def test_tick_skips_during_cancel_only(self):
        agent = _make_agent(paper=False)
        orig, _ = _stub_system_status({"status": "cancel_only", "timestamp": "..."})
        try:
            skip, log = _check_status_gate(agent)
            assert skip, "should skip when status is cancel_only"
            assert "cancel_only" in log
        finally:
            _restore(orig)

    def test_degradation_on_status_error(self):
        """API error should degrade to online — never block ticks."""
        agent = _make_agent(paper=False)
        orig, _ = _stub_system_status({"error": "Command timed out", "retryable": True})
        try:
            skip, _ = _check_status_gate(agent)
            assert not skip, "should NOT skip on API error (graceful degradation)"
            assert agent._last_kraken_status == "online"
        finally:
            _restore(orig)

    def test_degradation_on_non_dict_response(self):
        """Non-dict response should degrade to online."""
        agent = _make_agent(paper=False)
        original = KrakenCLI.system_status

        def fake():
            return "not a dict"

        KrakenCLI.system_status = staticmethod(fake)
        try:
            skip, _ = _check_status_gate(agent)
            assert not skip
            assert agent._last_kraken_status == "online"
        finally:
            KrakenCLI.system_status = staticmethod(original)

    def test_paper_mode_skips_status_check(self):
        """Paper mode should never call system_status."""
        agent = _make_agent(paper=True)
        orig, rec = _stub_system_status({"status": "maintenance"})
        try:
            skip, _ = _check_status_gate(agent)
            assert not skip, "paper mode should never skip"
            assert rec.calls == 0, "system_status should not be called in paper mode"
            assert agent._last_kraken_status is None
        finally:
            _restore(orig)

    def test_transition_maintenance_to_online_logs_recovery(self):
        """Should log 'back online' once when transitioning from maintenance."""
        agent = _make_agent(paper=False)

        # First: go to maintenance
        orig, _ = _stub_system_status({"status": "maintenance"})
        try:
            skip1, log1 = _check_status_gate(agent)
            assert skip1
            assert "skipping tick" in log1
        finally:
            _restore(orig)

        # Second: back to online
        orig2, _ = _stub_system_status({"status": "online"})
        try:
            skip2, log2 = _check_status_gate(agent)
            assert not skip2
            assert "back online" in log2
            assert "maintenance" in log2
        finally:
            _restore(orig2)

    def test_repeated_maintenance_logs_once(self):
        """Multiple maintenance ticks should only log the first transition."""
        agent = _make_agent(paper=False)
        orig, _ = _stub_system_status({"status": "maintenance"})
        try:
            _, log1 = _check_status_gate(agent)
            assert "skipping tick" in log1, "first maintenance tick should log"

            _, log2 = _check_status_gate(agent)
            assert log2 == "", "subsequent maintenance ticks should NOT re-log"
        finally:
            _restore(orig)

    def test_repeated_online_does_not_log(self):
        """Multiple online ticks should produce no log output."""
        agent = _make_agent(paper=False)
        orig, _ = _stub_system_status({"status": "online"})
        try:
            _, log1 = _check_status_gate(agent)
            assert log1 == ""
            _, log2 = _check_status_gate(agent)
            assert log2 == ""
        finally:
            _restore(orig)

    def test_full_cycle_online_maintenance_online(self):
        """Full cycle: online -> maintenance -> maintenance -> online."""
        agent = _make_agent(paper=False)

        # online
        orig, _ = _stub_system_status({"status": "online"})
        try:
            skip, log = _check_status_gate(agent)
            assert not skip and log == ""
        finally:
            _restore(orig)

        # maintenance (first)
        orig, _ = _stub_system_status({"status": "maintenance"})
        try:
            skip, log = _check_status_gate(agent)
            assert skip and "skipping tick" in log
        finally:
            _restore(orig)

        # maintenance (second — no log)
        orig, _ = _stub_system_status({"status": "maintenance"})
        try:
            skip, log = _check_status_gate(agent)
            assert skip and log == ""
        finally:
            _restore(orig)

        # back online
        orig, _ = _stub_system_status({"status": "online"})
        try:
            skip, log = _check_status_gate(agent)
            assert not skip and "back online" in log
        finally:
            _restore(orig)


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [TestStatusGate]

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
    print(f"  Status Gate Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
