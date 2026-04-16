"""Unit tests for Phase 11: hydra_shadow_validator + tuner write path.

Covers:
  - Tuner: apply_external_param_update (happy, clamp, reject non-finite,
           reject unknown, rollback_to_previous, rollback empty)
  - ShadowValidator: submit / queue / single-active-slot,
    ingest_candle noop without active, record_live_close accumulates,
    poll_complete verdicts (approve_eligible, rejected, still_running),
    human approve → tuner write, reject / cancel, auto-reject on
    insufficient improvement, expiry via tick() + tiny timeout,
    persistence round-trip across validator recreation, rollback_last_approval,
    broadcast + callback integration.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_engine import Candle  # noqa: E402
from hydra_reviewer import ProposedChange  # noqa: E402
from hydra_shadow_validator import (  # noqa: E402
    DEFAULT_MIN_TRADES,
    ShadowCandidate,
    ShadowValidator,
    ValidationResult,
    _parse_iso,
)
from hydra_tuner import DEFAULT_PARAMS, PARAM_BOUNDS, ParameterTracker  # noqa: E402


def _make_tracker(tmp: Path, pair: str = "SOL/USDC") -> ParameterTracker:
    return ParameterTracker(pair=pair, save_dir=str(tmp))


def _param_change(target="momentum_rsi_upper", value=75.0,
                  scope="pair:SOL/USDC") -> ProposedChange:
    return ProposedChange(
        change_type="param",
        scope=scope,
        target=target,
        current_value=DEFAULT_PARAMS.get(target, 0.0),
        proposed_value=value,
        expected_impact={"sharpe": 0.3},
    )


# ═══════════════════════════════════════════════════════════════
# Tuner write path
# ═══════════════════════════════════════════════════════════════

class TestTunerWritePath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-tuner-ext-"))
        self.tracker = _make_tracker(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_apply_happy_path(self):
        before = self.tracker.get_tunable_params()
        res = self.tracker.apply_external_param_update(
            {"momentum_rsi_upper": 78.0}, source="unit"
        )
        self.assertEqual(res["applied"], {"momentum_rsi_upper": 78.0})
        self.assertEqual(res["source"], "unit")
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"], 78.0)
        # File persisted
        self.assertTrue(Path(self.tracker.save_path).exists())
        # History captures prior state for rollback
        self.assertEqual(len(self.tracker._param_history), 1)
        self.assertEqual(self.tracker._param_history[0]["momentum_rsi_upper"],
                         before["momentum_rsi_upper"])

    def test_apply_clamps_to_bounds(self):
        # upper bound for momentum_rsi_upper is 90
        self.tracker.apply_external_param_update(
            {"momentum_rsi_upper": 200.0}, source="unit"
        )
        lo, hi = PARAM_BOUNDS["momentum_rsi_upper"]
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"], hi)

    def test_apply_rejects_nonfinite(self):
        import math
        res = self.tracker.apply_external_param_update(
            {"momentum_rsi_upper": math.inf}, source="unit",
        )
        self.assertEqual(res["applied"], {})
        self.assertIn("nonfinite:momentum_rsi_upper", res["skipped"])

    def test_apply_rejects_unparseable(self):
        res = self.tracker.apply_external_param_update(
            {"momentum_rsi_upper": "not_a_number"}, source="unit",
        )
        self.assertEqual(res["applied"], {})
        self.assertIn("nan:momentum_rsi_upper", res["skipped"])

    def test_apply_rejects_unknown_key(self):
        res = self.tracker.apply_external_param_update(
            {"fake_param": 1.0}, source="unit",
        )
        self.assertEqual(res["applied"], {})
        self.assertIn("unknown:fake_param", res["skipped"])

    def test_apply_nothing_no_history_pollution(self):
        # An all-rejected call should NOT bloat history
        self.tracker.apply_external_param_update({"fake_param": 99})
        self.assertEqual(len(self.tracker._param_history), 0)

    def test_rollback_reverts_single_apply(self):
        original = self.tracker.get_tunable_params()
        self.tracker.apply_external_param_update({"momentum_rsi_upper": 78.0})
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"], 78.0)
        ok = self.tracker.rollback_to_previous()
        self.assertTrue(ok)
        self.assertEqual(self.tracker.current_params, original)
        # History cleared (depth=1)
        self.assertEqual(len(self.tracker._param_history), 0)

    def test_rollback_empty_returns_false(self):
        self.assertFalse(self.tracker.rollback_to_previous())

    def test_rollback_only_depth_1(self):
        # Two applies; rollback reverts ONLY the most recent
        self.tracker.apply_external_param_update({"momentum_rsi_upper": 72.0})
        self.tracker.apply_external_param_update({"momentum_rsi_upper": 78.0})
        ok = self.tracker.rollback_to_previous()
        self.assertTrue(ok)
        # Reverts to 72.0, not to the ORIGINAL 70.0
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"], 72.0)

    def test_update_count_unchanged_by_external(self):
        # External updates don't bump the tuner's observation-driven counter
        before = self.tracker.update_count
        self.tracker.apply_external_param_update({"momentum_rsi_upper": 78.0})
        self.assertEqual(self.tracker.update_count, before)


# ═══════════════════════════════════════════════════════════════
# Validator core
# ═══════════════════════════════════════════════════════════════

class TestShadowValidatorSubmit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-"))
        self.tracker = _make_tracker(self.tmp)
        self.v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=3,
            store_root=self.tmp,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_submit_returns_id_and_activates(self):
        cid = self.v.submit(_param_change(), experiment_id="e1")
        self.assertTrue(cid)
        active = self.v.current()
        self.assertIsNotNone(active)
        self.assertEqual(active.id, cid)
        self.assertEqual(active.status, "active")
        self.assertEqual(active.experiment_id, "e1")
        self.assertEqual(active.pair, "SOL/USDC")

    def test_submit_rejects_code_change(self):
        bad = ProposedChange(change_type="code", scope="global",
                             target="file.py:10", proposed_value=None)
        with self.assertRaises(ValueError):
            self.v.submit(bad, experiment_id="e-bad")

    def test_submit_rejects_missing_target(self):
        bad = ProposedChange(change_type="param", scope="global", target="",
                             proposed_value=70.0)
        # Empty target counts as None/missing per validator's check
        with self.assertRaises(ValueError):
            ShadowValidator(store_root=self.tmp).submit(bad, experiment_id="e")

    def test_submit_global_scope_multi_pair(self):
        # Global scope with a multi-pair registry spins up one engine per pair.
        tracker2 = _make_tracker(self.tmp, pair="BTC/USDC")
        v2 = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker, "BTC/USDC": tracker2},
            min_trades=3,
            store_root=self.tmp / "sub",
        )
        v2.submit(_param_change(scope="global"), experiment_id="e-multi")
        active = v2.current()
        self.assertEqual(active.pair, "*")
        self.assertEqual(len(v2._shadow_engines), 2)

    def test_second_submit_queues_behind(self):
        c1 = self.v.submit(_param_change(), experiment_id="e1")
        c2 = self.v.submit(_param_change(value=72.0), experiment_id="e2")
        queue = self.v.queue_snapshot()
        statuses = {c.id: c.status for c in queue}
        self.assertEqual(statuses[c1], "active")
        self.assertEqual(statuses[c2], "pending")
        # Only the active candidate has a shadow engine spun up
        self.assertEqual(len(self.v._shadow_engines), 1)


class TestShadowValidatorVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-v-"))
        self.tracker = _make_tracker(self.tmp)
        self.v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=3,
            min_improvement_pct=0.5,
            store_root=self.tmp,
        )
        self.cid = self.v.submit(_param_change(), experiment_id="e-v")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_live_close_accumulates(self):
        self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": 1.0})
        self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": 2.0})
        active = self.v.current()
        self.assertEqual(active.trades_observed, 2)
        self.assertEqual(active.live_pnl_sum, 3.0)

    def test_record_ignores_buy(self):
        self.v.record_live_close("SOL/USDC", {"side": "BUY", "profit": 0.0})
        self.assertEqual(self.v.current().trades_observed, 0)

    def test_record_ignores_zero_profit(self):
        self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": 0.0})
        self.assertEqual(self.v.current().trades_observed, 0)

    def test_record_ignores_mismatched_pair(self):
        # Candidate is scoped to SOL/USDC; BTC trade doesn't count
        self.v.record_live_close("BTC/USDC", {"side": "SELL", "profit": 1.0})
        self.assertEqual(self.v.current().trades_observed, 0)

    def test_poll_still_running_below_min_trades(self):
        self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": 1.0})
        self.assertIsNone(self.v.poll_complete())

    def test_poll_approve_eligible_when_live_negative(self):
        # Live takes 3 losses → shadow (zero moves) wins by default math
        for _ in range(3):
            self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": -1.0})
        res = self.v.poll_complete()
        self.assertIsNotNone(res)
        self.assertEqual(res.verdict, "approve_eligible")
        self.assertEqual(res.trades_evaluated, 3)
        self.assertGreater(res.delta_pct, 0)

    def test_poll_rejects_when_shadow_underperforms(self):
        # Force shadow_pnl_sum=0 and live_pnl_sum large positive
        for _ in range(3):
            self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": 10.0})
        res = self.v.poll_complete()
        self.assertIsNotNone(res)
        self.assertEqual(res.verdict, "rejected")
        # After rejection, active slot is gone
        self.assertIsNone(self.v.current())

    def test_rejection_advances_queue(self):
        c2 = self.v.submit(_param_change(value=72.0), experiment_id="e-v2")
        # Reject the first (still active)
        for _ in range(3):
            self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": 10.0})
        self.v.poll_complete()    # auto-rejects first
        active = self.v.current()
        self.assertIsNotNone(active)
        self.assertEqual(active.id, c2)


class TestShadowValidatorApprove(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-a-"))
        self.tracker = _make_tracker(self.tmp)
        self.v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=3,
            min_improvement_pct=0.5,
            store_root=self.tmp,
        )
        self.cid = self.v.submit(_param_change(value=78.0), experiment_id="e-a")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_approve_before_window_raises(self):
        with self.assertRaises(ValueError):
            self.v.approve(self.cid)

    def test_approve_writes_to_tuner(self):
        # Make live lose → shadow wins, eligible
        for _ in range(3):
            self.v.record_live_close("SOL/USDC", {"side": "SELL", "profit": -1.0})
        res = self.v.poll_complete()
        self.assertEqual(res.verdict, "approve_eligible")

        out = self.v.approve(self.cid, approver="eric")
        self.assertIn("applied", out)
        self.assertIn("SOL/USDC", out["applied"])
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"], 78.0)
        # Candidate is terminal; active slot gone
        self.assertIsNone(self.v.current())

    def test_reject_before_completion(self):
        ok = self.v.reject(self.cid, reason="human thought better of it")
        self.assertTrue(ok)
        # Non-active after rejection
        self.assertIsNone(self.v.current())
        # Tuner untouched
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"],
                         DEFAULT_PARAMS["momentum_rsi_upper"])

    def test_cancel_returns_true_for_active(self):
        ok = self.v.cancel(self.cid)
        self.assertTrue(ok)
        # Second cancel is a noop
        self.assertFalse(self.v.cancel(self.cid))

    def test_approve_wrong_id_raises(self):
        with self.assertRaises(ValueError):
            self.v.approve("unknown-id")


# ═══════════════════════════════════════════════════════════════
# Expiry + tick
# ═══════════════════════════════════════════════════════════════

class TestExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-exp-"))
        self.tracker = _make_tracker(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tick_expires_stale_candidate(self):
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=10,
            window_timeout_sec=0.001,    # immediate expiry for test
            store_root=self.tmp,
        )
        cid = v.submit(_param_change(), experiment_id="e-expire")
        # Let a moment pass so _parse_iso diff > 0.001s
        time.sleep(0.02)
        v.tick()
        active = v.current()
        self.assertIsNone(active)
        # Terminal status recorded
        terminal = [c for c in v.queue_snapshot() if c.id == cid]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].status, "expired")


# ═══════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════

class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-p-"))
        self.tracker = _make_tracker(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_survives_validator_recreation(self):
        v1 = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=3, store_root=self.tmp,
        )
        cid = v1.submit(_param_change(), experiment_id="e-persist")
        v1.record_live_close("SOL/USDC", {"side": "SELL", "profit": -1.0})
        # New instance reads state off disk
        v2 = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=3, store_root=self.tmp,
        )
        active = v2.current()
        self.assertIsNotNone(active)
        self.assertEqual(active.id, cid)
        self.assertEqual(active.trades_observed, 1)

    def test_malformed_state_is_tolerated(self):
        state_path = self.tmp / "shadow_state.json"
        state_path.write_text("not json {[")
        v = ShadowValidator(store_root=self.tmp)
        # Clean bootstrap — no exceptions
        self.assertIsNone(v.current())
        self.assertEqual(v.queue_snapshot(), [])


# ═══════════════════════════════════════════════════════════════
# Rollback
# ═══════════════════════════════════════════════════════════════

class TestRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-r-"))
        self.tracker = _make_tracker(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rollback_last_approval_undoes_write(self):
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=3, store_root=self.tmp,
        )
        cid = v.submit(_param_change(value=78.0), experiment_id="e-rb")
        for _ in range(3):
            v.record_live_close("SOL/USDC", {"side": "SELL", "profit": -1.0})
        v.poll_complete()
        v.approve(cid)
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"], 78.0)
        # Rollback reverts
        res = v.rollback_last_approval()
        self.assertEqual(res, {"SOL/USDC": True})
        self.assertEqual(self.tracker.current_params["momentum_rsi_upper"],
                         DEFAULT_PARAMS["momentum_rsi_upper"])

    def test_rollback_without_approval_is_false_per_pair(self):
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            store_root=self.tmp,
        )
        res = v.rollback_last_approval()
        self.assertEqual(res, {"SOL/USDC": False})

    def test_rollback_empty_registry(self):
        v = ShadowValidator(tuner_registry={}, store_root=self.tmp)
        self.assertEqual(v.rollback_last_approval(), {})


# ═══════════════════════════════════════════════════════════════
# Broadcast + callback
# ═══════════════════════════════════════════════════════════════

class TestBroadcast(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-b-"))
        self.tracker = _make_tracker(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_broadcaster_receives_events(self):
        bc = MagicMock()
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=2, store_root=self.tmp, broadcaster=bc,
        )
        v.submit(_param_change(), experiment_id="e-bc")
        v.record_live_close("SOL/USDC", {"side": "SELL", "profit": -1.0})
        # Expect shadow_state messages
        types = [c.args[0] for c in bc.broadcast_message.call_args_list]
        self.assertIn("shadow_state", types)

    def test_on_state_change_callback_invoked(self):
        events: List[str] = []
        def cb(event, cand): events.append(event)
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            min_trades=2, store_root=self.tmp, on_state_change=cb,
        )
        v.submit(_param_change(), experiment_id="e-cb")
        v.record_live_close("SOL/USDC", {"side": "SELL", "profit": -1.0})
        # submitted + activated + progress all fire
        self.assertIn("submitted", events)
        self.assertIn("activated", events)
        self.assertIn("progress", events)


# ═══════════════════════════════════════════════════════════════
# Ingest candle (noop paths + active forwarding)
# ═══════════════════════════════════════════════════════════════

class TestIngestCandle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hydra-shadow-i-"))
        self.tracker = _make_tracker(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ingest_without_active_is_noop(self):
        v = ShadowValidator(store_root=self.tmp)
        v.ingest_candle("SOL/USDC",
                        Candle(100, 101, 99, 100.5, 1000.0, 0))
        # No crash, no state change

    def test_ingest_unscoped_pair_is_noop(self):
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            store_root=self.tmp,
        )
        v.submit(_param_change(), experiment_id="e")
        v.ingest_candle("BTC/USDC",
                        Candle(100, 101, 99, 100.5, 1000.0, 0))
        # Shadow engine for BTC doesn't exist → silent no-op
        self.assertIsNone(v._shadow_engines.get("BTC/USDC"))

    def test_ingest_forwards_to_shadow_engine(self):
        v = ShadowValidator(
            tuner_registry={"SOL/USDC": self.tracker},
            store_root=self.tmp,
        )
        v.submit(_param_change(), experiment_id="e")
        engine = v._shadow_engines["SOL/USDC"]
        initial_price_count = len(engine.prices)
        v.ingest_candle("SOL/USDC",
                        Candle(100, 101, 99, 100.5, 1000.0, 0))
        self.assertEqual(len(engine.prices), initial_price_count + 1)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

class TestHelpers(unittest.TestCase):
    def test_parse_iso_round_trip(self):
        ts = _parse_iso("2025-01-15T10:00:00Z")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 0)

    def test_parse_iso_bad(self):
        self.assertIsNone(_parse_iso(""))
        self.assertIsNone(_parse_iso("not-a-date"))


if __name__ == "__main__":
    unittest.main()
