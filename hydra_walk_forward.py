"""HYDRA Walk-Forward Methodology — anchored quarterly folds + paired
Wilcoxon signed-rank test. Stdlib only.

This is the kernel for both:
- Mode B (hypothesis lab):     baseline params vs candidate params
- Mode C (release regression): prior version snapshot vs current branch

See docs/superpowers/specs/2026-04-26-research-tab-redesign-design.md §4.6.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class WilcoxonVerdict:
    n: int
    w_plus: float
    w_minus: float
    p_value: float
    candidate_wins: int
    median_delta: float
    verdict: str   # "better" | "worse" | "equivocal"


def wilcoxon_signed_rank(deltas: Sequence[float],
                         alpha: float = 0.05) -> WilcoxonVerdict:
    """Two-sided Wilcoxon signed-rank test on paired-difference samples.

    For n <= 25, uses the exact distribution (enumerate all 2^n sign
    permutations of the ranks). For larger n, uses the normal approximation
    with continuity correction.
    """
    nonzero = [d for d in deltas if d != 0.0]
    n = len(nonzero)
    if n == 0:
        return WilcoxonVerdict(0, 0.0, 0.0, 1.0, 0, 0.0, "equivocal")
    abs_vals = [abs(d) for d in nonzero]
    # Average ranks for ties.
    indexed = sorted(range(n), key=lambda i: abs_vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs_vals[indexed[j + 1]] == abs_vals[indexed[i]]:
            j += 1
        avg = (i + j + 2) / 2.0   # ranks 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero) if d < 0)
    w = min(w_plus, w_minus)
    candidate_wins = sum(1 for d in nonzero if d > 0)
    sorted_nonzero = sorted(nonzero)
    median_delta = sorted_nonzero[n // 2] if n % 2 == 1 else (
        (sorted_nonzero[n // 2 - 1] + sorted_nonzero[n // 2]) / 2.0
    )
    if n <= 25:
        p_value = _exact_p(ranks, w)
    else:
        # Normal approx with continuity correction.
        mean = n * (n + 1) / 4.0
        var = n * (n + 1) * (2 * n + 1) / 24.0
        z = (w - mean + 0.5) / math.sqrt(var)
        # Two-sided.
        p_value = 2.0 * _norm_cdf(-abs(z))
    if p_value < alpha:
        verdict = "better" if median_delta > 0 else "worse"
    else:
        verdict = "equivocal"
    return WilcoxonVerdict(n, w_plus, w_minus, p_value, candidate_wins,
                           median_delta, verdict)


def _exact_p(ranks: Sequence[float], w_observed: float) -> float:
    """Exact two-sided p-value: enumerate all 2^n sign assignments of the
    ranks, compute W- (sum of ranks assigned negative sign), and count how
    many are <= w_observed (or symmetrically >=)."""
    n = len(ranks)
    total = 1 << n
    le = 0
    for mask in range(total):
        s = 0.0
        for i in range(n):
            if mask & (1 << i):
                s += ranks[i]
        if s <= w_observed:
            le += 1
    p_one = le / total
    return min(1.0, 2.0 * p_one)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
