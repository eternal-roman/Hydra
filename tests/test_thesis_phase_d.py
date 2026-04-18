"""
HYDRA Thesis Phase D — Ladder primitive + rung-aware journal stamping.

Validates:
1. Ladder CRUD (create, list, cancel) with per-pair cap enforcement.
2. match_rung returns a hit when (pair, side, price) is within
   RUNG_PRICE_TOLERANCE_PCT of a PENDING rung's price.
3. HYDRA_THESIS_LADDERS feature flag gates journal-schema changes —
   without it, match_rung returns None and journal entries stay
   v2.13.2-shaped.
4. Rung placement + fill transitions (PENDING → PLACED → FILLED).
   Ladder status advances to FILLED when all rungs terminate.
5. Stop-loss check: BUY ladder with any FILLED rung stops on breach;
   unfilled ladder on breach just cancels (not STOPPED_OUT). Pending
   rungs all flip CANCELLED.
6. Expiry sweep flips ACTIVE ladders past expires_at; pending rungs
   cancel.
7. Disabled / kill-switch: all ladder paths are no-ops.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_thesis import (
    ThesisTracker, LadderStatus, RungStatus,
    DEFAULT_MAX_ACTIVE_LADDERS_PER_PAIR,
)


def _with_ladders_env(fn):
    """Enable HYDRA_THESIS_LADDERS for the duration of a test."""
    def wrapper():
        prev = os.environ.get("HYDRA_THESIS_LADDERS")
        os.environ["HYDRA_THESIS_LADDERS"] = "1"
        try:
            fn()
        finally:
            if prev is None:
                os.environ.pop("HYDRA_THESIS_LADDERS", None)
            else:
                os.environ["HYDRA_THESIS_LADDERS"] = prev
    wrapper.__name__ = fn.__name__
    return wrapper


def _make_ladder(t, **kwargs):
    defaults = dict(
        pair="BTC/USDC", side="BUY", total_size=0.003,
        rungs_spec=[
            {"price": 74000, "size": 0.001},
            {"price": 73500, "size": 0.001},
            {"price": 73000, "size": 0.001},
        ],
        stop_loss_price=72000,
        expiry_hours=24,
        reasoning="test ladder",
    )
    defaults.update(kwargs)
    return t.create_ladder(**defaults)


# ─── CRUD ─────────────────────────────────────────────────────────

@_with_ladders_env
def test_create_ladder_success():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t)
        assert l is not None
        assert l["pair"] == "BTC/USDC"
        assert l["side"] == "BUY"
        assert l["status"] == LadderStatus.ACTIVE.value
        assert len(l["rungs"]) == 3
        assert l["stop_loss_price"] == 72000


@_with_ladders_env
def test_create_ladder_rejects_bad_side():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = t.create_ladder(pair="BTC/USDC", side="HOLD", total_size=0.001,
                            rungs_spec=[{"price": 70000, "size": 0.001}])
        assert l is None


@_with_ladders_env
def test_create_ladder_rejects_empty_rungs():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = t.create_ladder(pair="BTC/USDC", side="BUY",
                            total_size=0.001, rungs_spec=[])
        assert l is None


@_with_ladders_env
def test_cap_per_pair_enforced():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        t.update_knobs({"max_active_ladders_per_pair": 2})
        assert _make_ladder(t) is not None
        assert _make_ladder(t) is not None
        # Third should be rejected
        assert _make_ladder(t) is None


@_with_ladders_env
def test_cancel_ladder():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t)
        assert t.cancel_ladder(l["ladder_id"]) is True
        ladders = t.list_ladders()
        assert ladders[0]["status"] == LadderStatus.CANCELLED.value
        for r in ladders[0]["rungs"]:
            assert r["status"] == RungStatus.CANCELLED.value


@_with_ladders_env
def test_rung_size_scaling_to_total_size():
    """If rung sizes don't sum to total_size, create_ladder scales them."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = t.create_ladder(
            pair="BTC/USDC", side="BUY", total_size=0.010,
            rungs_spec=[
                {"price": 74000, "size": 0.001},
                {"price": 73000, "size": 0.001},
            ],
        )
        sizes = [r["size"] for r in l["rungs"]]
        assert abs(sum(sizes) - 0.010) < 1e-9


# ─── match_rung + feature flag ────────────────────────────────────

@_with_ladders_env
def test_match_rung_hits_within_tolerance():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        _make_ladder(t)
        # Exact match
        match = t.match_rung("BTC/USDC", "BUY", 74000)
        assert match is not None
        assert match["rung_idx"] == 0
        # Within 0.5% tolerance
        match = t.match_rung("BTC/USDC", "BUY", 74000 * 1.003)
        assert match is not None


@_with_ladders_env
def test_match_rung_misses_outside_tolerance():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        _make_ladder(t)
        # > 0.5% off
        assert t.match_rung("BTC/USDC", "BUY", 74000 * 1.01) is None


@_with_ladders_env
def test_match_rung_side_mismatch_returns_none():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        _make_ladder(t)
        assert t.match_rung("BTC/USDC", "SELL", 74000) is None


@_with_ladders_env
def test_match_rung_pair_mismatch_returns_none():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        _make_ladder(t)
        assert t.match_rung("SOL/USDC", "BUY", 74000) is None


def test_match_rung_returns_none_without_feature_flag():
    """Without HYDRA_THESIS_LADDERS set, the ladder path is inert."""
    with tempfile.TemporaryDirectory() as d:
        # Save a ladder under the flag, then read it back WITHOUT the flag
        os.environ["HYDRA_THESIS_LADDERS"] = "1"
        try:
            t = ThesisTracker.load_or_default(save_dir=d)
            _make_ladder(t)
        finally:
            os.environ.pop("HYDRA_THESIS_LADDERS", None)
        # Re-load with the flag unset — match_rung must be None
        t2 = ThesisTracker.load_or_default(save_dir=d)
        assert t2.match_rung("BTC/USDC", "BUY", 74000) is None


# ─── Rung lifecycle ───────────────────────────────────────────────

@_with_ladders_env
def test_rung_placement_transition():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t)
        assert t.record_rung_placement(l["ladder_id"], 0, userref=12345) is True
        ladders = t.list_ladders()
        r0 = ladders[0]["rungs"][0]
        assert r0["status"] == RungStatus.PLACED.value
        assert r0["placed_as_userref"] == 12345


@_with_ladders_env
def test_rung_fill_transition_and_ladder_complete():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t)
        for idx in range(3):
            t.record_rung_placement(l["ladder_id"], idx)
            t.record_rung_fill(l["ladder_id"], idx, filled_price=74000 - idx * 500)
        ladders = t.list_ladders()
        assert ladders[0]["status"] == LadderStatus.FILLED.value
        for r in ladders[0]["rungs"]:
            assert r["status"] == RungStatus.FILLED.value
            assert r["filled_price"] is not None


# ─── Stop-loss ────────────────────────────────────────────────────

@_with_ladders_env
def test_stop_loss_breach_after_fill_marks_stopped_out():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t)
        # Fill the first rung
        t.record_rung_placement(l["ladder_id"], 0)
        t.record_rung_fill(l["ladder_id"], 0, filled_price=74000)
        # Price breaks below stop_loss (72000)
        breached = t.check_stop_loss("BTC/USDC", 71900)
        assert l["ladder_id"] in breached
        ladders = t.list_ladders()
        assert ladders[0]["status"] == LadderStatus.STOPPED_OUT.value
        # Remaining pending rungs cancelled
        for r in ladders[0]["rungs"][1:]:
            assert r["status"] == RungStatus.CANCELLED.value


@_with_ladders_env
def test_stop_loss_breach_without_fill_just_cancels():
    """Athena's distinction: 'stopped out' implies capital committed.
    A ladder that moves against you before any fill just cancels."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t)
        breached = t.check_stop_loss("BTC/USDC", 71900)
        assert l["ladder_id"] in breached
        ladders = t.list_ladders()
        assert ladders[0]["status"] == LadderStatus.CANCELLED.value


@_with_ladders_env
def test_stop_loss_no_breach_no_change():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        _make_ladder(t)
        breached = t.check_stop_loss("BTC/USDC", 73500)
        assert breached == []


@_with_ladders_env
def test_stop_loss_sell_ladder_trips_on_price_rise():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = t.create_ladder(
            pair="BTC/USDC", side="SELL", total_size=0.002,
            rungs_spec=[{"price": 80000, "size": 0.001}, {"price": 81000, "size": 0.001}],
            stop_loss_price=82000,
        )
        t.record_rung_placement(l["ladder_id"], 0)
        t.record_rung_fill(l["ladder_id"], 0, filled_price=80000)
        breached = t.check_stop_loss("BTC/USDC", 82500)
        assert l["ladder_id"] in breached
        ladders = t.list_ladders()
        assert ladders[0]["status"] == LadderStatus.STOPPED_OUT.value


# ─── Expiry ───────────────────────────────────────────────────────

@_with_ladders_env
def test_expired_ladder_cancels_on_tick():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        l = _make_ladder(t, expiry_hours=1)
        # Manually backdate expires_at
        l_id = l["ladder_id"]
        for stored in t._state["active_ladders"]:
            if stored["ladder_id"] == l_id:
                stored["expires_at"] = "2000-01-01T00:00:00Z"
        t.on_tick(time.time())
        ladders = t.list_ladders()
        assert ladders[0]["status"] == LadderStatus.CANCELLED.value


# ─── Kill switch ──────────────────────────────────────────────────

def test_disabled_ladder_crud_noop():
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d, disabled=True)
        assert t.list_ladders() == []
        assert t.create_ladder(pair="BTC/USDC", side="BUY", total_size=0.001,
                               rungs_spec=[{"price": 70000, "size": 0.001}]) is None
        assert t.match_rung("BTC/USDC", "BUY", 70000) is None
        assert t.check_stop_loss("BTC/USDC", 65000) == []


def test_ladder_disabled_without_env_flag():
    """Even with a valid tracker, HYDRA_THESIS_LADDERS unset → match_rung
    short-circuits. Preserves v2.13.2 journal schema for users who haven't
    opted in."""
    with tempfile.TemporaryDirectory() as d:
        t = ThesisTracker.load_or_default(save_dir=d)
        # create_ladder works even without the flag (state is authored)
        os.environ["HYDRA_THESIS_LADDERS"] = "1"
        try:
            l = _make_ladder(t)
            assert l is not None
        finally:
            os.environ.pop("HYDRA_THESIS_LADDERS", None)
        # But match_rung (the journal-schema-affecting path) is a no-op
        assert t.match_rung("BTC/USDC", "BUY", 74000) is None
        assert t.check_stop_loss("BTC/USDC", 71900) == []


def run_tests():
    fns = [
        test_create_ladder_success, test_create_ladder_rejects_bad_side,
        test_create_ladder_rejects_empty_rungs, test_cap_per_pair_enforced,
        test_cancel_ladder, test_rung_size_scaling_to_total_size,
        test_match_rung_hits_within_tolerance, test_match_rung_misses_outside_tolerance,
        test_match_rung_side_mismatch_returns_none,
        test_match_rung_pair_mismatch_returns_none,
        test_match_rung_returns_none_without_feature_flag,
        test_rung_placement_transition,
        test_rung_fill_transition_and_ladder_complete,
        test_stop_loss_breach_after_fill_marks_stopped_out,
        test_stop_loss_breach_without_fill_just_cancels,
        test_stop_loss_no_breach_no_change,
        test_stop_loss_sell_ladder_trips_on_price_rise,
        test_expired_ladder_cancels_on_tick,
        test_disabled_ladder_crud_noop,
        test_ladder_disabled_without_env_flag,
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
    print(f"  Thesis Phase D Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'=' * 60}")
    if errors:
        print("\n  FAILURES:")
        for name, err in errors:
            print(f"    {name}: {err}")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
