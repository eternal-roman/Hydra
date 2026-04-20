#!/usr/bin/env python3
"""
HYDRA Backtest — AI Reviewer & Self-Evaluation Layer (Phase 7 of v2.10.0).

The centerpiece anti-handwaving layer. Every backtest result flows through
a `ResultReviewer`. The reviewer:

  1. Gathers evidence — walk-forward slices, Monte Carlo CI on trade
     profits, out-of-sample gap, per-pair + per-regime breakdowns.
  2. Computes 7 rigor gates AS CODE (not prompt) — see DEFAULT_GATES and
     `_compute_gates`. A proposed change is auto-apply-eligible ONLY when
     `all_gates_passed == True`. This is the architectural anti-handwaving
     mechanism: the LLM cannot bypass gates via clever prose.
  3. Asks Claude Opus (optional) for a human-readable rationale + proposed
     changes. The LLM's verdict field is cross-checked against the gates;
     mismatches are DOWNGRADED in code.
  4. Persists the `ReviewDecision` via ExperimentStore.log_review +
     attaches it to the Experiment record.
  5. Supports `self_retrospective(lookback_days)` — audits its own prior
     recommendations and computes a `reviewer_accuracy_score`.

Anti-handwaving architecture (from docs/BACKTEST_SPEC.md §6.5):

  - Reviewer cannot recommend based on < 50 trades.
  - Reviewer cannot claim expected_sharpe_delta above MC upper CI.
  - Claimed expected_impact cross-checked against mc_mean_improvement —
    deviation > 50% → risk_flag.
  - Verdict/gate mismatch (e.g., PARAM_TWEAK with failing gates) →
    downgraded to RESULT_ANOMALOUS with "reviewer self-contradicted" flag.
  - Regime-concentrated improvement → PARAM_TWEAK becomes CODE_REVIEW
    scoped to that regime.
  - Proposed changes without quantitative evidence → CODE_REVIEW only,
    never PARAM_TWEAK.

The reviewer runs without an LLM — in heuristic-only mode it derives a
verdict from the gates themselves. This makes the reviewer usable in
offline / budget-constrained / CI scenarios.

See docs/BACKTEST_SPEC.md §5.5 for the data schemas.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hydra_backtest import BacktestResult, _iso_utc_now
from hydra_backtest_metrics import (
    ImprovementReport,
    MonteCarloReport,
    OutOfSampleReport,
    WalkForwardReport,
    monte_carlo_improvement,
    monte_carlo_resample,
    out_of_sample_gap,
    regime_conditioned_pnl,
    walk_forward,
)
from hydra_experiments import Experiment, ExperimentStore


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

REVIEWER_VERSION = "1.0.0-phase7"

# Gate thresholds (editable via hydra_reviewer_config.json by ops).
DEFAULT_GATES: Dict[str, float] = {
    "min_trades_50": 50,                     # trade count floor
    "mc_ci_lower_positive": 0.0,             # MC lower bound must exceed this
    "wf_majority_improved": 0.6,             # 60% of WF slices sharpe>0
    "oos_gap_acceptable_pct": 30.0,          # |gap_pct| must be under this
    "improvement_above_2se_multiplier": 2.0, # mean_improvement > N * se
    "cross_pair_majority": 0.5,              # fraction of pairs improved
    "regime_concentration_threshold": 0.80,  # >80% P&L in one regime = fail
}

VALID_VERDICTS = {
    "NO_CHANGE",
    "PARAM_TWEAK",
    "CODE_REVIEW",
    "RESULT_ANOMALOUS",
    "HYPOTHESIS_REFUTED",
}


# ═══════════════════════════════════════════════════════════════
# Data classes (matches docs/BACKTEST_SPEC.md §5.5)
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProposedChange:
    """One concrete change the reviewer is proposing."""
    change_type: str                           # "param" | "code"
    scope: str                                 # "global" | "pair:X" | "regime:Y"
    target: str                                # param name or "file.py:line"
    current_value: Optional[float] = None
    proposed_value: Optional[float] = None
    expected_impact: Dict[str, float] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    rationale: str = ""
    risk_notes: str = ""


@dataclass
class RepeatabilityEvidence:
    """Evidence feeding the rigor gates. Spec §5.5 verbatim."""
    # Walk-forward
    wf_slices_tested: int = 0
    wf_improved_slices: int = 0
    wf_improvement_pct_per_slice: List[float] = field(default_factory=list)
    wf_mean_sharpe: float = 0.0
    wf_sharpe_stability: float = 0.0

    # Monte Carlo on trade profits (single-series)
    mc_iterations: int = 0
    mc_mean_improvement: float = 0.0
    mc_ci_95: Tuple[float, float] = (0.0, 0.0)
    mc_p_value: float = 1.0
    mc_std_error: float = 0.0

    # Out-of-sample
    oos_held_out_pct: float = 0.0
    in_sample_sharpe: float = 0.0
    oos_sharpe: float = 0.0
    oos_gap_pct: float = 0.0

    # Cross-pair
    pairs_improved: int = 0
    pairs_total: int = 0
    improvement_by_pair: Dict[str, float] = field(default_factory=dict)

    # Regime
    regimes_improved: int = 0
    regimes_total: int = 4
    improvement_by_regime: Dict[str, float] = field(default_factory=dict)
    regime_concentration: float = 0.0          # max(|pnl|) / sum(|pnl|)
    dominant_regime: Optional[str] = None

    # Trade count sanity
    total_trades_in_sample: int = 0

    # Diagnostic — records "wf:<ExcType>" / "oos:<ExcType>" when optional
    # analysis passes failed. Surfaces to risk_flags so gate failures like
    # wf_majority_improved=False aren't silently unexplained.
    run_failures: List[str] = field(default_factory=list)


@dataclass
class ReviewDecision:
    """Full reviewer output for one experiment. Spec §5.5."""
    experiment_id: str
    reviewed_at: str
    reviewer_model: str
    reviewer_version: str

    verdict: str                               # VALID_VERDICTS
    observations: List[str] = field(default_factory=list)
    root_cause_hypothesis: str = ""
    reasoning: str = ""

    proposed_changes: List[ProposedChange] = field(default_factory=list)

    materiality_score: float = 0.0             # 0-1 normalized Sharpe delta
    repeatability: RepeatabilityEvidence = field(default_factory=RepeatabilityEvidence)
    gates_passed: Dict[str, bool] = field(default_factory=dict)
    all_gates_passed: bool = False

    confidence: str = "LOW"                    # LOW | MEDIUM | HIGH
    risk_flags: List[str] = field(default_factory=list)
    source_files_read: List[str] = field(default_factory=list)
    tokens_used: int = 0
    cost_usd: float = 0.0

    llm_used: bool = False
    original_verdict: Optional[str] = None     # pre-downgrade, for audit

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Tuples don't JSON-roundtrip; spec uses Tuple for mc_ci_95
        d["repeatability"]["mc_ci_95"] = list(d["repeatability"]["mc_ci_95"])
        return d


@dataclass
class SelfRetrospective:
    """Reviewer's audit of its own prior recommendations."""
    generated_at: str
    lookback_days: int
    recommendations_reviewed: int
    param_tweaks_proposed: int
    code_reviews_proposed: int
    no_change_verdicts: int
    anomalous_verdicts: int
    reviewer_accuracy_score: Optional[float] = None
    notes: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# ResultReviewer
# ═══════════════════════════════════════════════════════════════

REVIEWER_PROMPT = """You are HYDRA's ResultReviewer. You analyze a completed backtest result, the system's automatically-computed rigor gates, and cross-cutting evidence (walk-forward, Monte Carlo CI, out-of-sample gap, per-regime P&L). You produce a structured ReviewDecision.

ABSOLUTE RULES (code-enforced — you cannot override these):
1. NEVER propose a PARAM_TWEAK verdict when any rigor gate has failed. If a gate failed, the code will downgrade your verdict. Better to choose NO_CHANGE or CODE_REVIEW yourself.
2. NEVER claim an expected Sharpe delta exceeding the Monte Carlo upper 95% CI bound. The CI is your ceiling.
3. NEVER propose a PARAM_TWEAK based on prose reasoning alone. Proposed changes must cite specific quantitative evidence (experiment id, MC CI, WF slice sharpes, regime breakdown).
4. Materiality — changes are "worth it" only if expected impact > 2× standard error. Smaller improvements are noise.
5. Repeatability — a result on 30 trades is NOT repeatable, regardless of how sharp the metrics look. `min_trades_50` must pass.

VERDICT OPTIONS:
- NO_CHANGE: result is as expected; no actionable improvement identified.
- PARAM_TWEAK: a specific param change is recommended AND all gates pass.
- CODE_REVIEW: a code-level or rule change is worth human inspection (auto-apply never allowed).
- RESULT_ANOMALOUS: the result doesn't fit expected patterns; flag for human.
- HYPOTHESIS_REFUTED: the experimenter's stated hypothesis is contradicted by the data.

Respond ONLY with this JSON (no other text):
{
  "verdict": "NO_CHANGE" | "PARAM_TWEAK" | "CODE_REVIEW" | "RESULT_ANOMALOUS" | "HYPOTHESIS_REFUTED",
  "observations": ["specific factual pattern, <200 chars", ...],
  "root_cause_hypothesis": "what is driving this result",
  "reasoning": "full reasoning chain",
  "proposed_changes": [
    {
      "change_type": "param" | "code",
      "scope": "global" | "pair:SOL/USDC" | "regime:VOLATILE",
      "target": "param_name or file.py:line",
      "current_value": <number or null>,
      "proposed_value": <number or null>,
      "expected_impact": {"sharpe": 0.3, "max_dd_pct": -1.2},
      "evidence_refs": ["experiment_id_or_regime_window", ...],
      "rationale": "why",
      "risk_notes": "how this could go wrong"
    }
  ],
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "risk_flags": ["flag1", ...]
}"""


class ResultReviewer:
    """Produces a ReviewDecision for each backtest. See module docstring."""

    # Threshold at which a cost_alert is emitted (log + WS broadcast). Fires
    # at most once per UTC day per reviewer. Decoupled from `max_daily_cost`
    # so a reviewer with `enforce_budget=False` (backtest mode) still surfaces
    # spend visibility to the user.
    COST_ALERT_USD = 10.0

    def __init__(
        self,
        anthropic_client: Optional[Any] = None,
        reviewer_model: str = "claude-opus-4-6",
        max_daily_cost: float = 5.0,
        store: Optional[ExperimentStore] = None,
        config_path: Optional[Path] = None,
        max_tokens: int = 1200,
        enforce_budget: bool = True,
        broadcaster: Optional[Any] = None,
        source_root: Optional[Path] = None,
    ) -> None:
        self.client = anthropic_client
        self.model = reviewer_model
        self.max_daily_cost = max_daily_cost
        self.store = store if store is not None else ExperimentStore()
        self.gates = dict(DEFAULT_GATES)
        self.max_tokens = max_tokens
        self.enforce_budget = enforce_budget
        self.broadcaster = broadcaster
        # Root for read_source_file tool — defaults to this module's repo.
        self.source_root = Path(source_root) if source_root else Path(__file__).resolve().parent

        # Cost tracking (Claude Opus list price; override via config).
        # Protected by self._cost_lock — multiple BacktestWorkerPool threads
        # may call review() concurrently.
        self._cost_lock = threading.Lock()
        self._cost_in_per_m = 15.0
        self._cost_out_per_m = 75.0
        self._daily_tokens_in = 0
        self._daily_tokens_out = 0
        self._daily_cost = 0.0
        self._day_key = time.strftime("%Y%m%d", time.gmtime())
        self._cost_alert_fired_day = ""    # UTC day-key the $10 alert last fired for

        # Resolve config path — explicit arg wins; else .hydra-experiments/reviewer_config.json
        # bootstrapped on first run so ops can tune gates without reading code.
        if config_path is None:
            try:
                default_path = self.store.root / "reviewer_config.json"
                if not default_path.exists():
                    _bootstrap_reviewer_config(default_path, self.gates,
                                                self._cost_in_per_m, self._cost_out_per_m)
                config_path = default_path
            except Exception:
                config_path = None

        if config_path and Path(config_path).exists():
            self._load_config(Path(config_path))

    # ─── config loader ───

    def _load_config(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        gates = data.get("gates")
        if isinstance(gates, dict):
            for k, v in gates.items():
                if k in self.gates and isinstance(v, (int, float)):
                    self.gates[k] = float(v)
        cost = data.get("cost", {})
        if isinstance(cost, dict):
            self._cost_in_per_m = float(cost.get("input_per_million", self._cost_in_per_m))
            self._cost_out_per_m = float(cost.get("output_per_million", self._cost_out_per_m))

    # ─── public API ───

    def review(
        self,
        experiment: Experiment,
        baseline_experiment: Optional[Experiment] = None,
    ) -> ReviewDecision:
        """Produce a ReviewDecision. Never raises — exceptions become a
        safe RESULT_ANOMALOUS verdict with the trace in risk_flags.
        """
        try:
            return self._review_inner(experiment, baseline_experiment)
        except Exception as e:
            return ReviewDecision(
                experiment_id=experiment.id,
                reviewed_at=_iso_utc_now(),
                reviewer_model=self.model,
                reviewer_version=REVIEWER_VERSION,
                verdict="RESULT_ANOMALOUS",
                observations=[f"reviewer faulted: {type(e).__name__}"],
                reasoning=str(e),
                risk_flags=["reviewer_exception"],
                confidence="LOW",
                all_gates_passed=False,
                llm_used=False,
            )

    def _review_inner(
        self,
        experiment: Experiment,
        baseline: Optional[Experiment],
    ) -> ReviewDecision:
        if experiment.result is None:
            return ReviewDecision(
                experiment_id=experiment.id,
                reviewed_at=_iso_utc_now(),
                reviewer_model=self.model,
                reviewer_version=REVIEWER_VERSION,
                verdict="RESULT_ANOMALOUS",
                observations=["experiment has no result to review"],
                all_gates_passed=False,
                confidence="LOW",
            )

        # 1. Evidence gathering — expensive passes happen here.
        evidence = self._build_repeatability_evidence(experiment, baseline)

        # 2. Code-enforced gate computation — anti-handwaving.
        gates = self._compute_gates(evidence, experiment, baseline)
        all_passed = all(gates.values())

        # 3. Optional LLM deliberation.
        llm_output: Dict[str, Any] = {}
        tokens_in = 0
        tokens_out = 0
        llm_used = False
        source_files_read: List[str] = []

        if self.client is not None and self._within_budget():
            try:
                llm_output, tokens_in, tokens_out, source_files_read = self._llm_deliberate(
                    experiment, evidence, gates,
                )
                llm_used = bool(llm_output)
            except Exception as e:
                # LLM failure → heuristic-only fallback. Record in risk flags.
                llm_output = {"__llm_error__": f"{type(e).__name__}: {e}"}
                llm_used = False

        # 4. Build decision; apply downgrade logic.
        decision = self._assemble_decision(
            experiment=experiment,
            evidence=evidence,
            gates=gates,
            all_passed=all_passed,
            llm_output=llm_output,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            llm_used=llm_used,
            source_files_read=source_files_read,
        )

        # 5. Persist.
        try:
            self.store.log_review(experiment.id, decision.to_dict())
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

        # 6. PR draft for CODE_REVIEW verdicts (I8-compliant advisory file).
        if decision.verdict == "CODE_REVIEW":
            try:
                write_pr_draft(decision, experiment, self.store.root)
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")

        return decision

    def batch_review(
        self,
        experiment_ids: List[str],
    ) -> List[ReviewDecision]:
        out: List[ReviewDecision] = []
        for eid in experiment_ids:
            try:
                exp = self.store.load(eid)
            except KeyError:
                continue
            out.append(self.review(exp))
        return out

    def self_retrospective(self, lookback_days: int = 30) -> SelfRetrospective:
        """Audit the reviewer's own prior decisions. Reads review_history.jsonl
        and shadow_outcomes.jsonl (from hydra_shadow_validator) to compute
        a reviewer_accuracy_score grounded in real shadow-validation outcomes.

        Accuracy metric: fraction of PARAM_TWEAK verdicts whose originating
        experiment made it through shadow validation with `status="approved"`.
        Denominator is PARAM_TWEAK verdicts that have ANY terminal shadow
        outcome — open / expired candidates don't count. Returns None for
        accuracy when the denominator is zero (no signal).
        """
        path = self.store.root / "review_history.jsonl"
        if not path.exists():
            return SelfRetrospective(
                generated_at=_iso_utc_now(),
                lookback_days=lookback_days,
                recommendations_reviewed=0,
                param_tweaks_proposed=0, code_reviews_proposed=0,
                no_change_verdicts=0, anomalous_verdicts=0,
            )

        cutoff_ts = time.time() - lookback_days * 86400.0
        counts = {"PARAM_TWEAK": 0, "CODE_REVIEW": 0, "NO_CHANGE": 0,
                  "RESULT_ANOMALOUS": 0, "HYPOTHESIS_REFUTED": 0}
        param_tweak_exp_ids: List[str] = []
        total = 0
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_ts = _parse_iso_utc(rec.get("ts", ""))
            if rec_ts is not None and rec_ts < cutoff_ts:
                continue
            verdict = (rec.get("review") or {}).get("verdict", "")
            if verdict in counts:
                counts[verdict] += 1
            if verdict == "PARAM_TWEAK":
                eid = rec.get("exp_id")
                if eid:
                    param_tweak_exp_ids.append(eid)
            total += 1

        # Join against shadow outcomes to compute accuracy.
        shadow_path = self.store.root / "shadow_outcomes.jsonl"
        outcomes_by_exp: Dict[str, str] = {}   # exp_id -> last terminal status
        if shadow_path.exists():
            try:
                for line in shadow_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    eid = out.get("experiment_id")
                    status = out.get("status")
                    if not eid or status not in {"approved", "rejected",
                                                  "expired", "cancelled"}:
                        continue
                    # Later entries for the same experiment win (same review
                    # might be revalidated after a tweak).
                    outcomes_by_exp[eid] = status
            except OSError as e:
                import logging; logging.warning(f"Ignored exception: {e}")

        # Compute accuracy.
        approved = 0
        evaluated = 0
        for eid in param_tweak_exp_ids:
            status = outcomes_by_exp.get(eid)
            if status is None:
                continue            # no outcome yet — don't count either way
            evaluated += 1
            if status == "approved":
                approved += 1

        accuracy: Optional[float] = None
        notes: List[str] = []
        if evaluated == 0:
            notes.append("no terminal shadow outcomes in lookback window")
        else:
            accuracy = approved / evaluated
            notes.append(f"accuracy computed from {approved}/{evaluated} "
                         f"param_tweaks with terminal shadow outcomes")

        return SelfRetrospective(
            generated_at=_iso_utc_now(),
            lookback_days=lookback_days,
            recommendations_reviewed=total,
            param_tweaks_proposed=counts["PARAM_TWEAK"],
            code_reviews_proposed=counts["CODE_REVIEW"],
            no_change_verdicts=counts["NO_CHANGE"],
            anomalous_verdicts=counts["RESULT_ANOMALOUS"],
            reviewer_accuracy_score=accuracy,
            notes=notes,
        )

    # Rolling-accuracy cache used for confidence decay. Recomputed lazily
    # with a TTL so self_retrospective() isn't re-run on every review.
    _ACCURACY_CACHE_TTL_SEC = 300.0       # 5 minutes
    # Confidence penalty threshold: if recent accuracy drops below this,
    # HIGH → MEDIUM downgrade is applied to new decisions. 0.5 means
    # "the reviewer is worse than a coin flip" — a reasonable floor.
    _CONFIDENCE_DECAY_THRESHOLD = 0.5
    # Minimum evaluated sample before we apply decay — prevents noise
    # from the first few validations from suppressing confidence.
    _CONFIDENCE_DECAY_MIN_SAMPLE = 5

    def _recent_accuracy(self) -> Tuple[Optional[float], int]:
        """Cached (accuracy, evaluated_count) over the last 30 days.
        Returns (None, 0) if there's no signal yet.
        """
        now = time.time()
        cached = getattr(self, "_accuracy_cache", None)
        if cached and (now - cached[0]) < self._ACCURACY_CACHE_TTL_SEC:
            return cached[1], cached[2]
        try:
            retro = self.self_retrospective(lookback_days=30)
            acc = retro.reviewer_accuracy_score
            # Re-derive evaluated_count from the note (stable format above).
            evaluated = 0
            for n in retro.notes:
                if "param_tweaks with terminal shadow outcomes" in n:
                    try:
                        parts = n.split()
                        for p in parts:
                            if "/" in p:
                                evaluated = int(p.split("/")[1])
                                break
                    except (ValueError, IndexError) as e:
                        import logging; logging.warning(f"Ignored exception: {e}")
            self._accuracy_cache = (now, acc, evaluated)
            return acc, evaluated
        except Exception:
            return None, 0

    # ─── evidence gathering ───

    def _build_repeatability_evidence(
        self,
        experiment: Experiment,
        baseline: Optional[Experiment],
    ) -> RepeatabilityEvidence:
        result = experiment.result
        assert result is not None

        ev = RepeatabilityEvidence()
        ev.total_trades_in_sample = result.metrics.total_trades

        # Walk-forward — run if not already on experiment. Failures are
        # recorded on the evidence so downstream gates fail informatively.
        wf: Optional[WalkForwardReport] = experiment.wf_report
        if wf is None:
            try:
                wf = walk_forward(experiment.config, n_windows=3)
            except Exception as e:
                wf = None
                ev.run_failures.append(f"wf:{type(e).__name__}")
        if wf is not None:
            ev.wf_slices_tested = wf.n_windows
            ev.wf_improved_slices = wf.improved_slices
            ev.wf_improvement_pct_per_slice = list(wf.improvement_pct_per_slice)
            ev.wf_mean_sharpe = wf.mean_sharpe
            ev.wf_sharpe_stability = wf.sharpe_stability

        # Out-of-sample — reuse or compute. Failures are recorded; the gate
        # still fails (correctly) because oos_gap_pct stays at its default.
        oos: Optional[OutOfSampleReport] = experiment.oos_report
        if oos is None:
            try:
                oos = out_of_sample_gap(experiment.config, in_sample_pct=0.8)
            except Exception as e:
                oos = None
                ev.run_failures.append(f"oos:{type(e).__name__}")
        if oos is not None:
            ev.oos_held_out_pct = 1.0 - oos.in_sample_pct
            ev.in_sample_sharpe = oos.in_sample_sharpe
            ev.oos_sharpe = oos.oos_sharpe
            ev.oos_gap_pct = oos.gap_pct

        # Monte Carlo improvement vs baseline (if provided) or vs self-resample
        mc: Optional[ImprovementReport] = None
        trade_profits = [
            float(t.get("profit", 0.0))
            for t in (result.trade_log or [])
            if t.get("profit") not in (None, 0.0)
        ]
        if baseline is not None and baseline.result is not None:
            baseline_profits = [
                float(t.get("profit", 0.0))
                for t in (baseline.result.trade_log or [])
                if t.get("profit") not in (None, 0.0)
            ]
            if trade_profits and baseline_profits:
                try:
                    mc = monte_carlo_improvement(
                        baseline_profits, trade_profits, n_iter=300,
                    )
                except Exception:
                    mc = None
        elif trade_profits:
            # Fall back to resample-vs-zero: mean_improvement = mean trade P&L
            # with CI that bounds whether the mean is positive.
            try:
                mc_single = monte_carlo_resample(trade_profits, n_iter=300)
                # Synthesize ImprovementReport-shaped evidence
                ev.mc_iterations = mc_single.n_iter
                ev.mc_mean_improvement = statistics.fmean(trade_profits)
                ev.mc_ci_95 = (mc_single.sharpe_ci.lower, mc_single.sharpe_ci.upper)
                ev.mc_std_error = mc_single.sharpe_ci.std_error
                # p-value = fraction of resampled mean sharpes ≤ 0 — we don't
                # have that directly; conservative: 1.0 when mean≤0, 0.0
                # when CI strictly positive, 0.5 otherwise.
                if mc_single.sharpe_ci.lower > 0:
                    ev.mc_p_value = 0.05
                elif ev.mc_mean_improvement <= 0:
                    ev.mc_p_value = 1.0
                else:
                    ev.mc_p_value = 0.5
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")

        if mc is not None:
            ev.mc_iterations = mc.n_iter
            ev.mc_mean_improvement = mc.mean_improvement
            ev.mc_ci_95 = (mc.ci_lower, mc.ci_upper)
            ev.mc_p_value = mc.p_value
            # SE estimate: (upper - lower) / (2 * 1.96) from a 95% CI
            ev.mc_std_error = (mc.ci_upper - mc.ci_lower) / 3.92 if mc.n_iter > 0 else 0.0

        # Cross-pair — from per_pair_metrics
        pairs_total = len(result.per_pair_metrics)
        pairs_improved = 0
        improvement_by_pair: Dict[str, float] = {}
        for pair, m in result.per_pair_metrics.items():
            improvement_by_pair[pair] = round(m.total_return_pct, 4)
            # "Improved" here = positive return (no baseline → self-reference)
            if baseline is not None and baseline.result is not None:
                b_m = baseline.result.per_pair_metrics.get(pair)
                if b_m is not None and m.total_return_pct > b_m.total_return_pct:
                    pairs_improved += 1
            elif m.total_return_pct > 0:
                pairs_improved += 1
        ev.pairs_total = pairs_total
        ev.pairs_improved = pairs_improved
        ev.improvement_by_pair = improvement_by_pair

        # Regime breakdown — single aggregate ribbon concat + trade_log join
        regime_pnl = regime_conditioned_pnl(
            result.trade_log, result.regime_ribbon,
        )
        improvement_by_regime = {r: v["pnl"] for r, v in regime_pnl.items()}
        regimes_improved = sum(1 for r, v in regime_pnl.items() if v["pnl"] > 0)
        # Concentration: max |pnl| share over sum |pnl|
        abs_pnls = [abs(v) for v in improvement_by_regime.values()]
        total_abs = sum(abs_pnls) or 0.0
        if total_abs > 0:
            concentration = max(abs_pnls) / total_abs
            dominant = max(improvement_by_regime, key=lambda k: abs(improvement_by_regime[k]))
        else:
            concentration = 0.0
            dominant = None
        ev.regimes_improved = regimes_improved
        ev.improvement_by_regime = improvement_by_regime
        ev.regime_concentration = concentration
        ev.dominant_regime = dominant

        return ev

    # ─── Gate computation (anti-handwaving core) ───

    def _compute_gates(
        self,
        ev: RepeatabilityEvidence,
        experiment: Experiment,
        baseline: Optional[Experiment],
    ) -> Dict[str, bool]:
        """7 code-enforced rigor gates. See DEFAULT_GATES docstring."""
        g: Dict[str, bool] = {}

        # 1. min_trades_50: result must have ≥ threshold trades
        g["min_trades_50"] = (
            ev.total_trades_in_sample >= self.gates["min_trades_50"]
        )

        # 2. mc_ci_lower_positive: MC lower 95% bound > threshold
        g["mc_ci_lower_positive"] = (
            ev.mc_iterations > 0
            and ev.mc_ci_95[0] > self.gates["mc_ci_lower_positive"]
        )

        # 3. wf_majority_improved: improved_slices / slices_tested ≥ threshold
        if ev.wf_slices_tested > 0:
            ratio = ev.wf_improved_slices / ev.wf_slices_tested
            g["wf_majority_improved"] = ratio >= self.gates["wf_majority_improved"]
        else:
            g["wf_majority_improved"] = False

        # 4. oos_gap_acceptable: |gap_pct| < threshold
        g["oos_gap_acceptable"] = (
            abs(ev.oos_gap_pct) < self.gates["oos_gap_acceptable_pct"]
        )

        # 5. improvement_above_2se: mean > N * SE
        if ev.mc_std_error > 0:
            g["improvement_above_2se"] = (
                ev.mc_mean_improvement
                > self.gates["improvement_above_2se_multiplier"] * ev.mc_std_error
            )
        else:
            g["improvement_above_2se"] = False

        # 6. cross_pair_majority: ≥ threshold of pairs improved
        if ev.pairs_total > 0:
            ratio = ev.pairs_improved / ev.pairs_total
            g["cross_pair_majority"] = ratio >= self.gates["cross_pair_majority"]
        else:
            g["cross_pair_majority"] = False

        # 7. regime_not_concentrated: no single regime dominates > threshold
        g["regime_not_concentrated"] = (
            ev.regime_concentration < self.gates["regime_concentration_threshold"]
        )

        return g

    # ─── LLM deliberation ───

    def _within_budget(self) -> bool:
        # Rollover daily counter at UTC midnight. Protected — multiple
        # BacktestWorkerPool threads may call review() concurrently.
        # `enforce_budget=False` (set on the reviewer for backtest-only
        # workers) bypasses the cap entirely but still rolls counters.
        today = time.strftime("%Y%m%d", time.gmtime())
        with self._cost_lock:
            if today != self._day_key:
                self._day_key = today
                self._daily_tokens_in = 0
                self._daily_tokens_out = 0
                self._daily_cost = 0.0
                self._cost_alert_fired_day = ""
            if not self.enforce_budget:
                return True
            return self._daily_cost < self.max_daily_cost

    # Cap on read_source_file invocations per review — prevents the LLM
    # from burning budget slurping the whole repo.
    _SOURCE_READS_PER_REVIEW = 6
    # Max file bytes returned per read; larger files are truncated with a
    # notice appended so the LLM sees the boundary explicitly.
    _SOURCE_BYTES_CAP = 16_000
    # Max tool-use loop iterations. Safety net against runaway loops.
    _TOOL_LOOP_ITERATIONS_CAP = 6

    def _llm_deliberate(
        self,
        experiment: Experiment,
        evidence: RepeatabilityEvidence,
        gates: Dict[str, bool],
    ) -> Tuple[Dict[str, Any], int, int, List[str]]:
        """Ask Claude for a structured ReviewDecision using tool-use loop.

        Returns (parsed_dict, tokens_in, tokens_out, source_files_read).
        `parsed_dict` is {} on failure. `source_files_read` is the ordered
        list of paths the LLM fetched via read_source_file (allow-listed).
        """
        user_msg = self._build_user_message(experiment, evidence, gates)
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": user_msg},
        ]
        source_files_read: List[str] = []
        reads_remaining = self._SOURCE_READS_PER_REVIEW
        tin_total = 0
        tout_total = 0
        final_text = ""

        for _ in range(self._TOOL_LOOP_ITERATIONS_CAP):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=REVIEWER_PROMPT,
                messages=messages,
                tools=REVIEWER_TOOLS,
                timeout=60.0,
            )
            usage = getattr(response, "usage", None)
            tin_total += getattr(usage, "input_tokens", 0) if usage else 0
            tout_total += getattr(usage, "output_tokens", 0) if usage else 0

            content_blocks = getattr(response, "content", []) or []
            stop_reason = getattr(response, "stop_reason", None)

            # Extract text + tool_use blocks from this response.
            text_out = ""
            tool_uses: List[Dict[str, Any]] = []
            for block in content_blocks:
                btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if btype == "text":
                    text_out += (block.get("text", "") if isinstance(block, dict)
                                  else getattr(block, "text", ""))
                elif btype == "tool_use":
                    tool_uses.append({
                        "id": block.get("id") if isinstance(block, dict) else getattr(block, "id", ""),
                        "name": block.get("name") if isinstance(block, dict) else getattr(block, "name", ""),
                        "input": block.get("input") if isinstance(block, dict) else getattr(block, "input", {}),
                    })

            final_text = text_out  # last text block wins

            if stop_reason != "tool_use":
                # Terminal. If model hit max_tokens mid-thought while a
                # tool_use was pending, surface the oddity — we don't silently
                # drop signal. (Normal end_turn leaves tool_uses empty.)
                if stop_reason == "max_tokens" and tool_uses:
                    print(f"[REVIEWER] max_tokens reached with {len(tool_uses)} "
                          f"pending tool_use block(s); treating as terminal",
                          flush=True)
                break

            # Echo assistant content back (Anthropic requires the full
            # content, including tool_use blocks, on the next turn).
            messages.append({
                "role": "assistant",
                "content": self._serialize_content_for_echo(content_blocks),
            })

            # Dispatch each tool_use → tool_result.
            tool_results_block: List[Dict[str, Any]] = []
            for tu in tool_uses:
                tool_results_block.append(self._dispatch_reviewer_tool(
                    tu, source_files_read, lambda: reads_remaining,
                ))
                # Only read_source_file is rate-limited; decrement when used.
                if tu.get("name") == "read_source_file":
                    reads_remaining -= 1

            messages.append({
                "role": "user",
                "content": tool_results_block,
            })
        else:
            print(f"[REVIEWER] tool-use loop cap ({self._TOOL_LOOP_ITERATIONS_CAP}) "
                  f"reached without end_turn; using last text", flush=True)

        # Account daily cost + fire alert (single-lock write).
        call_cost = self._compute_cost(tin_total, tout_total)
        with self._cost_lock:
            self._daily_tokens_in += tin_total
            self._daily_tokens_out += tout_total
            self._daily_cost += call_cost
            daily_cost_snapshot = self._daily_cost
            day_key_snapshot = self._day_key
            should_alert = (
                daily_cost_snapshot >= self.COST_ALERT_USD
                and self._cost_alert_fired_day != day_key_snapshot
            )
            if should_alert:
                self._cost_alert_fired_day = day_key_snapshot

        if should_alert:
            self._emit_cost_alert(daily_cost_snapshot, day_key_snapshot)

        parsed = _parse_json(final_text) or {}
        return parsed, tin_total, tout_total, source_files_read

    @staticmethod
    def _serialize_content_for_echo(blocks: List[Any]) -> List[Dict[str, Any]]:
        """Convert SDK content blocks into dicts safe to echo back to Anthropic."""
        out: List[Dict[str, Any]] = []
        for b in blocks:
            if isinstance(b, dict):
                out.append(b)
                continue
            btype = getattr(b, "type", None)
            if btype == "text":
                out.append({"type": "text", "text": getattr(b, "text", "")})
            elif btype == "tool_use":
                out.append({
                    "type": "tool_use",
                    "id": getattr(b, "id", ""),
                    "name": getattr(b, "name", ""),
                    "input": getattr(b, "input", {}) or {},
                })
            else:
                # Unknown block type: skip rather than send garbage.
                continue
        return out

    def _dispatch_reviewer_tool(
        self,
        tu: Dict[str, Any],
        source_files_read: List[str],
        reads_remaining_getter: Any,
    ) -> Dict[str, Any]:
        """Route one tool_use to the right handler; always return a
        tool_result block even on error (Anthropic requires pairing)."""
        tool_name = tu.get("name") or ""
        tool_id = tu.get("id") or ""
        tool_input = tu.get("input") or {}
        is_error = False
        try:
            if tool_name == "read_source_file":
                reads_remaining = int(reads_remaining_getter()) if callable(reads_remaining_getter) else 0
                if reads_remaining <= 0:
                    content = ("ERROR: read_source_file quota exhausted "
                               f"({self._SOURCE_READS_PER_REVIEW} per review)")
                    is_error = True
                else:
                    path = str(tool_input.get("path") or "")
                    content = _safe_read_source_file(
                        path, self.source_root, self._SOURCE_BYTES_CAP,
                    )
                    if content.startswith("ERROR:"):
                        is_error = True
                    else:
                        source_files_read.append(path)
            else:
                content = f"ERROR: unknown tool {tool_name!r}"
                is_error = True
        except Exception as e:
            content = f"ERROR: {type(e).__name__}: {e}"
            is_error = True

        block: Dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content[:self._SOURCE_BYTES_CAP],
        }
        if is_error:
            block["is_error"] = True
        return block

    def _compute_cost(self, tin: int, tout: int) -> float:
        """Single source of truth for token→USD conversion."""
        return (tin / 1_000_000 * self._cost_in_per_m
                + tout / 1_000_000 * self._cost_out_per_m)

    def _emit_cost_alert(self, daily_cost: float, day_key: str) -> None:
        """One-shot disclosure when reviewer daily spend crosses COST_ALERT_USD.

        Fires at most once per UTC day via `_cost_alert_fired_day`. Survives
        enforce_budget=False (the point: tell the user regardless of cap).
        """
        msg = (f"[REVIEWER] daily cost ${daily_cost:.2f} has crossed the "
               f"${self.COST_ALERT_USD:.2f}/day disclosure threshold (day={day_key})")
        try:
            print(msg, flush=True)
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        if self.broadcaster is not None and hasattr(self.broadcaster, "broadcast_message"):
            try:
                self.broadcaster.broadcast_message("cost_alert", {
                    "component": "reviewer",
                    "daily_cost_usd": round(daily_cost, 4),
                    "threshold_usd": self.COST_ALERT_USD,
                    "day_key": day_key,
                    "enforce_budget": self.enforce_budget,
                })
            except Exception as e:
                import logging; logging.warning(f"Ignored exception: {e}")

    def _build_user_message(
        self,
        experiment: Experiment,
        ev: RepeatabilityEvidence,
        gates: Dict[str, bool],
    ) -> str:
        m = experiment.result.metrics
        # Compact payload — keep under ~2K tokens
        payload = {
            "experiment": {
                "id": experiment.id,
                "name": experiment.name,
                "hypothesis": experiment.hypothesis,
                "base_preset": experiment.base_preset,
                "overrides": experiment.overrides,
                "pairs": list(experiment.config.pairs),
                "mode": experiment.config.mode,
            },
            "metrics": {
                "total_trades": m.total_trades,
                "total_return_pct": round(m.total_return_pct, 4),
                "sharpe": round(m.sharpe, 4),
                "sortino": round(m.sortino, 4),
                "max_drawdown_pct": round(m.max_drawdown_pct, 4),
                "profit_factor": round(m.profit_factor, 4) if math.isfinite(m.profit_factor) else None,
                "win_rate_pct": round(m.win_rate_pct, 2),
            },
            "repeatability_evidence": asdict(ev),
            "rigor_gates": gates,
            "all_gates_passed": all(gates.values()),
        }
        return (
            "Review the following backtest. The rigor gates are already "
            "computed by the system — your job is verdict + rationale + "
            "proposed changes (bound by the gates).\n\n"
            + json.dumps(payload, indent=2, default=_json_default)
        )

    # ─── Assembly + downgrade ───

    def _assemble_decision(
        self,
        experiment: Experiment,
        evidence: RepeatabilityEvidence,
        gates: Dict[str, bool],
        all_passed: bool,
        llm_output: Dict[str, Any],
        tokens_in: int,
        tokens_out: int,
        llm_used: bool,
        source_files_read: Optional[List[str]] = None,
    ) -> ReviewDecision:
        # Default verdict: heuristic from gates only
        default_verdict = self._heuristic_verdict(gates, all_passed, evidence)

        verdict = llm_output.get("verdict") if llm_used else None
        if verdict not in VALID_VERDICTS:
            verdict = default_verdict

        original_verdict = verdict
        risk_flags: List[str] = list(llm_output.get("risk_flags") or [])

        # Surface any run_failures from evidence gathering so operators can
        # explain gate misses (e.g., wf_majority_improved=False because WF
        # raised ValueError on too-few candles).
        for rf in evidence.run_failures:
            risk_flags.append(f"run_failed:{rf}")

        observations = list(llm_output.get("observations") or [])
        reasoning = str(llm_output.get("reasoning") or "")
        root_cause = str(llm_output.get("root_cause_hypothesis") or "")
        confidence = str(llm_output.get("confidence") or "LOW").upper()
        if confidence not in ("LOW", "MEDIUM", "HIGH"):
            confidence = "LOW"

        proposed_changes = self._parse_proposed_changes(llm_output.get("proposed_changes"))

        # Confidence decay: if recent PARAM_TWEAK accuracy (from shadow
        # outcomes) is below threshold, downgrade HIGH → MEDIUM on this
        # decision. Protects against a reviewer that's gotten optimistic
        # and is now routinely suggesting changes that fail validation.
        recent_acc, evaluated = self._recent_accuracy()
        if (confidence == "HIGH"
                and recent_acc is not None
                and evaluated >= self._CONFIDENCE_DECAY_MIN_SAMPLE
                and recent_acc < self._CONFIDENCE_DECAY_THRESHOLD):
            confidence = "MEDIUM"
            risk_flags.append(
                f"confidence_decayed:recent_accuracy={recent_acc:.2f}"
                f":evaluated={evaluated}"
            )

        # ─── Downgrade logic: anti-handwaving, code-enforced ───
        # Order matters. Most-specific downgrade (regime-scoped) is checked
        # BEFORE the generic all-gates-fail path so a single-gate failure
        # (regime only) routes to CODE_REVIEW instead of RESULT_ANOMALOUS.

        failed_gates = [k for k, v in gates.items() if not v]

        # A) Regime-concentrated improvement is the ONLY failing gate →
        #    scope-down to regime CODE_REVIEW (real signal, narrow scope).
        #    Set-equality (not list) so gate iteration order is irrelevant.
        if (verdict == "PARAM_TWEAK"
                and evidence.dominant_regime is not None
                and set(failed_gates) == {"regime_not_concentrated"}):
            risk_flags.append(
                f"regime_concentrated:{evidence.dominant_regime}:"
                f"{evidence.regime_concentration:.2f}"
            )
            for pc in proposed_changes:
                if pc.scope == "global":
                    pc.scope = f"regime:{evidence.dominant_regime}"
            verdict = "CODE_REVIEW"

        # B) PARAM_TWEAK with any other failing gates → RESULT_ANOMALOUS.
        elif verdict == "PARAM_TWEAK" and not all_passed:
            risk_flags.append(
                f"reviewer_self_contradicted_gates_failed:{','.join(failed_gates)}"
            )
            verdict = "RESULT_ANOMALOUS"

        # C) Prose-only proposed changes (no expected_impact) → CODE_REVIEW.
        if verdict == "PARAM_TWEAK" and proposed_changes:
            if all(not pc.expected_impact for pc in proposed_changes):
                risk_flags.append("prose_only_no_quantitative_evidence")
                verdict = "CODE_REVIEW"

        # D) Claimed expected sharpe delta above MC upper CI → risk flag.
        for pc in proposed_changes:
            claimed = pc.expected_impact.get("sharpe")
            if claimed is not None and evidence.mc_iterations > 0:
                if claimed > evidence.mc_ci_95[1] + 1e-9:
                    risk_flags.append(
                        f"claimed_sharpe_exceeds_mc_upper_ci:{claimed:.3f}>{evidence.mc_ci_95[1]:.3f}"
                    )

        # E) Claimed impact deviation from mc_mean_improvement > 50% → risk.
        if evidence.mc_iterations > 0 and evidence.mc_mean_improvement > 1e-9:
            for pc in proposed_changes:
                sharpe_claim = pc.expected_impact.get("sharpe")
                if sharpe_claim is None:
                    continue
                dev = abs(sharpe_claim - evidence.mc_mean_improvement) / abs(evidence.mc_mean_improvement)
                if dev > 0.5:
                    risk_flags.append(f"impact_deviation_vs_mc:{dev:.2f}")

        # F) Empty proposed_changes with a verdict of PARAM_TWEAK → anomaly.
        if verdict == "PARAM_TWEAK" and not proposed_changes:
            risk_flags.append("param_tweak_without_proposed_changes")
            verdict = "RESULT_ANOMALOUS"

        # Materiality score: normalized Sharpe delta into [0, 1].
        materiality = self._materiality_score(evidence)

        return ReviewDecision(
            experiment_id=experiment.id,
            reviewed_at=_iso_utc_now(),
            reviewer_model=self.model if llm_used else "heuristic",
            reviewer_version=REVIEWER_VERSION,
            verdict=verdict,
            observations=observations,
            root_cause_hypothesis=root_cause,
            reasoning=reasoning,
            proposed_changes=proposed_changes,
            materiality_score=materiality,
            repeatability=evidence,
            gates_passed=gates,
            all_gates_passed=all_passed,
            confidence=confidence,
            risk_flags=risk_flags,
            source_files_read=list(source_files_read or []),
            tokens_used=tokens_in + tokens_out,
            cost_usd=round(self._compute_cost(tokens_in, tokens_out), 4),
            llm_used=llm_used,
            original_verdict=original_verdict if original_verdict != verdict else None,
        )

    @staticmethod
    def _heuristic_verdict(
        gates: Dict[str, bool],
        all_passed: bool,
        ev: RepeatabilityEvidence,
    ) -> str:
        """No-LLM fallback verdict from gates alone."""
        if not gates.get("min_trades_50", False):
            return "RESULT_ANOMALOUS"
        if all_passed and ev.mc_mean_improvement > 0:
            return "PARAM_TWEAK"
        if ev.oos_gap_pct > 100.0:
            return "RESULT_ANOMALOUS"
        if (ev.regime_concentration >= 0.80
                and ev.dominant_regime is not None):
            return "CODE_REVIEW"
        if ev.mc_iterations > 0 and ev.mc_mean_improvement <= 0:
            return "HYPOTHESIS_REFUTED"
        return "NO_CHANGE"

    @staticmethod
    def _materiality_score(ev: RepeatabilityEvidence) -> float:
        # Normalize mean_improvement / max(|upper|, |lower|) into [0, 1]
        if ev.mc_iterations == 0:
            return 0.0
        denom = max(abs(ev.mc_ci_95[0]), abs(ev.mc_ci_95[1]), 1e-9)
        score = ev.mc_mean_improvement / denom
        if math.isnan(score) or math.isinf(score):
            return 0.0
        return max(0.0, min(1.0, score))

    @staticmethod
    def _parse_proposed_changes(raw: Any) -> List[ProposedChange]:
        out: List[ProposedChange] = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                out.append(ProposedChange(
                    change_type=str(item.get("change_type") or "code"),
                    scope=str(item.get("scope") or "global"),
                    target=str(item.get("target") or ""),
                    current_value=item.get("current_value"),
                    proposed_value=item.get("proposed_value"),
                    expected_impact=dict(item.get("expected_impact") or {}),
                    evidence_refs=list(item.get("evidence_refs") or []),
                    rationale=str(item.get("rationale") or ""),
                    risk_notes=str(item.get("risk_notes") or ""),
                ))
            except (TypeError, ValueError):
                continue
        return out


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    import re as _re
    if not text:
        return None
    cleaned = text.strip()
    fence = _re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", cleaned, _re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except Exception as e:
        import logging; logging.warning(f"Ignored exception: {e}")
    # Best-effort: last {...} blob
    match = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return None


def _parse_iso_utc(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        t = time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        return time.mktime(t) - time.timezone
    except (ValueError, TypeError):
        return None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ═══════════════════════════════════════════════════════════════
# Reviewer tool-use: read_source_file + schemas
# ═══════════════════════════════════════════════════════════════

REVIEWER_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "read_source_file",
        "description": (
            "Read one Hydra source or test file, returning its contents as "
            "plain text (UTF-8, truncated to a safe cap). Scoped to this "
            "repository; any path outside the allow-list is refused. Use this "
            "to ground CODE_REVIEW proposals in real code — never infer what "
            "the code does when you can read it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Repo-relative path. Allowed: hydra_*.py at repo root "
                        "and tests/**/*.py. Denied: .env, *config*.json, any "
                        "path containing '..' or an absolute prefix."
                    ),
                },
            },
            "required": ["path"],
        },
    },
]


_SOURCE_ALLOW_GLOBS = (r"^hydra_[A-Za-z0-9_]+\.py$", r"^tests/[A-Za-z0-9_/\-]+\.py$")
_SOURCE_DENY_SUBSTR = (".env", "config.json", "credentials", "secret", "token")


def _safe_read_source_file(
    path_arg: str,
    source_root: Path,
    bytes_cap: int,
) -> str:
    """Allow-list + deny-list resolver for read_source_file.

    Returns file contents (UTF-8) on success or an "ERROR: ..." string.
    Never raises. Never follows symlinks out of the repo. Truncates to
    `bytes_cap` with a trailing "[...truncated at N bytes...]" notice.
    """
    import re as _re
    import posixpath as _pp

    raw = (path_arg or "").strip().replace("\\", "/")
    if not raw:
        return "ERROR: empty path"
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return "ERROR: absolute paths not allowed"
    if ".." in raw.split("/"):
        return "ERROR: parent-dir traversal not allowed"

    # Normalize. posixpath.normpath preserves relative semantics without
    # introducing OS-specific separators.
    norm = _pp.normpath(raw)
    if norm.startswith("..") or norm.startswith("/"):
        return "ERROR: path escapes repo root"

    # Deny-list substring check.
    lowered = norm.lower()
    for needle in _SOURCE_DENY_SUBSTR:
        if needle in lowered:
            return f"ERROR: path matches deny-list ({needle!r})"

    # Allow-list regex check.
    if not any(_re.match(pat, norm) for pat in _SOURCE_ALLOW_GLOBS):
        return ("ERROR: path not in allow-list; allowed are hydra_*.py at "
                "repo root and tests/**/*.py")

    try:
        resolved = (source_root / norm).resolve()
        # Reject symlinks escaping the repo.
        root_resolved = source_root.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            return "ERROR: path resolves outside repo root"
        if not resolved.exists():
            return f"ERROR: file not found: {norm}"
        if not resolved.is_file():
            return f"ERROR: not a regular file: {norm}"
        raw_bytes = resolved.read_bytes()
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

    truncated = False
    if len(raw_bytes) > bytes_cap:
        raw_bytes = raw_bytes[:bytes_cap]
        truncated = True

    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: decode failed: {type(e).__name__}: {e}"

    if truncated:
        text += f"\n\n[...truncated at {bytes_cap} bytes...]"
    return text


def _bootstrap_reviewer_config(
    path: Path,
    default_gates: Dict[str, float],
    cost_in_per_m: float,
    cost_out_per_m: float,
) -> None:
    """Write a default reviewer_config.json to `path` so ops can tune
    thresholds without editing code. Idempotent (caller guards existence)."""
    data = {
        "_description": (
            "Hydra ResultReviewer configuration. Edit and restart the agent. "
            "Unknown keys are ignored; malformed files are silently reverted "
            "to built-in defaults. Regenerate by deleting this file."
        ),
        "gates": dict(default_gates),
        "cost": {
            "input_per_million": cost_in_per_m,
            "output_per_million": cost_out_per_m,
            "_note": (
                "Update these when Anthropic's published Opus pricing changes; "
                "they drive the $10/day disclosure and the cost_usd stamped "
                "on each ReviewDecision."
            ),
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # Bootstrapping is a convenience; failure is non-fatal.
        pass


# ═══════════════════════════════════════════════════════════════
# PR-draft emitter — I8 guarantee lives here.
# ═══════════════════════════════════════════════════════════════

def write_pr_draft(
    decision: "ReviewDecision",
    experiment: "Experiment",
    store_root: Path,
) -> Optional[Path]:
    """Emit a human-readable PR draft markdown for CODE_REVIEW verdicts.

    Writes to `{store_root}/pr_drafts/{experiment_id}_{timestamp}.md`. The
    file is advisory — it names the proposed changes, the evidence, and the
    risk flags, so the operator can open a real PR from a grounded starting
    point. Never touches source files; I8 forbids auto-apply of code changes.

    Returns the path on success, None on failure (reviewer never throws).
    """
    try:
        drafts_dir = store_root / "pr_drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        safe_eid = "".join(c if c.isalnum() or c in "-_" else "_"
                            for c in (experiment.id or "unknown"))[:60]
        out = drafts_dir / f"{safe_eid}_{ts}.md"

        lines: List[str] = []
        lines.append(f"# PR Draft — experiment `{experiment.id}`")
        lines.append("")
        lines.append(f"- **Verdict:** `{decision.verdict}`"
                     + (f" (downgraded from `{decision.original_verdict}`)"
                        if decision.original_verdict else ""))
        lines.append(f"- **Reviewer:** {decision.reviewer_model} "
                     f"({decision.reviewer_version})")
        lines.append(f"- **Reviewed at:** {decision.reviewed_at}")
        lines.append(f"- **Confidence:** {decision.confidence}")
        lines.append(f"- **All gates passed:** `{decision.all_gates_passed}`")
        lines.append(f"- **Hypothesis:** {experiment.hypothesis or '(none)'}")
        lines.append(f"- **Materiality score:** {decision.materiality_score:.3f}")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(decision.reasoning.strip() or "_(no reasoning captured)_")
        if decision.root_cause_hypothesis:
            lines.append("")
            lines.append("### Root-cause hypothesis")
            lines.append("")
            lines.append(decision.root_cause_hypothesis.strip())
        lines.append("")
        lines.append("## Proposed Changes")
        lines.append("")
        if not decision.proposed_changes:
            lines.append("_(reviewer produced no concrete proposals — this is a flag; "
                         "CODE_REVIEW without proposed_changes usually means the "
                         "evidence is ambiguous.)_")
        else:
            for i, pc in enumerate(decision.proposed_changes, 1):
                lines.append(f"### {i}. `{pc.change_type}` @ `{pc.scope}` → `{pc.target}`")
                lines.append("")
                if pc.current_value is not None or pc.proposed_value is not None:
                    lines.append(f"- **Current:** `{pc.current_value}`")
                    lines.append(f"- **Proposed:** `{pc.proposed_value}`")
                if pc.expected_impact:
                    lines.append(f"- **Expected impact:** `{pc.expected_impact}`")
                if pc.evidence_refs:
                    lines.append(f"- **Evidence refs:** {', '.join(f'`{r}`' for r in pc.evidence_refs)}")
                lines.append("")
                if pc.rationale:
                    lines.append(f"**Rationale:** {pc.rationale}")
                    lines.append("")
                if pc.risk_notes:
                    lines.append(f"**Risks:** {pc.risk_notes}")
                    lines.append("")

        if decision.risk_flags:
            lines.append("## Risk Flags")
            lines.append("")
            for rf in decision.risk_flags:
                lines.append(f"- `{rf}`")
            lines.append("")

        if decision.source_files_read:
            lines.append("## Source files consulted")
            lines.append("")
            for sf in decision.source_files_read:
                lines.append(f"- `{sf}`")
            lines.append("")

        lines.append("## Rigor gates")
        lines.append("")
        lines.append("| Gate | Passed |")
        lines.append("|------|--------|")
        for gate_name, passed in decision.gates_passed.items():
            lines.append(f"| `{gate_name}` | {'yes' if passed else 'NO'} |")
        lines.append("")

        lines.append("## Evidence snapshot")
        lines.append("")
        ev = decision.repeatability
        lines.append(f"- total_trades_in_sample: {ev.total_trades_in_sample}")
        lines.append(f"- MC mean_improvement: {ev.mc_mean_improvement:.4f} "
                     f"(95% CI: [{ev.mc_ci_95[0]:.4f}, {ev.mc_ci_95[1]:.4f}], "
                     f"p={ev.mc_p_value:.3f}, iters={ev.mc_iterations})")
        lines.append(f"- Walk-forward: {ev.wf_improved_slices}/{ev.wf_slices_tested} "
                     f"slices improved (mean sharpe {ev.wf_mean_sharpe:.3f})")
        lines.append(f"- Out-of-sample gap: {ev.oos_gap_pct:.2f}% "
                     f"(in-sample sharpe {ev.in_sample_sharpe:.3f} vs "
                     f"oos sharpe {ev.oos_sharpe:.3f})")
        lines.append(f"- Cross-pair improved: {ev.pairs_improved}/{ev.pairs_total}")
        lines.append(f"- Regime concentration: {ev.regime_concentration:.3f} "
                     f"(dominant: {ev.dominant_regime or 'none'})")
        if ev.run_failures:
            lines.append(f"- Run failures: {', '.join(ev.run_failures)}")
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("_Generated by Hydra ResultReviewer — advisory only. I8 "
                     "forbids auto-apply of code changes. Open a real PR from "
                     "this draft after human review._")

        out.write_text("\n".join(lines), encoding="utf-8")
        return out
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# CLI smoke
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    import tempfile
    from hydra_backtest import make_quick_config
    from hydra_experiments import new_experiment, run_experiment

    tmp = Path(tempfile.mkdtemp(prefix="hydra-rev-smoke-"))
    print(f"[reviewer smoke] store: {tmp}")
    store = ExperimentStore(root=tmp)
    reviewer = ResultReviewer(anthropic_client=None, store=store)

    cfg = make_quick_config(name="rev-smoke", n_candles=300, seed=3)
    from dataclasses import replace as _replace
    cfg = _replace(cfg, coordinator_enabled=False)
    exp = new_experiment(name="rev-smoke", config=cfg,
                         hypothesis="smoke test reviewer")
    run_experiment(exp, store=store)
    print(f"[reviewer smoke] trades={exp.result.metrics.total_trades} "
          f"sharpe={exp.result.metrics.sharpe:.3f}")

    decision = reviewer.review(exp)
    print(f"[reviewer smoke] verdict={decision.verdict} "
          f"all_gates_passed={decision.all_gates_passed} "
          f"confidence={decision.confidence}")
    print(f"[reviewer smoke] gates={decision.gates_passed}")
    print(f"[reviewer smoke] materiality={decision.materiality_score:.3f} "
          f"risk_flags={decision.risk_flags}")

    retro = reviewer.self_retrospective(lookback_days=7)
    print(f"[reviewer smoke] retrospective: reviews={retro.recommendations_reviewed} "
          f"param_tweaks={retro.param_tweaks_proposed}")
