import json
import sqlite3
from hydra_history_store import HistoryStore
from tools.run_regression import persist_regression_run


def test_persist_creates_run_and_metric_rows(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    run_id = "abc123"
    persist_regression_run(
        store, run_id=run_id, hydra_version="2.20.0", git_sha="deadbeef",
        param_hash="paramX", pair="BTC/USD", grain_sec=3600,
        spec_json=json.dumps({"fold_kind": "quarterly"}),
        per_fold_metrics={
            0: {"sharpe": 1.1, "total_return_pct": 5.0},
            1: {"sharpe": 1.2, "total_return_pct": 6.0},
        },
        aggregate_metrics={"sharpe": 1.15, "total_return_pct": 5.5},
        equity_curve=[(1_700_000_000, 100.0), (1_700_003_600, 101.0)],
        trades=[],
    )
    with sqlite3.connect(str(tmp_path / "h.sqlite")) as conn:
        n_runs = conn.execute("SELECT COUNT(*) FROM regression_run").fetchone()[0]
        n_metrics = conn.execute("SELECT COUNT(*) FROM regression_metrics").fetchone()[0]
        n_curve = conn.execute("SELECT COUNT(*) FROM regression_equity_curve").fetchone()[0]
    assert n_runs == 1
    # 2 folds × 2 metrics each = 4 per_fold rows + 2 aggregate rows = 6
    assert n_metrics == 6
    assert n_curve == 2
