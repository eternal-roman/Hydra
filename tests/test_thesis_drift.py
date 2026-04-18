"""
HYDRA Thesis Drift Regression Test (Phase A invariant)

Locks the Phase A contract: HYDRA_THESIS_DISABLED=1 and default-enabled
produce bit-identical live behavior because Phase A doesn't wire thesis into
the tick path. Any future phase that begins influencing the tick MUST
preserve this for the disabled case.

Specifically verifies:
1. ThesisTracker(disabled=True) returns context_for = None, size_hint = 1.0
2. Default ThesisTracker (no knob changes) ALSO returns context_for = None
   and size_hint = 1.0 in Phase A — because the brain wiring has not landed.
3. on_tick does not mutate any observable state in either mode.
4. Importing hydra_thesis does not write any files to the save_dir.
5. No thesis-owned state files appear on disk until ThesisTracker.save() is
   explicitly called by a user-initiated knob update.

Subsequent phases will extend this test to exercise the full tick path and
compare resulting order journals bit-for-bit between disabled and default.
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


def _assert_inert(t: ThesisTracker, label: str):
    assert t.context_for("BTC/USDC") is None, f"{label}: context_for must be None in Phase A"
    assert t.context_for("SOL/USDC", {"action": "BUY"}) is None, f"{label}: context_for must be None in Phase A"
    assert t.size_hint_for("BTC/USDC") == 1.0, f"{label}: size_hint must be 1.0 in Phase A"
    assert t.size_hint_for("SOL/USDC", {"action": "BUY"}) == 1.0, f"{label}: size_hint must be 1.0 in Phase A"
    # on_tick must not raise and must not flip inertness
    t.on_tick(1_700_000_000.0)
    assert t.context_for("BTC/USDC") is None
    assert t.size_hint_for("BTC/USDC") == 1.0


def test_disabled_vs_default_are_both_inert():
    with tempfile.TemporaryDirectory() as d:
        disabled = ThesisTracker.load_or_default(save_dir=d, disabled=True)
        default = ThesisTracker.load_or_default(save_dir=d, disabled=False)
        _assert_inert(disabled, "disabled")
        _assert_inert(default, "default-enabled")


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
        test_disabled_vs_default_are_both_inert,
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
