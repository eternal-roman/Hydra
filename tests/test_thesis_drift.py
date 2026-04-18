"""
HYDRA Thesis Drift Regression Test (Phase B invariant)

Locks the drift contract: HYDRA_THESIS_DISABLED=1 produces v2.12.5
bit-identical behavior. Default-enabled Phase B now surfaces real thesis
context to the brain — but size_hint stays 1.0 under the default advisory
enforcement, so *live sizing and placement* remain unchanged. Only
binding enforcement (opt-in, Phase E) can alter sizes.

Specifically verifies:
1. ThesisTracker(disabled=True) returns context_for = None, size_hint = 1.0
2. Default ThesisTracker (advisory) returns a real context (not None) but
   size_hint stays 1.0 — brain augmentation without sizing change.
3. Flipping enforcement to "binding" with the default size_hint_range and
   PRESERVATION posture begins to move size_hint away from 1.0. That path
   is opt-in; it is NOT exercised on default installs.
4. on_tick does not mutate observable state in either mode.
5. Loading hydra_thesis does not write any files to the save_dir.
6. No thesis subdir appears until explicitly used in later phases.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_thesis import (
    ThesisTracker, STATE_FILENAME, THESIS_SCHEMA_VERSION,
    DOCUMENTS_DIRNAME, PROCESSED_DIRNAME, PENDING_DIRNAME,
    EVIDENCE_ARCHIVE_DIRNAME,
)


def _assert_fully_inert(t: ThesisTracker, label: str):
    """Disabled mode contract: context_for is None, size_hint is 1.0."""
    assert t.context_for("BTC/USDC") is None, f"{label}: context_for must be None"
    assert t.context_for("SOL/USDC", {"action": "BUY"}) is None, f"{label}: context_for must be None"
    assert t.size_hint_for("BTC/USDC") == 1.0, f"{label}: size_hint must be 1.0"
    assert t.size_hint_for("SOL/USDC", {"action": "BUY"}) == 1.0, f"{label}: size_hint must be 1.0"
    t.on_tick(1_700_000_000.0)
    assert t.context_for("BTC/USDC") is None
    assert t.size_hint_for("BTC/USDC") == 1.0


def _assert_sizing_invariant(t: ThesisTracker, label: str):
    """Phase B augmentation contract: context_for returns real data but
    size_hint stays 1.0 under default advisory enforcement — so live
    placement math is unchanged from v2.12.5."""
    ctx = t.context_for("BTC/USDC", {"action": "BUY"})
    assert ctx is not None, f"{label}: context_for must surface a ThesisContext"
    assert t.size_hint_for("BTC/USDC") == 1.0, f"{label}: size_hint must be 1.0 under advisory"
    assert t.size_hint_for("SOL/USDC", {"action": "BUY"}) == 1.0, f"{label}: size_hint must be 1.0"


def test_disabled_is_fully_inert():
    with tempfile.TemporaryDirectory() as d:
        disabled = ThesisTracker.load_or_default(save_dir=d, disabled=True)
        _assert_fully_inert(disabled, "disabled")


def test_default_enforcement_preserves_sizing():
    with tempfile.TemporaryDirectory() as d:
        default = ThesisTracker.load_or_default(save_dir=d, disabled=False)
        _assert_sizing_invariant(default, "default-advisory")


def test_binding_enforcement_begins_moving_sizing():
    """Proves the opt-in path: only flipping to binding changes sizing.
    This is the Phase E entry gate — off by default."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d, disabled=False)
        t.update_knobs({"posture_enforcement": "binding"})
        # Default size_hint_range is (0.85, 1.15) and default posture is PRESERVATION
        # → size_hint should be the low end, 0.85.
        hint = t.size_hint_for("BTC/USDC")
        assert hint != 1.0, "binding mode must begin moving size_hint"
        assert 0.84 < hint < 0.86, f"expected ~0.85 under PRESERVATION, got {hint}"


def test_import_creates_no_files():
    with tempfile.TemporaryDirectory() as d:
        # Merely loading should not write anything.
        t = ThesisTracker.load_or_default(save_dir=d)
        files_after_load = set(os.listdir(d))
        assert files_after_load == set(), f"load should not touch disk; found: {files_after_load}"
        # on_tick also silent in Phase A
        t.on_tick(1.0)
        files_after_tick = set(os.listdir(d))
        assert files_after_tick == set(), f"on_tick should be silent in Phase A; found: {files_after_tick}"


def test_disabled_save_never_writes():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
        t.save()
        t.update_knobs({"conviction_floor_adjustment": 0.1})
        t.update_posture("ACCUMULATION")
        # No state file appears in disabled mode
        assert not os.path.exists(os.path.join(d, STATE_FILENAME))


def test_explicit_save_creates_only_one_file():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.save()
        files = set(os.listdir(d))
        # Must have exactly hydra_thesis.json (and no .tmp left behind).
        assert files == {STATE_FILENAME}
        # Subdirs are lazy: not created on plain save.
        for sub in (DOCUMENTS_DIRNAME, PROCESSED_DIRNAME, PENDING_DIRNAME, EVIDENCE_ARCHIVE_DIRNAME):
            assert not os.path.exists(os.path.join(d, sub))


def test_schema_version_stable():
    assert THESIS_SCHEMA_VERSION == "1.0.0"


def run_tests():
    fns = [
        test_disabled_is_fully_inert,
        test_default_enforcement_preserves_sizing,
        test_binding_enforcement_begins_moving_sizing,
        test_import_creates_no_files,
        test_disabled_save_never_writes,
        test_explicit_save_creates_only_one_file,
        test_schema_version_stable,
    ]
    passed = 0
    failed = 0
    errors = []
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

    print(f"\n  {'='*60}")
    print(f"  Thesis Drift Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")
    if errors:
        print("\n  FAILURES:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
