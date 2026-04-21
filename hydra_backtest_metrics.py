#!/usr/bin/env python3
"""
HYDRA Backtest — Advanced Metrics (Phase 2 of v2.10.0 backtest platform).

Institutional-grade robustness analytics for backtest results. Pure Python
stdlib (no numpy/scipy) so the engine's "zero dependencies" stance extends
to the metrics layer. See docs/BACKTEST_SPEC.md §6.2.

Functions
---------
annualization_factor(candle_interval_min)
    sqrt(365·24·60 / interval) — the factor we scale mean/std of per-bar
    returns by to get annualized Sharpe/Sortino.

bootstrap_ci(values, n_iter, ci, seed)
    Vanilla percentile bootstrap for the mean of `values`.

monte_carlo_resample(trade_profits, n_iter, block_len, seed, candle_interval_min)
    Block bootstrap over realized trade profits. Returns CIs for:
      total_return_pct, sharpe, max_drawdown_pct, profit_factor.

monte_carlo_improvement(baseline_profits, variant_profits, n_iter, block_len, seed)
    The "MC-on-delta" pass used by the reviewer's RepeatabilityEvidence:
    resamples both sides jointly and reports CI + p-value for the delta in
    mean-per-trade.

regime_conditioned_pnl(trade_log, regime_ribbon)
    Per-regime {pnl, trades, win_rate, avg_pnl} dict — evidence for the
    "regime_not_concentrated" rigor gate.

walk_forward(base_config, train_pct, test_pct, n_windows)
    Slides train/test windows across the full candle series; returns a
    WalkForwardReport with per-slice Sharpe + stability metrics.

out_of_sample_gap(base_config, in_sample_pct)
    First N% vs last (1-N)% of the candle series; reports
    (in_sharpe, oos_sharpe, gap_pct).

parameter_sensitivity(base_config, param_ranges, n_values)
    Linear sweep of each param; returns normalized |∂sharpe/∂param|.

Design invariants
-----------------
- Deterministic: all functions seeded; same inputs → identical outputs (I12).
- Stdlib only: no numpy/pandas/scipy (inherits engine stance).
- Safe reuse of Phase-1 BacktestRunner via its `sources_override` hook — no
  duplication of `_loop`, preserving I7 (zero drift) across slice/OOS paths.
- Live-state safe: consumes only completed BacktestResult / trade_log data;
  never holds refs to live agent state (I2).
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from hydra_engine import Candle
from hydra_backtest import (
    BacktestConfig,
    BacktestResult,
    BacktestRunner,
    CandleSource,
    make_candle_source,
)


# ═══════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════

@dataclass
class WalkForwardSlice:
    """Per-window record from a walk-forward run.

    `window_index` — 0-based position of this slice across the full series.
    `candles_start/end` — [start, end) indices into the materialized candle list.
    """

    window_index: int
    candles_start: int
    candles_end: int
    total_trades: int
    total_return_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    final_equity: float


@dataclass
class WalkForwardReport:
    n_windows: int
    train_pct: float
    test_pct: float
    slices: List[WalkForwardSlice] = field(default_factory=list)
    mean_sharpe: float = 0.0
    std_sharpe: float = 0.0
    sharpe_stability: float = 0.0  # std / |mean| — lower is more stable
    improved_slices: int = 0       # count where sharpe > 0
    improvement_pct_per_slice: List[float] = field(default_factory=list)


@dataclass
class MonteCarloCI:
    lower: float
    upper: float
    mean: float
    std_error: float


@dataclass
class MonteCarloReport:
    n_iter: int
    block_len: int
    total_return_ci: MonteCarloCI
    sharpe_ci: MonteCarloCI
    max_drawdown_ci: MonteCarloCI
    profit_factor_ci: MonteCarloCI


@dataclass
class ImprovementReport:
    n_iter: int
    mean_improvement: float       # mean(variant) - mean(baseline) across resamples
    ci_lower: float
    ci_upper: float
    p_value: float                # P(delta ≤ 0) estimated from the resample distribution
    variant_mean: float
    baseline_mean: float


@dataclass
class OutOfSampleReport:
    in_sample_pct: float
    in_sample_sharpe: float
    oos_sharpe: float
    in_sample_return_pct: float
    oos_return_pct: float
    gap_pct: float                # (in_sample - oos) / |in_sample| * 100, 0 if in_sample=0
    in_sample_trades: int
    oos_trades: int


@dataclass
class ParamSensitivity:
    param: str
    scope: str                    # "global" | "pair:SOL/USDC" | ...
    values: List[float]
    sharpes: List[float]
    sensitivity: float            # |slope| * (max - min) — normalized
    best_value: float
    best_sharpe: float


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def annualization_factor(candle_interval_min: int) -> float:
    """sqrt(365·24·60 / interval_min). Same formula used in live Sharpe/Sortino."""
    if candle_interval_min <= 0:
        raise ValueError("candle_interval_min must be positive")
    return math.sqrt((365.0 * 24.0 * 60.0) / float(candle_interval_min))


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile on a pre-sorted sequence; pct in [0,1]."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = pct * (len(sorted_vals) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def bootstrap_ci(
    values: Sequence[float],
    n_iter: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> Tuple[float, float]:
    """Percentile bootstrap CI for the mean of `values`. Empty input → (0, 0)."""
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    if not (0 < ci < 1):
        raise ValueError("ci must be in (0, 1)")
    rng = random.Random(seed)
    n = len(values)
    means: List[float] = []
    for _ in range(n_iter):
        sample_sum = 0.0
        for _i in range(n):
            sample_sum += values[rng.randint(0, n - 1)]
        means.append(sample_sum / n)
    means.sort()
    alpha = (1 - ci) / 2
    return (_percentile(means, alpha), _percentile(means, 1 - alpha))


def _returns_from_profits(profits: Sequence[float], starting_equity: float) -> Tuple[List[float], List[float]]:
    """Convert a sequence of per-trade profit dollars into (equity_curve, returns).

    Returns are relative per-trade increments (trade_pnl / prior_equity). Equity
    is cumulative starting from `starting_equity`.
    """
    equity = [starting_equity]
    returns: List[float] = []
    for p in profits:
        prev = equity[-1]
        if prev <= 0:
            returns.append(0.0)
            equity.append(prev + p)
            continue
        returns.append(p / prev)
        equity.append(prev + p)
    return equity, returns


def _sharpe_from_returns(returns: Sequence[float], annual_factor: float) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)  # pop stdev is fine for resamples of fixed length
    if sd <= 0:
        return 0.0
    return (mean / sd) * annual_factor


def _max_dd_from_equity(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > worst:
                worst = dd
    return worst


def _profit_factor(profits: Sequence[float]) -> float:
    gains = sum(p for p in profits if p > 0)
    losses = -sum(p for p in profits if p < 0)
    if losses <= 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def _block_bootstrap_sample(
    profits: Sequence[float],
    block_len: int,
    rng: random.Random,
) -> List[float]:
    """NON-CIRCULAR block resample preserving local temporal structure.

    For each block draw, sample a start index uniformly from the set of
    valid starts (0..n-block_len) so every block fits without wrapping.
    Emit block_len consecutive profits; repeat until length ≥ n; truncate.
    Blocks within a single resample are drawn independently, so two draws
    can share overlapping ranges — "non-circular" refers to wrap-around,
    not cross-draw disjointness.

    Fix 4: previously used `(start + j) % n` circular indexing, which
    joined tail-of-sequence to head-of-sequence inside a block. For small
    trade counts (n ≤ ~50) this was effectively IID and yielded CIs that
    were too narrow — rigor gate `mc_ci_lower_positive` passed marginal
    strategies. Non-circular blocks preserve the intended autocorrelation
    structure of the original sequence.
    """
    n = len(profits)
    if n == 0:
        return []
    if block_len <= 0 or block_len >= n:
        # Degenerate: fall back to iid bootstrap so the call still produces a sample
        return [profits[rng.randint(0, n - 1)] for _ in range(n)]
    max_start = n - block_len  # inclusive upper bound — no wrap needed
    sample: List[float] = []
    while len(sample) < n:
        start = rng.randint(0, max_start)
        sample.extend(profits[start:start + block_len])
    return sample[:n]


# ═══════════════════════════════════════════════════════════════
# Monte Carlo resampling
# ═══════════════════════════════════════════════════════════════

def monte_carlo_resample(
    trade_profits: Sequence[float],
    n_iter: int = 500,
    block_len: int = 20,
    seed: int = 42,
    candle_interval_min: int = 15,
    starting_equity: float = 100.0,
) -> MonteCarloReport:
    """Block bootstrap over realized trade profits.

    For each iteration, resample a same-length trade sequence via block
    bootstrap (block_len preserves short-horizon autocorrelation), then
    recompute total_return / sharpe / max_dd / profit_factor. Return 95% CIs.

    Note: the sharpe here is computed on trade-level returns, not candle-level.
    Reviewer uses this to bound the significance of its observed improvement.
    """
    if not trade_profits:
        empty = MonteCarloCI(0.0, 0.0, 0.0, 0.0)
        return MonteCarloReport(
            n_iter=0, block_len=block_len,
            total_return_ci=empty, sharpe_ci=empty,
            max_drawdown_ci=empty, profit_factor_ci=empty,
        )

    rng = random.Random(seed)
    af = annualization_factor(candle_interval_min)

    total_returns: List[float] = []
    sharpes: List[float] = []
    max_dds: List[float] = []
    pfs: List[float] = []

    for _ in range(n_iter):
        sample = _block_bootstrap_sample(trade_profits, block_len, rng)
        equity, returns = _returns_from_profits(sample, starting_equity)
        total_returns.append((equity[-1] - starting_equity) / starting_equity * 100.0)
        sharpes.append(_sharpe_from_returns(returns, af))
        max_dds.append(_max_dd_from_equity(equity))
        pfs.append(_profit_factor(sample))

    def _ci(values: List[float]) -> MonteCarloCI:
        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return MonteCarloCI(0.0, 0.0, 0.0, 0.0)
        finite_sorted = sorted(finite)
        lo = _percentile(finite_sorted, 0.025)
        hi = _percentile(finite_sorted, 0.975)
        mu = statistics.fmean(finite)
        se = statistics.pstdev(finite) if len(finite) > 1 else 0.0
        return MonteCarloCI(lo, hi, mu, se)

    return MonteCarloReport(
        n_iter=n_iter,
        block_len=block_len,
        total_return_ci=_ci(total_returns),
        sharpe_ci=_ci(sharpes),
        max_drawdown_ci=_ci(max_dds),
        profit_factor_ci=_ci(pfs),
    )


def monte_carlo_improvement(
    baseline_profits: Sequence[float],
    variant_profits: Sequence[float],
    n_iter: int = 500,
    block_len: int = 20,
    seed: int = 42,
) -> ImprovementReport:
    """Resample both sequences jointly; report CI and p-value for the delta
    in mean per-trade P&L.

    p_value = fraction of resamples where variant mean ≤ baseline mean.
    mc_ci_lower > 0 is the reviewer's statistical gate for a positive result.
    """
    if not baseline_profits or not variant_profits:
        return ImprovementReport(
            n_iter=0, mean_improvement=0.0, ci_lower=0.0, ci_upper=0.0,
            p_value=1.0, variant_mean=0.0, baseline_mean=0.0,
        )

    rng = random.Random(seed)
    deltas: List[float] = []
    for _ in range(n_iter):
        b = _block_bootstrap_sample(baseline_profits, block_len, rng)
        v = _block_bootstrap_sample(variant_profits, block_len, rng)
        delta = statistics.fmean(v) - statistics.fmean(b)
        deltas.append(delta)

    deltas.sort()
    lo = _percentile(deltas, 0.025)
    hi = _percentile(deltas, 0.975)
    mean_d = statistics.fmean(deltas)
    neg_or_zero = sum(1 for d in deltas if d <= 0)
    p_value = neg_or_zero / len(deltas)

    return ImprovementReport(
        n_iter=n_iter,
        mean_improvement=mean_d,
        ci_lower=lo,
        ci_upper=hi,
        p_value=p_value,
        variant_mean=statistics.fmean(variant_profits),
        baseline_mean=statistics.fmean(baseline_profits),
    )


# ═══════════════════════════════════════════════════════════════
# Regime-conditioned P&L
# ═══════════════════════════════════════════════════════════════

def regime_conditioned_pnl(
    trade_log: Sequence[Dict[str, Any]],
    regime_ribbon: Dict[str, List[str]],
) -> Dict[str, Dict[str, Any]]:
    """Attribute each realized trade's P&L to the regime active at its tick.

    Trade attribution: a trade's `profit` field is set on close (SELL). We
    look up the regime at `trade.tick` on its pair. Ribbons index by tick;
    defensive fallback to the nearest valid tick if the tick exceeds ribbon
    length (shouldn't happen, but keeps the math safe).

    Returns: {regime: {"pnl": float, "trades": int, "wins": int,
                       "losses": int, "win_rate_pct": float, "avg_pnl": float}}
    """
    agg: Dict[str, Dict[str, float]] = {}
    for t in trade_log:
        profit = t.get("profit")
        if profit is None or profit == 0:
            # SELL with nonzero realized P&L is what we attribute
            # (BUY entries carry profit=0 in the Phase 1 trade_log)
            continue
        pair = t.get("pair")
        tick = t.get("tick", 0)
        ribbon = regime_ribbon.get(pair, [])
        if not ribbon:
            continue
        idx = min(max(0, int(tick)), len(ribbon) - 1)
        regime = ribbon[idx] or "RANGING"

        bucket = agg.setdefault(regime, {
            "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0,
        })
        bucket["pnl"] += float(profit)
        bucket["trades"] += 1
        if profit > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1

    out: Dict[str, Dict[str, Any]] = {}
    for regime, bucket in agg.items():
        trades = int(bucket["trades"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        denom = wins + losses
        out[regime] = {
            "pnl": bucket["pnl"],
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (wins / denom * 100.0) if denom > 0 else 0.0,
            "avg_pnl": (bucket["pnl"] / trades) if trades > 0 else 0.0,
        }
    return out


# ═══════════════════════════════════════════════════════════════
# Walk-forward & OOS
# ═══════════════════════════════════════════════════════════════

class ListCandleSource(CandleSource):
    """In-memory candle source. Yields a pre-materialized list per pair.

    Used by walk_forward / out_of_sample_gap to feed sliced candle views into
    a BacktestRunner without duplicating `_loop`. Kept in metrics module (not
    hydra_backtest.py) because it's only meaningful when you already have
    materialized candles in hand.
    """

    def __init__(self, candles_by_pair: Dict[str, List[Candle]], label: str = "list") -> None:
        self._candles = candles_by_pair
        self._label = label

    def iter_candles(self, pair: str) -> Iterator[Candle]:
        for c in self._candles.get(pair, []):
            yield c

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": "list",
            "label": self._label,
            "counts": {p: len(v) for p, v in self._candles.items()},
        }


def _materialize_candles(cfg: BacktestConfig) -> Dict[str, List[Candle]]:
    """Pull the full candle series per pair once, so downstream slicers don't
    re-hit the source (fast for synthetic, rate-limit-friendly for Kraken)."""
    out: Dict[str, List[Candle]] = {}
    for pair in cfg.pairs:
        src = make_candle_source(cfg)
        out[pair] = list(src.iter_candles(pair))
    return out


def _final_equity(result: BacktestResult) -> float:
    total = 0.0
    for _pair, curve in result.equity_curve.items():
        if curve:
            total += curve[-1]
    return total


def _slice_length(full: Dict[str, List[Candle]]) -> int:
    # walk_forward iterates over the SHORTEST pair series to stay aligned;
    # per-pair candles are time-aligned in BacktestRunner.
    return min((len(v) for v in full.values()), default=0)


def walk_forward(
    base_config: BacktestConfig,
    train_pct: float = 0.6,
    test_pct: float = 0.4,
    n_windows: int = 5,
) -> WalkForwardReport:
    """Slide train+test windows across the full candle series.

    Window layout (indices into the materialized candle list):
        window_i: [start_i, start_i + (train+test)*W)
        test segment: last test_pct fraction of the window
    Backtest is run on the TEST segment only; training is nominal (params
    already baked into config — parameter fitting happens upstream).

    n_windows=5, train_pct=0.6, test_pct=0.4 → windows of 100% size each,
    stepped by (total - window) / (n-1). For a 1000-candle series this is
    effectively one full-size run per seed offset.

    Typical usage (reviewer): n_windows=5, train_pct=0.7, test_pct=0.3 over
    a 5000-candle history → 5 test slices of 1500 candles each.
    """
    if n_windows < 1:
        raise ValueError("n_windows must be ≥ 1")
    if not (0 < train_pct < 1) or not (0 < test_pct <= 1):
        raise ValueError("train_pct / test_pct must be in (0, 1]")

    full = _materialize_candles(base_config)
    total_len = _slice_length(full)
    if total_len == 0:
        return WalkForwardReport(n_windows=0, train_pct=train_pct, test_pct=test_pct)

    window_size = max(1, int(total_len * (train_pct + test_pct)))
    window_size = min(window_size, total_len)
    test_size = max(1, int(window_size * test_pct / (train_pct + test_pct)))
    # Step evenly; windows overlap when window_size > (total / n).
    if n_windows == 1:
        step = 0
    else:
        step = max(1, (total_len - window_size) // (n_windows - 1))

    slices: List[WalkForwardSlice] = []
    for i in range(n_windows):
        start = min(i * step, max(0, total_len - window_size))
        end = start + window_size
        if end > total_len:
            end = total_len
        test_start = end - test_size

        sliced_by_pair = {p: full[p][test_start:end] for p in base_config.pairs}
        sources_override = {
            p: ListCandleSource({p: sliced_by_pair[p]}, label=f"wf_{i}")
            for p in base_config.pairs
        }
        runner = BacktestRunner(base_config, sources_override=sources_override)
        result = runner.run()
        slices.append(WalkForwardSlice(
            window_index=i,
            candles_start=test_start,
            candles_end=end,
            total_trades=result.metrics.total_trades,
            total_return_pct=result.metrics.total_return_pct,
            sharpe=result.metrics.sharpe,
            sortino=result.metrics.sortino,
            max_drawdown_pct=result.metrics.max_drawdown_pct,
            final_equity=_final_equity(result),
        ))

    # Aggregate
    sharpes = [s.sharpe for s in slices if math.isfinite(s.sharpe)]
    improvement_pcts = [s.total_return_pct for s in slices]
    improved = sum(1 for s in slices if s.sharpe > 0)
    mean_sh = statistics.fmean(sharpes) if sharpes else 0.0
    std_sh = statistics.pstdev(sharpes) if len(sharpes) > 1 else 0.0
    stability = (std_sh / abs(mean_sh)) if abs(mean_sh) > 1e-9 else (std_sh if std_sh > 0 else 0.0)

    return WalkForwardReport(
        n_windows=n_windows,
        train_pct=train_pct,
        test_pct=test_pct,
        slices=slices,
        mean_sharpe=mean_sh,
        std_sharpe=std_sh,
        sharpe_stability=stability,
        improved_slices=improved,
        improvement_pct_per_slice=improvement_pcts,
    )


def out_of_sample_gap(
    base_config: BacktestConfig,
    in_sample_pct: float = 0.8,
) -> OutOfSampleReport:
    """Split candle series at `in_sample_pct`; run backtest on each half
    separately; report Sharpe gap.

    Gap > 30% is a red flag for overfitting and fails the `oos_gap_acceptable`
    rigor gate in the reviewer.
    """
    if not (0.0 < in_sample_pct < 1.0):
        raise ValueError("in_sample_pct must be in (0, 1)")

    full = _materialize_candles(base_config)
    total_len = _slice_length(full)
    if total_len < 2:
        return OutOfSampleReport(
            in_sample_pct=in_sample_pct,
            in_sample_sharpe=0.0, oos_sharpe=0.0,
            in_sample_return_pct=0.0, oos_return_pct=0.0,
            gap_pct=0.0, in_sample_trades=0, oos_trades=0,
        )

    split = max(1, min(total_len - 1, int(total_len * in_sample_pct)))

    in_sources = {p: ListCandleSource({p: full[p][:split]}, label="is") for p in base_config.pairs}
    oos_sources = {p: ListCandleSource({p: full[p][split:]}, label="oos") for p in base_config.pairs}

    in_result = BacktestRunner(base_config, sources_override=in_sources).run()
    oos_result = BacktestRunner(base_config, sources_override=oos_sources).run()

    in_sh = in_result.metrics.sharpe
    oos_sh = oos_result.metrics.sharpe
    gap = ((in_sh - oos_sh) / abs(in_sh) * 100.0) if abs(in_sh) > 1e-9 else 0.0

    return OutOfSampleReport(
        in_sample_pct=in_sample_pct,
        in_sample_sharpe=in_sh,
        oos_sharpe=oos_sh,
        in_sample_return_pct=in_result.metrics.total_return_pct,
        oos_return_pct=oos_result.metrics.total_return_pct,
        gap_pct=gap,
        in_sample_trades=in_result.metrics.total_trades,
        oos_trades=oos_result.metrics.total_trades,
    )


# ═══════════════════════════════════════════════════════════════
# Parameter sensitivity
# ═══════════════════════════════════════════════════════════════

def _linspace(low: float, high: float, n: int) -> List[float]:
    if n <= 1:
        return [low]
    step = (high - low) / (n - 1)
    return [low + i * step for i in range(n)]


def _apply_param(cfg: BacktestConfig, pair: str, param: str, value: float) -> BacktestConfig:
    """Return a new config with overrides[pair][param] = value."""
    import json as _json
    overrides = dict(cfg.param_overrides)
    pair_ov = dict(overrides.get(pair, {}))
    pair_ov[param] = float(value)
    overrides[pair] = pair_ov
    return replace(cfg, param_overrides_json=_json.dumps(overrides))


def parameter_sensitivity(
    base_config: BacktestConfig,
    param_ranges: Dict[str, Tuple[float, float]],
    n_values: int = 5,
    pair: Optional[str] = None,
) -> Dict[str, ParamSensitivity]:
    """Sparse linear sweep of each param in `param_ranges`.

    For each param, run `n_values` backtests across its range and report:
      sensitivity = max |dSharpe/dParam| * (high - low)
      best_value = param value that produced max Sharpe

    `pair` defaults to the first pair in base_config.
    Caller is responsible for sweep budget — n_params * n_values backtests
    are spawned (serial; parallelism is a Phase 6 concern via the worker pool).
    """
    if not param_ranges:
        return {}
    target_pair = pair or base_config.pairs[0]
    out: Dict[str, ParamSensitivity] = {}

    for param, (low, high) in param_ranges.items():
        if high <= low:
            continue
        values = _linspace(low, high, n_values)
        sharpes: List[float] = []
        for v in values:
            cfg = _apply_param(base_config, target_pair, param, v)
            result = BacktestRunner(cfg).run()
            sharpes.append(result.metrics.sharpe)

        # Finite-difference slope magnitude, normalized by range
        max_slope = 0.0
        for i in range(1, len(values)):
            dv = values[i] - values[i - 1]
            if dv == 0:
                continue
            slope = abs((sharpes[i] - sharpes[i - 1]) / dv)
            if slope > max_slope:
                max_slope = slope
        sensitivity = max_slope * (high - low)
        best_idx = max(range(len(sharpes)), key=lambda i: sharpes[i])

        out[param] = ParamSensitivity(
            param=param,
            scope=f"pair:{target_pair}",
            values=values,
            sharpes=sharpes,
            sensitivity=sensitivity,
            best_value=values[best_idx],
            best_sharpe=sharpes[best_idx],
        )
    return out


# ═══════════════════════════════════════════════════════════════
# CLI smoke (no external deps — synthetic only)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    from hydra_backtest import make_quick_config

    cfg = make_quick_config(name="metrics-smoke", n_candles=300, seed=7)
    print("[metrics smoke] walk_forward…")
    wf = walk_forward(cfg, train_pct=0.6, test_pct=0.4, n_windows=3)
    print(f"  n={wf.n_windows} mean_sharpe={wf.mean_sharpe:.3f} stability={wf.sharpe_stability:.3f}")
    print("[metrics smoke] out_of_sample_gap…")
    oos = out_of_sample_gap(cfg, in_sample_pct=0.8)
    print(f"  in={oos.in_sample_sharpe:.3f} oos={oos.oos_sharpe:.3f} gap={oos.gap_pct:.1f}%")
    print("[metrics smoke] bootstrap_ci on synthetic returns…")
    vals = [0.01, -0.005, 0.02, -0.01, 0.015, 0.008, -0.003, 0.012, 0.0, 0.005]
    lo, hi = bootstrap_ci(vals, n_iter=500, seed=1)
    print(f"  mean CI 95%: [{lo:.5f}, {hi:.5f}]")
    print("[metrics smoke] done.")
