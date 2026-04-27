"""Tests for walk-forward runner (T13)."""
import datetime as dt
from hydra_walk_forward import (
    run_walk_forward, WalkForwardSpec, FoldMetrics, FoldResult
)


def _ts(y, m, d=1):
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp())


def test_runner_returns_per_fold_results_with_wilcoxon():
    """Use a deterministic fake runner: candidate always beats baseline by
    +0.1 Sharpe. Wilcoxon should call this BETTER for any n>=6."""
    def fake_runner(pair, params, fold):
        is_baseline = params.get("is_baseline", False)
        return FoldMetrics(
            sharpe=1.0 if is_baseline else 1.1,
            total_return_pct=10.0 if is_baseline else 11.0,
            max_dd_pct=5.0,
            fee_adj_return_pct=9.0 if is_baseline else 10.0,
            n_trades=10,
        )

    result = run_walk_forward(
        pair="BTC/USD",
        history_start_ts=_ts(2020, 1, 1),
        history_end_ts=_ts(2023, 1, 1),
        baseline_params={"is_baseline": True},
        candidate_params={"is_baseline": False},
        spec=WalkForwardSpec(is_lookback_quarters=4),
        runner=fake_runner,
    )
    assert len(result.folds) >= 6
    assert result.wilcoxon["sharpe"].verdict == "better"
    assert result.wilcoxon["sharpe"].candidate_wins == len(result.folds)
