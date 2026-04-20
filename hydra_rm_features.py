#!/usr/bin/env python3
"""HYDRA Risk Manager engine-internal features.

Pure functions that derive portfolio-health signals from data already
available to HydraAgent: engine candle buffers, the order journal,
in-memory balance history, and cross-pair engine handles. Consumed by
`hydra_agent._build_quant_indicators` and surfaced to the Risk Manager
prompt as concrete, articulable flags (replacing RM's prior habit of
producing only "general caution").

════════════════════════════════════════════════════════════════════════
HARD INVARIANT — READ-ONLY / NO SIDE EFFECTS
════════════════════════════════════════════════════════════════════════
Every function here is pure: input dataclasses / lists in, Optional[float]
(or Optional[dict]) out. No mutation. No subprocess. No network. No file
I/O. If a future contributor is tempted to add one, that is a bug and
violates the module's single reason for existence (CLAUDE.md: "Files
that change together should live together").

If `HYDRA_RM_FEATURES_DISABLED=1` in env, callers skip this module
entirely — they should not invoke these functions and then discard
results. The disable check lives in the caller.
════════════════════════════════════════════════════════════════════════

Fields produced (all Optional[float], units as documented per function):
  realized_vol_pct(candles, window_min)   : annualized stddev of log-returns, percent
  drawdown_velocity_pct_per_hr(history)   : peak-to-trough burn rate over trailing window
  fill_rate_24h(journal, now)             : filled / (filled + cancelled + failed), [0,1]
  avg_slippage_bps_24h(journal, now)      : signed (+favorable / -adverse), bps
  cross_pair_corr(returns_a, returns_b)   : Pearson correlation, [-1,1]
  minutes_since_last_trade(journal, now)  : minutes since most recent terminal fill

Returns None whenever input is insufficient for a statistically
meaningful result. Callers pass None straight into the quant_indicators
dict where R10 treats it as missing-field data (distinct from bad
data), and the RM prompt interprets missing fields as "not enough
history to flag on this axis."
"""

import math
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

# Minimum samples for a meaningful Pearson correlation. Below this, the
# confidence interval on r is so wide that the signal misleads more than
# informs. 30 is a conventional floor; our 15m-candle 24h window gives 96.
_CORR_MIN_SAMPLES = 30

# Minimum minutes of balance history before drawdown velocity is computed.
# Less than this and you see startup-noise "drawdowns" that aren't real.
_DDV_MIN_WINDOW_MIN = 10.0

# Seconds → minutes helper (readability only; inline division loses intent).
_SEC_PER_MIN = 60.0

# Minutes in a year (365.25 * 24 * 60) for vol annualization.
_MIN_PER_YEAR = 525960.0


def realized_vol_pct(
    candles: Sequence[Dict],
    window_minutes: int,
) -> Optional[float]:
    """Annualized realized volatility over a trailing window, in percent.

    Args:
        candles: chronological sequence of candle dicts, each with 'close'
            and 'ts' (UNIX seconds). Only the tail inside the window is used.
            Caller passes the engine's own candle buffer; no I/O here.
        window_minutes: how far back to look. The candle duration is
            inferred from the first two candles' 'ts' delta; if fewer
            than 2 candles, returns None.

    Returns:
        Annualized stddev of log-returns × 100, rounded to 2 decimals.
        None if < 3 candles fit the window (stddev of 2 points is
        degenerate; 3 gives 2 returns which is the minimum for sample
        stddev to be non-zero-by-construction).
    """
    if not candles or len(candles) < 3:
        return None
    try:
        candle_minutes = max(1.0, (candles[1]["ts"] - candles[0]["ts"]) / _SEC_PER_MIN)
    except (KeyError, TypeError, IndexError):
        return None

    needed = int(window_minutes / candle_minutes) + 1  # +1 for N-1 returns
    tail = list(candles[-needed:])
    if len(tail) < 3:
        return None

    try:
        log_returns: List[float] = []
        for prev, curr in zip(tail, tail[1:]):
            p0 = float(prev["close"])
            p1 = float(curr["close"])
            if p0 <= 0 or p1 <= 0:
                return None
            log_returns.append(math.log(p1 / p0))
    except (KeyError, TypeError, ValueError):
        return None

    n = len(log_returns)
    if n < 2:
        return None
    mean = sum(log_returns) / n
    var = sum((r - mean) ** 2 for r in log_returns) / (n - 1)  # sample variance
    sigma = math.sqrt(var)
    annualization = math.sqrt(_MIN_PER_YEAR / candle_minutes)
    return round(sigma * annualization * 100.0, 2)


def drawdown_velocity_pct_per_hr(
    history: Iterable[Tuple[float, float]],
    now: float,
    window_minutes: float = 60.0,
) -> Optional[float]:
    """Peak-to-current burn rate over a trailing window, in percent/hour.

    Args:
        history: iterable of (unix_seconds, balance) pairs, chronological
            order not required. Caller typically passes a bounded deque.
        now: current UNIX seconds (caller supplies for testability).
        window_minutes: how far back to look for the peak. Default 60.

    Returns:
        Sign convention: negative = balance falling (real drawdown),
        0.0 = flat or rising, positive = impossible by design (current
        is always <= peak_in_window, since peak is max of window).
        Returns None when the window contains less than
        `_DDV_MIN_WINDOW_MIN` of data or when all samples lie outside
        the window.
    """
    cutoff = now - window_minutes * _SEC_PER_MIN
    in_window = [(ts, bal) for ts, bal in history if ts >= cutoff]
    if not in_window:
        return None
    in_window.sort(key=lambda p: p[0])
    span_min = (in_window[-1][0] - in_window[0][0]) / _SEC_PER_MIN
    if span_min < _DDV_MIN_WINDOW_MIN:
        return None

    peak_ts, peak_bal = max(in_window, key=lambda p: p[1])
    current_ts, current_bal = in_window[-1]
    if peak_bal <= 0:
        return None
    if current_bal >= peak_bal:
        return 0.0
    pct_drop = (current_bal - peak_bal) / peak_bal * 100.0  # negative
    minutes_since_peak = max(1.0, (current_ts - peak_ts) / _SEC_PER_MIN)
    return round(pct_drop * 60.0 / minutes_since_peak, 2)
