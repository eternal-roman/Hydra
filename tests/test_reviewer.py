"""Unit tests for Phase 7: hydra_reviewer (ResultReviewer + rigor gates +
downgrade logic).

Focus: anti-handwaving is enforced in CODE, not LLM prompt. Tests assert
that every verdict downgrade path fires on the expected evidence shape,
regardless of what an LLM might claim.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hydra_backtest import (  # noqa: E402
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    make_quick_config,
)
from hydra_experiments import (  # noqa: E402
    Experiment,
    ExperimentStore,
    new_experiment,
    run_experiment,
)
from hydra_reviewer import (  # noqa: E402
    DEFAULT_GATES,
    ProposedChange,
    RepeatabilityEvidence,
    REVIEWER_VERSION,
    ResultReviewer,
    ReviewDecision,
    SelfRetrospective,
    VALID_VERDICTS,
    _parse_json,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_experiment_with_trades(
    n_trades: int = 60,
    profits_pattern: str = "mixed",
    regime: str = "TREND_UP",
) -> Experiment:
    """Build a fully-synthesized Experiment — no backtest execution, just
    a hand-crafted trade_log. Lets tests target specific evidence shapes
    without the noise of running a real backtest."""
    cfg = make_quick_config(name="synth", n_candles=100, seed=1)

    # Build a fake trade_log: alternating buy/sell pairs; only sells carry profit.
    trade_log: List[Dict[str, Any]] = []
    for i in range(n_trades):
        pnl_sign = 1.0 if profits_pattern == "all_wins" else (
            -1.0 if profits_pattern == "all_losses" else (
                1.0 if i % 2 == 0 else -1.0
            )
        )
        trade_log.append({
            "tick": i * 2,
            "pair": "SOL/USDC",
            "side": "BUY",
            "profit": 0.0,
            "confidence": 0.7,
        })
        trade_log.append({
            "tick": i * 2 + 1,
            "pair": "SOL/USDC",
            "side": "SELL",
            "profit": pnl_sign * (1.0 + 0.1 * i),
            "confidence": 0.7,
        })

    # Ribbon of length enough to index all ticks — single regime
    ribbon_len = 2 * n_trades
    ribbon = {"SOL/USDC": [regime] * ribbon_len}

    # Fake result with per-pair metrics
    m = BacktestMetrics(
        total_trades=n_trades,
        total_return_pct=10.0 if profits_pattern != "all_losses" else -20.0,
        sharpe=1.5 if profits_pattern != "all_losses" else -1.5,
        win_count=n_trades if profits_pattern == "all_wins" else n_trades // 2,
        loss_count=0 if profits_pattern == "all_wins" else n_trades // 2,
        win_rate_pct=100.0 if profits_pattern == "all_wins" else 50.0,
        profit_factor=99.0 if profits_pattern == "all_wins" else 1.1,
    )
    result = BacktestResult(
        config=cfg,
        status="complete",
        equity_curve={"SOL/USDC": [100.0 + i * 0.1 for i in range(ribbon_len)]},
        regime_ribbon=ribbon,
        trade_log=trade_log,
        metrics=m,
        per_pair_metrics={"SOL/USDC": m},
    )

    exp = new_experiment(name="synth", config=cfg, hypothesis="test")
    exp.result = result
    exp.status = "complete"
    return exp


def _make_store() -> tuple:
    tmp = Path(tempfile.mkdtemp(prefix="hydra-rev-test-"))
    return tmp, ExperimentStore(root=tmp)


# ═══════════════════════════════════════════════════════════════
# Gate computation
# ═══════════════════════════════════════════════════════════════

class TestGateComputation(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = _make_store()
        self.reviewer = ResultReviewer(anthropic_client=None, store=self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_min_trades_50_fails_on_short_run(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=10)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(10), None)
        self.assertFalse(gates["min_trades_50"])

    def test_min_trades_50_passes_at_boundary(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=50)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(50), None)
        self.assertTrue(gates["min_trades_50"])

    def test_mc_ci_lower_positive_fires(self):
        ev = RepeatabilityEvidence(
            total_trades_in_sample=100, mc_iterations=300,
            mc_ci_95=(0.1, 0.5),
        )
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertTrue(gates["mc_ci_lower_positive"])

    def test_mc_ci_lower_negative_fails(self):
        ev = RepeatabilityEvidence(
            total_trades_in_sample=100, mc_iterations=300,
            mc_ci_95=(-0.05, 0.5),
        )
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["mc_ci_lower_positive"])

    def test_wf_majority_improved_60pct(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   wf_slices_tested=5, wf_improved_slices=3)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertTrue(gates["wf_majority_improved"])    # 3/5 = 0.6

    def test_wf_minority_improved_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   wf_slices_tested=5, wf_improved_slices=2)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["wf_majority_improved"])   # 2/5 = 0.4

    def test_wf_not_run_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   wf_slices_tested=0)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["wf_majority_improved"])

    def test_oos_gap_within_threshold(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100, oos_gap_pct=15.0)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertTrue(gates["oos_gap_acceptable"])

    def test_oos_gap_too_large_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100, oos_gap_pct=45.0)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["oos_gap_acceptable"])

    def test_oos_gap_negative_large_also_fails(self):
        # Negative gap = OOS outperformed in-sample; abs > threshold still bad
        ev = RepeatabilityEvidence(total_trades_in_sample=100, oos_gap_pct=-35.0)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["oos_gap_acceptable"])

    def test_improvement_above_2se_passes(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   mc_iterations=300,
                                   mc_mean_improvement=1.0, mc_std_error=0.3)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertTrue(gates["improvement_above_2se"])   # 1.0 > 2*0.3=0.6

    def test_improvement_below_2se_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   mc_iterations=300,
                                   mc_mean_improvement=0.5, mc_std_error=0.3)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["improvement_above_2se"])  # 0.5 < 0.6

    def test_improvement_zero_se_fails(self):
        # se=0 is degenerate — treat as failing (conservative)
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   mc_iterations=300,
                                   mc_mean_improvement=1.0, mc_std_error=0.0)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["improvement_above_2se"])

    def test_cross_pair_majority(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   pairs_total=3, pairs_improved=2)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertTrue(gates["cross_pair_majority"])   # 2/3 ≥ 0.5

    def test_cross_pair_minority_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   pairs_total=3, pairs_improved=1)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["cross_pair_majority"])

    def test_cross_pair_zero_total_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100, pairs_total=0)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["cross_pair_majority"])

    def test_regime_not_concentrated_passes(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   regime_concentration=0.55)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertTrue(gates["regime_not_concentrated"])

    def test_regime_concentrated_fails(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   regime_concentration=0.95)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(100), None)
        self.assertFalse(gates["regime_not_concentrated"])

    def test_configurable_gate_thresholds(self):
        self.reviewer.gates["min_trades_50"] = 10
        ev = RepeatabilityEvidence(total_trades_in_sample=15)
        gates = self.reviewer._compute_gates(ev, _make_experiment_with_trades(15), None)
        self.assertTrue(gates["min_trades_50"])


# ═══════════════════════════════════════════════════════════════
# Downgrade logic (anti-handwaving)
# ═══════════════════════════════════════════════════════════════

class TestDowngradeLogic(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = _make_store()
        self.reviewer = ResultReviewer(anthropic_client=None, store=self.store)
        # Give us a reliably-passing evidence base so we can test just the downgrade paths
        self.good_evidence = RepeatabilityEvidence(
            total_trades_in_sample=100,
            mc_iterations=300,
            mc_mean_improvement=0.8, mc_ci_95=(0.2, 1.4), mc_std_error=0.3,
            wf_slices_tested=5, wf_improved_slices=4,
            oos_gap_pct=10.0,
            pairs_total=3, pairs_improved=2,
            regime_concentration=0.4,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assemble(self, exp: Experiment, evidence: RepeatabilityEvidence,
                  llm_output: Dict[str, Any]) -> ReviewDecision:
        gates = self.reviewer._compute_gates(evidence, exp, None)
        return self.reviewer._assemble_decision(
            experiment=exp,
            evidence=evidence,
            gates=gates,
            all_passed=all(gates.values()),
            llm_output=llm_output,
            tokens_in=0, tokens_out=0,
            llm_used=True,
        )

    def test_param_tweak_with_failing_gates_downgrades_to_anomalous(self):
        # min_trades fails → PARAM_TWEAK becomes RESULT_ANOMALOUS
        exp = _make_experiment_with_trades(20)
        ev = replace(self.good_evidence, total_trades_in_sample=20)
        llm = {
            "verdict": "PARAM_TWEAK",
            "proposed_changes": [{
                "change_type": "param", "scope": "global",
                "target": "momentum_rsi_upper",
                "current_value": 70.0, "proposed_value": 75.0,
                "expected_impact": {"sharpe": 0.5},
            }],
        }
        dec = self._assemble(exp, ev, llm)
        self.assertEqual(dec.verdict, "RESULT_ANOMALOUS")
        self.assertEqual(dec.original_verdict, "PARAM_TWEAK")
        self.assertTrue(any("gates_failed" in f for f in dec.risk_flags))

    def test_regime_concentrated_param_tweak_becomes_code_review(self):
        # All gates pass EXCEPT regime_not_concentrated
        exp = _make_experiment_with_trades(100)
        ev = replace(self.good_evidence,
                     regime_concentration=0.90,
                     dominant_regime="VOLATILE")
        llm = {
            "verdict": "PARAM_TWEAK",
            "proposed_changes": [{
                "change_type": "param", "scope": "global",
                "target": "volatile_atr_mult",
                "current_value": 1.8, "proposed_value": 1.5,
                "expected_impact": {"sharpe": 0.2},
            }],
        }
        dec = self._assemble(exp, ev, llm)
        # Downgrade: regime-concentrated → CODE_REVIEW scoped to regime
        self.assertEqual(dec.verdict, "CODE_REVIEW")
        self.assertTrue(any(pc.scope == "regime:VOLATILE"
                            for pc in dec.proposed_changes))
        self.assertTrue(any("regime_concentrated:VOLATILE" in f
                            for f in dec.risk_flags))

    def test_prose_only_param_tweak_becomes_code_review(self):
        # Proposed changes have empty expected_impact → must be CODE_REVIEW
        exp = _make_experiment_with_trades(100)
        llm = {
            "verdict": "PARAM_TWEAK",
            "proposed_changes": [{
                "change_type": "param", "scope": "global",
                "target": "momentum_rsi_upper",
                "current_value": 70.0, "proposed_value": 75.0,
                "expected_impact": {},         # ← empty
                "rationale": "felt right",
            }],
        }
        dec = self._assemble(exp, self.good_evidence, llm)
        self.assertEqual(dec.verdict, "CODE_REVIEW")
        self.assertIn("prose_only_no_quantitative_evidence", dec.risk_flags)

    def test_claimed_sharpe_above_mc_upper_ci_flagged(self):
        exp = _make_experiment_with_trades(100)
        ev = replace(self.good_evidence, mc_ci_95=(0.1, 0.5))
        llm = {
            "verdict": "PARAM_TWEAK",
            "proposed_changes": [{
                "change_type": "param", "scope": "global",
                "target": "x", "current_value": 1.0, "proposed_value": 2.0,
                "expected_impact": {"sharpe": 1.0},   # 1.0 > CI upper 0.5
            }],
        }
        dec = self._assemble(exp, ev, llm)
        self.assertTrue(any("claimed_sharpe_exceeds_mc_upper_ci" in f
                            for f in dec.risk_flags))

    def test_impact_deviation_from_mc_mean_flagged(self):
        # Claim 0.3 vs MC mean 0.1 — deviation = 200% → flagged
        exp = _make_experiment_with_trades(100)
        ev = replace(self.good_evidence,
                     mc_mean_improvement=0.1, mc_ci_95=(0.01, 0.5))
        llm = {
            "verdict": "PARAM_TWEAK",
            "proposed_changes": [{
                "change_type": "param", "scope": "global",
                "target": "x", "current_value": 1.0, "proposed_value": 2.0,
                "expected_impact": {"sharpe": 0.3},
            }],
        }
        dec = self._assemble(exp, ev, llm)
        self.assertTrue(any("impact_deviation_vs_mc" in f
                            for f in dec.risk_flags))

    def test_param_tweak_without_proposed_changes_anomalous(self):
        exp = _make_experiment_with_trades(100)
        llm = {"verdict": "PARAM_TWEAK", "proposed_changes": []}
        dec = self._assemble(exp, self.good_evidence, llm)
        self.assertEqual(dec.verdict, "RESULT_ANOMALOUS")
        self.assertIn("param_tweak_without_proposed_changes", dec.risk_flags)

    def test_valid_param_tweak_survives(self):
        exp = _make_experiment_with_trades(100)
        llm = {
            "verdict": "PARAM_TWEAK",
            "observations": ["well-evidenced"],
            "reasoning": "all gates pass and quantitative evidence present",
            "proposed_changes": [{
                "change_type": "param", "scope": "pair:SOL/USDC",
                "target": "momentum_rsi_upper",
                "current_value": 70.0, "proposed_value": 75.0,
                "expected_impact": {"sharpe": 0.7},  # within (0.2, 1.4) CI
                "evidence_refs": ["exp_abc"],
                "rationale": "oversold rejection improves entry timing",
            }],
            "confidence": "MEDIUM",
        }
        dec = self._assemble(exp, self.good_evidence, llm)
        self.assertEqual(dec.verdict, "PARAM_TWEAK")
        self.assertTrue(dec.all_gates_passed)
        # Flag exists if dev > 50% vs MC mean 0.8 — 0.7 is within 13% so ok
        self.assertFalse(any("impact_deviation_vs_mc" in f for f in dec.risk_flags))

    def test_invalid_llm_verdict_falls_back_to_heuristic(self):
        exp = _make_experiment_with_trades(100)
        llm = {"verdict": "BOGUS_VERDICT", "proposed_changes": []}
        dec = self._assemble(exp, self.good_evidence, llm)
        self.assertIn(dec.verdict, VALID_VERDICTS)


# ═══════════════════════════════════════════════════════════════
# Heuristic verdict (LLM-off path)
# ═══════════════════════════════════════════════════════════════

class TestHeuristicVerdict(unittest.TestCase):
    def test_short_run_is_anomalous(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=20)
        v = ResultReviewer._heuristic_verdict(
            {"min_trades_50": False}, False, ev,
        )
        self.assertEqual(v, "RESULT_ANOMALOUS")

    def test_all_pass_positive_is_param_tweak(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   mc_iterations=300,
                                   mc_mean_improvement=0.5)
        gates = {k: True for k in DEFAULT_GATES}
        v = ResultReviewer._heuristic_verdict(gates, True, ev)
        self.assertEqual(v, "PARAM_TWEAK")

    def test_hypothesis_refuted_on_nonpositive_mean(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   mc_iterations=300,
                                   mc_mean_improvement=-0.1)
        gates = {"min_trades_50": True,
                 "mc_ci_lower_positive": False,
                 "wf_majority_improved": False,
                 "oos_gap_acceptable": True,
                 "improvement_above_2se": False,
                 "cross_pair_majority": True,
                 "regime_not_concentrated": True}
        v = ResultReviewer._heuristic_verdict(gates, False, ev)
        self.assertEqual(v, "HYPOTHESIS_REFUTED")

    def test_regime_concentrated_is_code_review(self):
        ev = RepeatabilityEvidence(total_trades_in_sample=100,
                                   regime_concentration=0.95,
                                   dominant_regime="VOLATILE")
        gates = {"min_trades_50": True,
                 "mc_ci_lower_positive": False,
                 "wf_majority_improved": True,
                 "oos_gap_acceptable": True,
                 "improvement_above_2se": False,
                 "cross_pair_majority": True,
                 "regime_not_concentrated": False}
        v = ResultReviewer._heuristic_verdict(gates, False, ev)
        self.assertEqual(v, "CODE_REVIEW")


# ═══════════════════════════════════════════════════════════════
# Review end-to-end (no LLM)
# ═══════════════════════════════════════════════════════════════

class TestReviewEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = _make_store()
        self.reviewer = ResultReviewer(anthropic_client=None, store=self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_review_without_result_is_anomalous(self):
        exp = new_experiment(
            name="no-result",
            config=make_quick_config(name="x", n_candles=10),
            hypothesis="test",
        )
        dec = self.reviewer.review(exp)
        self.assertEqual(dec.verdict, "RESULT_ANOMALOUS")
        self.assertFalse(dec.all_gates_passed)

    def test_short_synth_review_is_anomalous(self):
        exp = _make_experiment_with_trades(5)
        dec = self.reviewer.review(exp)
        self.assertEqual(dec.verdict, "RESULT_ANOMALOUS")
        self.assertFalse(dec.gates_passed["min_trades_50"])

    def test_review_persists_to_history(self):
        exp = _make_experiment_with_trades(20)
        # Save so the store has the experiment too (not strictly required)
        self.store.save(exp)
        self.reviewer.review(exp)
        history = (self.tmp / "review_history.jsonl")
        self.assertTrue(history.exists())
        lines = history.read_text().splitlines()
        self.assertGreaterEqual(len(lines), 1)
        rec = json.loads(lines[-1])
        self.assertEqual(rec["exp_id"], exp.id)

    def test_review_never_raises(self):
        # Corrupt the experiment to force a fault path
        exp = _make_experiment_with_trades(100)
        exp.result.trade_log = None  # type: ignore  # deliberately broken
        dec = self.reviewer.review(exp)
        self.assertIn(dec.verdict, VALID_VERDICTS)
        # Either succeeds (robust to None) or anomalous with exception flag
        if "reviewer_exception" in dec.risk_flags:
            self.assertEqual(dec.verdict, "RESULT_ANOMALOUS")


# ═══════════════════════════════════════════════════════════════
# LLM integration (mock)
# ═══════════════════════════════════════════════════════════════

class TestLLMIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = _make_store()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_client(self, verdict_payload: Dict[str, Any],
                     tokens=(100, 50)) -> MagicMock:
        """Build a MagicMock whose `.messages.create` returns a one-shot
        end_turn response with `verdict_payload` as the JSON in a text block."""
        class _Block:
            def __init__(self, text): self.type = "text"; self.text = text
        class _Usage:
            def __init__(self, tin, tout): self.input_tokens = tin; self.output_tokens = tout
        class _Response:
            def __init__(self, content, u):
                self.content = content; self.stop_reason = "end_turn"; self.usage = u
        fake = MagicMock()
        fake.messages.create.return_value = _Response(
            [_Block(json.dumps(verdict_payload))],
            _Usage(tokens[0], tokens[1]),
        )
        return fake

    def test_llm_verdict_flows_through_when_gates_pass(self):
        fake = self._fake_client({
            "verdict": "PARAM_TWEAK",
            "observations": ["obs1"],
            "reasoning": "r",
            "proposed_changes": [{
                "change_type": "param", "scope": "pair:SOL/USDC",
                "target": "momentum_rsi_upper",
                "current_value": 70.0, "proposed_value": 75.0,
                "expected_impact": {"sharpe": 0.3},
            }],
            "confidence": "HIGH",
        })
        reviewer = ResultReviewer(anthropic_client=fake, store=self.store,
                                  max_daily_cost=100.0)

        # Manually synthesize passing evidence
        exp = _make_experiment_with_trades(100)
        # Patch evidence builder to return passing gates deterministically
        orig_build = reviewer._build_repeatability_evidence
        def _fake_build(experiment, baseline):
            return RepeatabilityEvidence(
                total_trades_in_sample=100, mc_iterations=300,
                mc_mean_improvement=0.3, mc_ci_95=(0.05, 0.6),
                mc_std_error=0.1,
                wf_slices_tested=5, wf_improved_slices=4,
                oos_gap_pct=10.0,
                pairs_total=3, pairs_improved=2,
                regime_concentration=0.4,
            )
        reviewer._build_repeatability_evidence = _fake_build  # type: ignore
        try:
            dec = reviewer.review(exp)
        finally:
            reviewer._build_repeatability_evidence = orig_build

        self.assertEqual(dec.verdict, "PARAM_TWEAK")
        self.assertTrue(dec.all_gates_passed)
        self.assertTrue(dec.llm_used)
        self.assertGreater(dec.tokens_used, 0)
        self.assertGreater(dec.cost_usd, 0.0)
        self.assertEqual(dec.confidence, "HIGH")

    def test_llm_verdict_downgraded_on_failing_gates(self):
        fake = self._fake_client({
            "verdict": "PARAM_TWEAK",
            "proposed_changes": [{
                "change_type": "param", "scope": "global",
                "target": "x", "current_value": 1.0, "proposed_value": 2.0,
                "expected_impact": {"sharpe": 0.5},
            }],
        })
        reviewer = ResultReviewer(anthropic_client=fake, store=self.store,
                                  max_daily_cost=100.0)
        # Short run → min_trades fails
        exp = _make_experiment_with_trades(10)
        dec = reviewer.review(exp)
        self.assertNotEqual(dec.verdict, "PARAM_TWEAK")
        self.assertEqual(dec.original_verdict, "PARAM_TWEAK")

    def test_llm_failure_falls_back_to_heuristic(self):
        fake = MagicMock()
        fake.messages.create.side_effect = RuntimeError("api dead")
        reviewer = ResultReviewer(anthropic_client=fake, store=self.store,
                                  max_daily_cost=100.0)
        exp = _make_experiment_with_trades(100)
        dec = reviewer.review(exp)
        # Should still produce a verdict (from heuristic)
        self.assertIn(dec.verdict, VALID_VERDICTS)
        # LLM was attempted but not used successfully
        self.assertFalse(dec.llm_used)

    def test_budget_exceeded_skips_llm(self):
        fake = self._fake_client({"verdict": "NO_CHANGE"})
        reviewer = ResultReviewer(anthropic_client=fake, store=self.store,
                                  max_daily_cost=0.0)  # zero budget
        exp = _make_experiment_with_trades(100)
        dec = reviewer.review(exp)
        self.assertFalse(dec.llm_used)
        fake.messages.create.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# Self-retrospective
# ═══════════════════════════════════════════════════════════════

class TestSelfRetrospective(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = _make_store()
        self.reviewer = ResultReviewer(anthropic_client=None, store=self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_history(self):
        retro = self.reviewer.self_retrospective(lookback_days=30)
        self.assertEqual(retro.recommendations_reviewed, 0)

    def test_counts_verdicts(self):
        self.store.log_review("exp1", {"verdict": "PARAM_TWEAK"})
        self.store.log_review("exp2", {"verdict": "CODE_REVIEW"})
        self.store.log_review("exp3", {"verdict": "NO_CHANGE"})
        self.store.log_review("exp4", {"verdict": "PARAM_TWEAK"})
        retro = self.reviewer.self_retrospective(lookback_days=30)
        self.assertEqual(retro.recommendations_reviewed, 4)
        self.assertEqual(retro.param_tweaks_proposed, 2)
        self.assertEqual(retro.code_reviews_proposed, 1)
        self.assertEqual(retro.no_change_verdicts, 1)

    def test_retrospective_type(self):
        retro = self.reviewer.self_retrospective()
        self.assertIsInstance(retro, SelfRetrospective)
        self.assertTrue(retro.generated_at)


# ═══════════════════════════════════════════════════════════════
# Config + utilities
# ═══════════════════════════════════════════════════════════════

class TestConfigAndUtilities(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = _make_store()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_file_overrides_gates(self):
        cfg_path = self.tmp / "reviewer.json"
        cfg_path.write_text(json.dumps({
            "gates": {"min_trades_50": 10, "oos_gap_acceptable_pct": 50.0},
        }))
        reviewer = ResultReviewer(store=self.store, config_path=cfg_path)
        self.assertEqual(reviewer.gates["min_trades_50"], 10)
        self.assertEqual(reviewer.gates["oos_gap_acceptable_pct"], 50.0)

    def test_malformed_config_falls_back(self):
        cfg_path = self.tmp / "reviewer.json"
        cfg_path.write_text("not json {[")
        reviewer = ResultReviewer(store=self.store, config_path=cfg_path)
        self.assertEqual(reviewer.gates["min_trades_50"], DEFAULT_GATES["min_trades_50"])

    def test_parse_json_fenced(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(_parse_json(text), {"a": 1})

    def test_parse_json_plain(self):
        self.assertEqual(_parse_json('{"x": 2}'), {"x": 2})

    def test_parse_json_invalid(self):
        self.assertIsNone(_parse_json("nonsense"))

    def test_decision_to_dict_is_json_serializable(self):
        dec = ReviewDecision(
            experiment_id="x", reviewed_at=_iso_utc_now_str(),
            reviewer_model="heuristic", reviewer_version=REVIEWER_VERSION,
            verdict="NO_CHANGE",
        )
        d = dec.to_dict()
        # mc_ci_95 must be a list (JSON-safe), not a tuple
        self.assertIsInstance(d["repeatability"]["mc_ci_95"], list)
        # Full round-trip
        serialized = json.dumps(d)
        self.assertIn('"verdict": "NO_CHANGE"', serialized)


def _iso_utc_now_str() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


if __name__ == "__main__":
    unittest.main()
