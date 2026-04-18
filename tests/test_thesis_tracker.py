"""
HYDRA Thesis Tracker Test Suite (Phase A)
Validates ThesisTracker: defaults, load/save round-trip, snapshot/restore,
knob clamping, posture updates, hard-rule floor protection (0.20 BTC ledger
shield), fail-soft behavior on corrupt state, and HYDRA_THESIS_DISABLED
kill switch.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_thesis import (
    ThesisTracker, Posture, ThesisKnobs, HardRules,
    DEFAULT_LEDGER_SHIELD_BTC, DEFAULT_SIZE_HINT_RANGE,
    DEFAULT_POSTURE_ENFORCEMENT, SIZE_HINT_HARD_BOUNDS,
    CONVICTION_FLOOR_ADJUSTMENT_RANGE, STATE_FILENAME,
    THESIS_SCHEMA_VERSION, DEFAULT_CHECKLIST_KEYS,
)


# ═══════════════════════════════════════════════════════════════
# 1. INITIALIZATION & DEFAULTS
# ═══════════════════════════════════════════════════════════════

class TestInit:
    def test_default_posture_is_preservation(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.posture == Posture.PRESERVATION.value

    def test_default_knobs_match_dataclass(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            knobs = t.knobs
            assert knobs["conviction_floor_adjustment"] == 0.0
            assert knobs["posture_enforcement"] == DEFAULT_POSTURE_ENFORCEMENT
            assert knobs["auto_apply_proposed_updates"] is False

    def test_default_hard_rules_ledger_shield(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.hard_rules["ledger_shield_btc"] == DEFAULT_LEDGER_SHIELD_BTC
            assert t.hard_rules["no_altcoin_gate"] is True

    def test_checklist_has_five_default_keys(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            state = t.current_state()
            assert len(state["checklist"]) == 5
            for key in DEFAULT_CHECKLIST_KEYS:
                assert key in state["checklist"]

    def test_default_schema_version(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.current_state()["version"] == THESIS_SCHEMA_VERSION


# ═══════════════════════════════════════════════════════════════
# 2. PERSISTENCE (load / save round-trip)
# ═══════════════════════════════════════════════════════════════

class TestPersistence:
    def test_save_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.save()
            assert os.path.exists(os.path.join(d, STATE_FILENAME))

    def test_save_load_roundtrip_posture(self):
        with tempfile.TemporaryDirectory() as d:
            t1 = ThesisTracker.load_or_default(save_dir=d)
            t1.update_posture(Posture.TRANSITION.value)
            t2 = ThesisTracker.load_or_default(save_dir=d)
            assert t2.posture == Posture.TRANSITION.value

    def test_save_load_roundtrip_knobs(self):
        with tempfile.TemporaryDirectory() as d:
            t1 = ThesisTracker.load_or_default(save_dir=d)
            t1.update_knobs({
                "conviction_floor_adjustment": 0.07,
                "size_hint_range": [0.7, 1.3],
                "posture_enforcement": "binding",
            })
            t2 = ThesisTracker.load_or_default(save_dir=d)
            assert abs(t2.knobs["conviction_floor_adjustment"] - 0.07) < 1e-9
            assert t2.knobs["size_hint_range"] == [0.7, 1.3]
            assert t2.knobs["posture_enforcement"] == "binding"

    def test_atomic_write_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.save()
            tmp = os.path.join(d, STATE_FILENAME + ".tmp")
            assert not os.path.exists(tmp), "temp file should be atomically renamed"

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, STATE_FILENAME)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.posture == Posture.PRESERVATION.value  # fail-soft

    def test_partial_state_merged_with_defaults(self):
        # A file written by a future version with extra keys must load cleanly,
        # and a file missing keys must backfill from defaults.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, STATE_FILENAME)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"posture": "ACCUMULATION", "future_field": 42}, f)
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.posture == Posture.ACCUMULATION.value
            # Defaults backfilled — knobs block exists
            assert "conviction_floor_adjustment" in t.knobs


# ═══════════════════════════════════════════════════════════════
# 3. SNAPSHOT / RESTORE (for session snapshot integration)
# ═══════════════════════════════════════════════════════════════

class TestSnapshotRestore:
    def test_snapshot_returns_dict(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            snap = t.snapshot()
            assert isinstance(snap, dict)
            assert snap["posture"] == Posture.PRESERVATION.value

    def test_restore_overwrites_state(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            fake_snap = t.snapshot()
            fake_snap["posture"] = Posture.ACCUMULATION.value
            t.restore(fake_snap)
            assert t.posture == Posture.ACCUMULATION.value

    def test_restore_none_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.restore(None)  # should not raise
            assert t.posture == Posture.PRESERVATION.value

    def test_restore_malformed_keeps_current_state(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_posture(Posture.TRANSITION.value)
            # Passing something that looks like a dict but has no relevant keys
            t.restore({"garbage": object()})
            # Should not crash; state stays consistent (posture might revert to default)
            assert t.posture in (p.value for p in Posture)


# ═══════════════════════════════════════════════════════════════
# 4. KNOB CLAMPING / VALIDATION
# ═══════════════════════════════════════════════════════════════

class TestKnobClamping:
    def test_conviction_floor_clamped_low(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_knobs({"conviction_floor_adjustment": -5.0})
            lo, _ = CONVICTION_FLOOR_ADJUSTMENT_RANGE
            assert t.knobs["conviction_floor_adjustment"] == lo

    def test_conviction_floor_clamped_high(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_knobs({"conviction_floor_adjustment": 99.0})
            _, hi = CONVICTION_FLOOR_ADJUSTMENT_RANGE
            assert t.knobs["conviction_floor_adjustment"] == hi

    def test_size_hint_range_reordered(self):
        # If user sends max < min, we swap so [min, max] is always sane.
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_knobs({"size_hint_range": [1.3, 0.7]})
            lo, hi = t.knobs["size_hint_range"]
            assert lo <= hi

    def test_size_hint_range_clamped_to_hard_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_knobs({"size_hint_range": [0.1, 5.0]})
            hard_lo, hard_hi = SIZE_HINT_HARD_BOUNDS
            lo, hi = t.knobs["size_hint_range"]
            assert lo == hard_lo
            assert hi == hard_hi

    def test_posture_enforcement_enum_only(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            prev = t.knobs["posture_enforcement"]
            t.update_knobs({"posture_enforcement": "maximum_override"})  # invalid
            assert t.knobs["posture_enforcement"] == prev
            t.update_knobs({"posture_enforcement": "binding"})  # valid
            assert t.knobs["posture_enforcement"] == "binding"

    def test_unknown_knob_silently_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            before = dict(t.knobs)
            t.update_knobs({"imaginary_future_knob": 99})
            assert t.knobs == before


# ═══════════════════════════════════════════════════════════════
# 5. POSTURE UPDATES
# ═══════════════════════════════════════════════════════════════

class TestPosture:
    def test_update_posture_valid(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.update_posture(Posture.ACCUMULATION.value) is True
            assert t.posture == Posture.ACCUMULATION.value

    def test_update_posture_invalid_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.update_posture("EUPHORIA") is False
            assert t.posture == Posture.PRESERVATION.value


# ═══════════════════════════════════════════════════════════════
# 6. HARD-RULE PROTECTION (ledger shield floor)
# ═══════════════════════════════════════════════════════════════

class TestHardRules:
    def test_ledger_shield_cannot_be_lowered(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_hard_rules({"ledger_shield_btc": 0.05})
            # Floor is DEFAULT_LEDGER_SHIELD_BTC (0.20) — lower values rejected.
            assert t.hard_rules["ledger_shield_btc"] == DEFAULT_LEDGER_SHIELD_BTC

    def test_ledger_shield_can_be_raised(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_hard_rules({"ledger_shield_btc": 0.50})
            assert t.hard_rules["ledger_shield_btc"] == 0.50

    def test_tax_friction_floor_clamped_nonnegative(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_hard_rules({"tax_friction_min_realized_pnl_usd": -10.0})
            assert t.hard_rules["tax_friction_min_realized_pnl_usd"] == 0.0

    def test_no_altcoin_gate_togglable(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.update_hard_rules({"no_altcoin_gate": False})
            assert t.hard_rules["no_altcoin_gate"] is False


# ═══════════════════════════════════════════════════════════════
# 7. KILL SWITCH (HYDRA_THESIS_DISABLED)
# ═══════════════════════════════════════════════════════════════

class TestKillSwitch:
    def test_disabled_returns_inert_tracker(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
            assert t.disabled is True
            assert t.snapshot() == {}

    def test_disabled_save_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
            t.save()
            assert not os.path.exists(os.path.join(d, STATE_FILENAME))

    def test_disabled_update_knobs_noop(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
            result = t.update_knobs({"conviction_floor_adjustment": 0.05})
            assert result["_meta"]["disabled"] is True

    def test_disabled_context_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
            assert t.context_for("BTC/USDC") is None

    def test_disabled_size_hint_is_one(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
            assert t.size_hint_for("BTC/USDC") == 1.0

    def test_env_flag_honored(self):
        # Flag honored when disabled param omitted
        with tempfile.TemporaryDirectory() as d:
            os.environ["HYDRA_THESIS_DISABLED"] = "1"
            try:
                t = ThesisTracker.load_or_default(save_dir=d)
                assert t.disabled is True
            finally:
                del os.environ["HYDRA_THESIS_DISABLED"]


# ═══════════════════════════════════════════════════════════════
# 8. PHASE A INVARIANTS (context + size_hint are inert)
# ═══════════════════════════════════════════════════════════════

class TestPhaseAInvariants:
    """Phase A must not change any live behavior. These tests lock that in."""

    def test_context_for_returns_none_in_phase_a(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.context_for("BTC/USDC", {"action": "BUY"}) is None

    def test_size_hint_is_unity_in_phase_a(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            assert t.size_hint_for("BTC/USDC", {"action": "BUY"}) == 1.0

    def test_on_tick_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            t = ThesisTracker.load_or_default(save_dir=d)
            t.on_tick(1_700_000_000.0)  # must not raise


# ═══════════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    classes = [
        TestInit, TestPersistence, TestSnapshotRestore, TestKnobClamping,
        TestPosture, TestHardRules, TestKillSwitch, TestPhaseAInvariants,
    ]
    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in classes:
        instance = cls()
        for method_name in dir(instance):
            if not method_name.startswith("test_"):
                continue
            total += 1
            method = getattr(instance, method_name)
            try:
                method()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  FAIL  {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  ERROR {cls.__name__}.{method_name}: {e}")

    print(f"\n  {'='*60}")
    print(f"  Thesis Tracker Tests: {passed}/{total} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  FAILURES:")
        for cls_name, method_name, err in errors:
            print(f"    {cls_name}.{method_name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
