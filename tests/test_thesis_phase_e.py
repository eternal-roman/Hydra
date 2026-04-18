"""
HYDRA Thesis Phase E — Opt-in posture enforcement.

Validates the binding-mode daily entry cap surface:

1. Default advisory enforcement → check_posture_restriction always allows.
   Upgrading to v2.13.4 produces zero behavior change for users who keep
   the default knobs.
2. Binding + PRESERVATION + default cap (2) → 3rd entry is restricted.
3. Binding + TRANSITION (default cap 4) → 5th entry is restricted.
4. Binding + ACCUMULATION (default uncapped) → all entries allowed.
5. record_entry increments the per-UTC-day counter; rollover happens at
   UTC midnight (prunes yesterday from state).
6. Custom caps via update_knobs({"max_daily_entries_by_posture": ...}).
7. Kill switch: disabled tracker short-circuits to allow=True.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_thesis import (
    ThesisTracker, Posture, DEFAULT_MAX_DAILY_ENTRIES_BY_POSTURE,
)


# ─── Default mode behavior (zero change on upgrade) ───────────────

def test_default_advisory_always_allows():
    """Users who don't opt in see zero behavior change from Phase E."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        # Record many entries — default advisory must not care
        for _ in range(20):
            t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is True
        assert r["reason"] == ""


def test_off_enforcement_always_allows():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({"posture_enforcement": "off"})
        for _ in range(5):
            t.record_entry("BTC/USDC")
        assert t.check_posture_restriction("BTC/USDC", "BUY")["allow"] is True


# ─── Binding mode — daily caps ─────────────────────────────────────

def test_binding_preservation_default_cap():
    """PRESERVATION default cap is 2 — third entry is restricted."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({"posture_enforcement": "binding"})
        t.update_posture(Posture.PRESERVATION.value)
        t.record_entry("BTC/USDC")
        t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is False
        assert r["cap"] == 2
        assert r["entries_today"] == 2
        assert "preservation" in r["reason"]


def test_binding_transition_default_cap():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({"posture_enforcement": "binding"})
        t.update_posture(Posture.TRANSITION.value)
        for _ in range(4):
            t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is False
        assert r["cap"] == 4


def test_binding_accumulation_uncapped():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({"posture_enforcement": "binding"})
        t.update_posture(Posture.ACCUMULATION.value)
        for _ in range(50):
            t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is True  # default None = uncapped
        assert r["cap"] is None


# ─── Per-pair isolation ────────────────────────────────────────────

def test_cap_is_per_pair():
    """Reaching PRESERVATION cap on BTC/USDC must not restrict SOL/USDC."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({"posture_enforcement": "binding"})
        t.record_entry("BTC/USDC")
        t.record_entry("BTC/USDC")
        r_btc = t.check_posture_restriction("BTC/USDC", "BUY")
        r_sol = t.check_posture_restriction("SOL/USDC", "BUY")
        assert r_btc["allow"] is False
        assert r_sol["allow"] is True
        assert r_sol["entries_today"] == 0


# ─── Custom cap knob ───────────────────────────────────────────────

def test_custom_preservation_cap():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({
            "posture_enforcement": "binding",
            "max_daily_entries_by_posture": {"PRESERVATION": 5},
        })
        for _ in range(5):
            t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is False
        assert r["cap"] == 5


def test_custom_cap_accepts_none_for_uncapped():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({
            "posture_enforcement": "binding",
            "max_daily_entries_by_posture": {"PRESERVATION": None},
        })
        for _ in range(20):
            t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is True


def test_custom_cap_ignores_unknown_posture_keys():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        # Garbage posture name is silently dropped; valid keys still merge
        t.update_knobs({
            "posture_enforcement": "binding",
            "max_daily_entries_by_posture": {
                "EUPHORIA": 99,  # not a valid posture — dropped
                "PRESERVATION": 1,
            },
        })
        t.record_entry("BTC/USDC")
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is False
        assert r["cap"] == 1


# ─── Counter + rollover ────────────────────────────────────────────

def test_record_entry_increments_counter():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        assert t.daily_entries_for("BTC/USDC") == 0
        t.record_entry("BTC/USDC")
        assert t.daily_entries_for("BTC/USDC") == 1
        t.record_entry("BTC/USDC")
        assert t.daily_entries_for("BTC/USDC") == 2
        # Different pair still zero
        assert t.daily_entries_for("SOL/USDC") == 0


def test_rollover_prunes_yesterdays_bucket():
    """record_entry is expected to keep only today's bucket in state."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        # Inject yesterday's bucket manually
        t._state["daily_entries"] = {
            "1999-01-01": {"BTC/USDC": 99},
            t._utc_day_key(): {"BTC/USDC": 1},
        }
        t.record_entry("BTC/USDC")
        de = t._state["daily_entries"]
        assert "1999-01-01" not in de  # pruned
        assert de[t._utc_day_key()]["BTC/USDC"] == 2


# ─── Kill switch ──────────────────────────────────────────────────

def test_disabled_tracker_always_allows():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
        r = t.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is True
        # record_entry is silently ignored
        t.record_entry("BTC/USDC")
        assert t.daily_entries_for("BTC/USDC") == 0


# ─── Persistence ──────────────────────────────────────────────────

def test_cap_state_persists_across_load():
    with tempfile.TemporaryDirectory() as d:
        t1 = ThesisTracker.load_or_default(save_dir=d)
        t1.update_knobs({
            "posture_enforcement": "binding",
            "max_daily_entries_by_posture": {"PRESERVATION": 1},
        })
        t1.record_entry("BTC/USDC")
        # Fresh tracker reads from disk
        t2 = ThesisTracker.load_or_default(save_dir=d)
        r = t2.check_posture_restriction("BTC/USDC", "BUY")
        assert r["allow"] is False
        assert r["cap"] == 1


def run_tests():
    fns = [
        test_default_advisory_always_allows,
        test_off_enforcement_always_allows,
        test_binding_preservation_default_cap,
        test_binding_transition_default_cap,
        test_binding_accumulation_uncapped,
        test_cap_is_per_pair,
        test_custom_preservation_cap,
        test_custom_cap_accepts_none_for_uncapped,
        test_custom_cap_ignores_unknown_posture_keys,
        test_record_entry_increments_counter,
        test_rollover_prunes_yesterdays_bucket,
        test_disabled_tracker_always_allows,
        test_cap_state_persists_across_load,
    ]
    passed = 0; failed = 0; errors = []
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            errors.append((fn.__name__, e))
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            errors.append((fn.__name__, e))
            print(f"  ERROR {fn.__name__}: {e}")
    print(f"\n  {'=' * 60}")
    print(f"  Thesis Phase E Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'=' * 60}")
    if errors:
        print("\n  FAILURES:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
