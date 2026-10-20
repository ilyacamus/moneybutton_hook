import math
import random

import pytest

from hookbot.core import stats


def test_quantiles_and_ranks():
    assert stats.median([3, 1, 2]) == 2
    assert stats.quantile([1, 2, 3, 4], 0.25) == pytest.approx(1.75)  # NumPy default
    assert stats.rankdata([10, 20, 20, 5]) == [2, 3.5, 3.5, 1]
    assert stats.percentile_rank([1, 2, 3, 4], 3) == pytest.approx(62.5)


def test_correlations():
    x = [1, 2, 3, 4, 5]
    assert stats.spearman(x, [v**3 for v in x]) == pytest.approx(1)
    assert stats.pearson(x, [5, 4, 3, 2, 1]) == pytest.approx(-1)
    assert stats.pearson([1, 1, 1], [1, 2, 3]) is None


def test_mann_whitney_matches_reference_values():
    # SciPy: mannwhitneyu([1,2,3,4,5],[6,7,8,9,10], method="asymptotic", use_continuity=False) -> U=0, p≈0.0090
    u, p = stats.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert u == 0 and p == pytest.approx(0.0090, abs=5e-4)
    u, p = stats.mann_whitney([1, 2, 3], [1, 2, 3])
    assert p == pytest.approx(1.0)


def test_effect_sizes():
    assert stats.cliffs_delta([2, 3], [1, 1]) == 1
    assert stats.auc([0.9, 0.8], [0.1, 0.85]) == pytest.approx(0.75)


def test_sign_test_exact():
    assert stats.sign_test(10, 0) == pytest.approx(2 / 1024)
    assert stats.sign_test(5, 5) == 1.0


def test_benjamini_hochberg():
    # R: p.adjust(c(0.01, 0.04, 0.03, 0.2), "BH") -> 0.04 0.0533 0.0533 0.2
    q = stats.benjamini_hochberg([0.01, 0.04, 0.03, 0.2])
    assert q == pytest.approx([0.04, 0.0533333, 0.0533333, 0.2], abs=1e-6)


def test_bootstrap_ci_contains_true_mean():
    rng = random.Random(1)
    data = [rng.gauss(10, 2) for _ in range(200)]
    lo, hi = stats.bootstrap_ci(data, stats.mean, n_boot=500)
    assert lo < 10 < hi and hi - lo < 1.5
    lo, hi = stats.bootstrap_diff_ci([5, 6, 7, 8], [1, 2, 3, 4], n_boot=500)
    assert 2 < lo <= hi < 6


def test_logistic_regression_recovers_direction():
    rng = random.Random(3)
    xs, ys = [], []
    for _ in range(400):
        a, b = rng.gauss(0, 1), rng.gauss(0, 1)
        p = 1 / (1 + math.exp(-(0.5 + 2 * a)))
        xs.append([a, b])
        ys.append(1 if rng.random() < p else 0)
    intercept, (wa, wb) = stats.logistic_regression(xs, ys, l2=0.1)
    assert wa == pytest.approx(2, abs=0.5) and abs(wb) < 0.4 and intercept == pytest.approx(0.5, abs=0.35)


def test_icc_reference_example():
    # Shrout & Fleiss (1979), Table 2: ICC(2,1) = 0.29
    ratings = [[9, 2, 5, 8], [6, 1, 3, 2], [8, 4, 6, 8], [7, 1, 2, 6], [10, 5, 6, 9], [6, 2, 4, 7]]
    assert stats.icc_a1(ratings) == pytest.approx(0.29, abs=0.01)
    assert stats.icc_a1([[1, 1]]) is None
    assert stats.icc_band(0.8) == "хорошая"


def test_weighted_logistic_follows_heavier_observations():
    xs = [[1.0]] * 20 + [[-1.0]] * 20
    old = [1] * 20 + [0] * 20  # old regime: x predicts y positively
    flipped = [0] * 20 + [1] * 20  # new regime: the opposite
    ys = old + flipped
    weights = [0.05] * 40 + [1.0] * 40
    _, (coef,) = stats.logistic_regression(xs * 2, ys, l2=0.1, sample_weights=weights)
    assert coef < 0
    assert stats.recency_weight(60, 60) == 0.5 and stats.recency_weight(0, 60) == 1.0
