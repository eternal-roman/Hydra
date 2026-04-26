"""Tests for hydra_walk_forward.wilcoxon_signed_rank.

Hand-computed reference values documented inline.
"""
import math
from hydra_walk_forward import wilcoxon_signed_rank


def test_all_positive_deltas():
    """Hand check: deltas = [1, 2, 3, 4, 5]; ranks = 1..5; W+ = 15, W- = 0;
    n=5 means smallest possible W- under H0. Expected p_two_sided ≈ 0.0625
    (exact distribution; not significant at 5%)."""
    v = wilcoxon_signed_rank([1.0, 2.0, 3.0, 4.0, 5.0])
    assert v.n == 5
    assert v.w_minus == 0
    assert v.w_plus == 15
    assert math.isclose(v.p_value, 0.0625, abs_tol=1e-3)


def test_zero_deltas_dropped():
    v = wilcoxon_signed_rank([0.0, 1.0, -1.0, 2.0])
    assert v.n == 3  # zeros dropped per Wilcoxon rule


def test_symmetric_deltas_no_signal():
    v = wilcoxon_signed_rank([1.0, -1.0, 2.0, -2.0, 3.0, -3.0])
    assert v.n == 6
    # Ranks 1,1,2,2,3,3 → W+ = W- = (1+2+3+1+2+3)/2 = 6 (after tied-ranks
    # average splits). For symmetric two-sided test this maximizes p.
    assert math.isclose(v.w_plus, v.w_minus, abs_tol=0.5)
