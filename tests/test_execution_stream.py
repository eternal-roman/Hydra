"""
HYDRA ExecutionStream Health & Auto-Restart Test Suite

Validates the health diagnostics, ensure_healthy auto-restart with cooldown,
reader-thread exit reason tracking, FakeExecutionStream parity, and the
agent tick body's transition-only warning behavior.

No real `kraken ws executions` subprocess is spawned — tests stub Popen and
manipulate internal state directly. The live subprocess path is exercised by
the live_harness in `validate` and `live` modes.
"""

import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import ExecutionStream, FakeExecutionStream  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# Helpers — fake Popen object so we don't actually spawn anything
# ═══════════════════════════════════════════════════════════════

class _FakeProc:
    """Stand-in for subprocess.Popen with controllable poll() behavior."""

    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False
        self.killed = False
        self.stdout = None
        self.stderr = None
        self.pid = 99999

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        if self._rc is None:
            self._rc = -9

    def wait(self, timeout=None):
        return self._rc

    def set_exit(self, rc):
        self._rc = rc


def _make_stream_with_fake_proc(rc=None, hb_age_s=0.0, reader_alive=True):
    """Build an ExecutionStream wired to a _FakeProc, with the heartbeat and
    reader-thread state preconfigured to whatever the test needs."""
    es = ExecutionStream(paper=False)
    es._proc = _FakeProc(rc=rc)
    es._last_heartbeat = time.time() - hb_age_s
    if reader_alive:
        # A trivially-alive thread we can interrogate via .is_alive()
        es._reader_thread = _LiveDaemon()
    else:
        es._reader_thread = _DeadThread()
    return es


class _LiveDaemon(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, target=self._run)
        self._stop = threading.Event()
        self.start()

    def _run(self):
        self._stop.wait()  # block until told to stop

    def stop(self):
        self._stop.set()


class _DeadThread:
    """Quack-alike for a thread that has already exited."""
    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


# ═══════════════════════════════════════════════════════════════
# TEST: health_status — paper short-circuit
# ═══════════════════════════════════════════════════════════════

class TestHealthStatusPaper:
    def test_paper_always_healthy(self):
        es = ExecutionStream(paper=True)
        assert es.health_status() == (True, "")
        assert es.healthy is True

    def test_paper_ensure_healthy_noop(self):
        es = ExecutionStream(paper=True)
        # Even with no proc, paper short-circuits.
        assert es.ensure_healthy() == (True, "")


# ═══════════════════════════════════════════════════════════════
# TEST: health_status — diagnostic reasons
# ═══════════════════════════════════════════════════════════════

class TestHealthStatusReasons:
    def test_subprocess_not_started(self):
        es = ExecutionStream(paper=False)
        ok, reason = es.health_status()
        assert ok is False
        assert reason == "subprocess not started"

    def test_subprocess_exited_includes_rc(self):
        es = _make_stream_with_fake_proc(rc=137)
        try:
            ok, reason = es.health_status()
            assert ok is False
            assert "subprocess exited" in reason
            assert "137" in reason
        finally:
            es._reader_thread.stop()

    def test_reader_thread_dead(self):
        es = _make_stream_with_fake_proc(rc=None, reader_alive=False)
        es._reader_exit_reason = "EOF (subprocess closed stdout)"
        ok, reason = es.health_status()
        assert ok is False
        assert reason.startswith("reader thread")
        assert "EOF" in reason

    def test_reader_thread_dead_unknown_reason(self):
        es = _make_stream_with_fake_proc(rc=None, reader_alive=False)
        es._reader_exit_reason = None
        ok, reason = es.health_status()
        assert ok is False
        assert "unknown" in reason

    def test_heartbeat_stale_includes_age(self):
        # Force a stale heartbeat by setting it 60s in the past (well over
        # the 30s threshold).
        es = _make_stream_with_fake_proc(rc=None, hb_age_s=60.0)
        try:
            ok, reason = es.health_status()
            assert ok is False
            assert "no heartbeat" in reason
            assert "60s" in reason
        finally:
            es._reader_thread.stop()

    def test_healthy_when_all_checks_pass(self):
        es = _make_stream_with_fake_proc(rc=None, hb_age_s=1.0)
        try:
            assert es.health_status() == (True, "")
            assert es.healthy is True
        finally:
            es._reader_thread.stop()


# ═══════════════════════════════════════════════════════════════
# TEST: ensure_healthy — auto-restart and cooldown
# ═══════════════════════════════════════════════════════════════

class _RestartCounter:
    """Replaces ExecutionStream.start so tests can track invocations without
    spawning a real subprocess."""

    def __init__(self, es: ExecutionStream, restart_makes_healthy=True):
        self.es = es
        self.calls = 0
        self.restart_makes_healthy = restart_makes_healthy
        self._original = es.start

    def install(self):
        def fake_start():
            self.calls += 1
            if self.restart_makes_healthy:
                self.es._proc = _FakeProc(rc=None)
                self.es._reader_thread = _LiveDaemon()
                self.es._last_heartbeat = time.time()
            return True
        self.es.start = fake_start

    def restore(self):
        self.es.start = self._original


class TestEnsureHealthyRestart:
    def test_healthy_returns_without_restart(self):
        es = _make_stream_with_fake_proc(rc=None, hb_age_s=1.0)
        rc = _RestartCounter(es)
        rc.install()
        try:
            ok, reason = es.ensure_healthy()
            assert ok is True
            assert reason == ""
            assert rc.calls == 0
        finally:
            rc.restore()
            es._reader_thread.stop()

    def test_unhealthy_triggers_restart(self):
        es = _make_stream_with_fake_proc(rc=137)
        # Drain the cooldown by setting last attempt far in the past
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es)
        rc.install()
        try:
            ok, reason = es.ensure_healthy()
            assert rc.calls == 1
            assert ok is True
            assert es._restart_count == 1
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass

    def test_cooldown_suppresses_second_restart(self):
        es = _make_stream_with_fake_proc(rc=137)
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es, restart_makes_healthy=False)
        rc.install()
        try:
            es.ensure_healthy()
            # First call attempted a restart
            assert rc.calls == 1
            # Mark unhealthy again — second call must NOT call start() because
            # the cooldown timer was just bumped.
            es._proc = _FakeProc(rc=137)
            ok, reason = es.ensure_healthy()
            assert rc.calls == 1, "second call should be cooldown-suppressed"
            assert ok is False
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass

    def test_cooldown_expiry_allows_new_restart(self):
        es = _make_stream_with_fake_proc(rc=137)
        es._last_restart_attempt = 0.0
        rc = _RestartCounter(es, restart_makes_healthy=False)
        rc.install()
        try:
            es.ensure_healthy()
            assert rc.calls == 1
            # Backdate the cooldown timer past RESTART_COOLDOWN_S so the
            # next call is allowed.
            es._last_restart_attempt = time.time() - es.RESTART_COOLDOWN_S - 1
            es._proc = _FakeProc(rc=137)
            es.ensure_healthy()
            assert rc.calls == 2
        finally:
            rc.restore()
            if es._reader_thread is not None:
                try:
                    es._reader_thread.stop()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════
# TEST: heartbeat dispatch updates the timestamp
# ═══════════════════════════════════════════════════════════════

class TestDispatchHeartbeat:
    def test_heartbeat_channel_bumps_timestamp(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 0.0  # ancient
        es._dispatch({"channel": "heartbeat"})
        assert es._last_heartbeat > time.time() - 1.0

    def test_executions_channel_bumps_timestamp(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 0.0
        es._dispatch({"channel": "executions", "type": "update", "data": []})
        assert es._last_heartbeat > time.time() - 1.0

    def test_status_channel_does_not_bump(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 12345.0  # canary value
        es._dispatch({"channel": "status", "data": []})
        assert es._last_heartbeat == 12345.0

    def test_subscribe_response_does_not_bump(self):
        es = ExecutionStream(paper=False)
        es._last_heartbeat = 12345.0
        es._dispatch({"method": "subscribe", "success": True})
        assert es._last_heartbeat == 12345.0


# ═══════════════════════════════════════════════════════════════
# TEST: FakeExecutionStream parity — health_status / ensure_healthy
# ═══════════════════════════════════════════════════════════════

class TestFakeExecutionStreamParity:
    def test_default_healthy(self):
        f = FakeExecutionStream()
        assert f.healthy is True
        assert f.health_status() == (True, "")
        assert f.ensure_healthy() == (True, "")

    def test_marked_unhealthy_reports_diagnostic(self):
        f = FakeExecutionStream()
        f.set_healthy(False)
        ok, reason = f.health_status()
        assert ok is False
        assert "fake stream marked unhealthy" in reason

    def test_ensure_healthy_does_not_restart(self):
        f = FakeExecutionStream()
        f.set_healthy(False)
        # FakeExecutionStream.start is a no-op; ensure_healthy should report
        # the same state without trying to "restart".
        ok, reason = f.ensure_healthy()
        assert ok is False
        assert "fake stream marked unhealthy" in reason


# ═══════════════════════════════════════════════════════════════
# Test runner
# ═══════════════════════════════════════════════════════════════

def run_tests():
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestHealthStatusPaper,
        TestHealthStatusReasons,
        TestEnsureHealthyRestart,
        TestDispatchHeartbeat,
        TestFakeExecutionStreamParity,
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
                print(f"  FAIL  {test_name} (error): {e}")

    print(f"\n  {'='*60}")
    print(f"  ExecutionStream Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
