import datetime as dt
from hydra_walk_forward import build_quarterly_folds, WalkForwardSpec


def _ts(year, month, day=1):
    return int(dt.datetime(year, month, day, tzinfo=dt.timezone.utc).timestamp())


def test_builds_quarterly_folds():
    spec = WalkForwardSpec(is_lookback_quarters=4)
    folds = build_quarterly_folds(_ts(2022, 1, 1), _ts(2023, 1, 1), spec)
    # 2022 → 4 OOS quarters: Q1 (Jan-Mar), Q2 (Apr-Jun), Q3, Q4. But the FIRST
    # fold needs at least 1 quarter of IS, so Q1 2022 is skipped.
    assert len(folds) == 3
    f0 = folds[0]   # IS = Q1 2022, OOS = Q2 2022
    assert f0.is_start == _ts(2022, 1, 1)
    assert f0.is_end == _ts(2022, 4, 1)
    assert f0.oos_start == _ts(2022, 4, 1)
    assert f0.oos_end == _ts(2022, 7, 1)


def test_is_lookback_capped():
    spec = WalkForwardSpec(is_lookback_quarters=2)
    # 3 years of data; on the last fold, IS should be capped to last 2 quarters.
    folds = build_quarterly_folds(_ts(2020, 1, 1), _ts(2023, 1, 1), spec)
    last = folds[-1]
    is_quarters = (last.is_end - last.is_start) // (90 * 86400)
    assert is_quarters <= 2 + 1   # ±1 for 90-vs-91-day months
