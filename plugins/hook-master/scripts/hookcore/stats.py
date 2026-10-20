"""Small, dependency-free statistics toolkit for the evaluator's science layer.

Everything the methodology (docs/METHODOLOGY.md) promises is computed here:
rank correlation, nonparametric group comparisons with effect sizes,
bootstrap intervals, multiple-testing control, AUC, penalized logistic
regression and inter-rater reliability.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Sequence


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def median(xs: Sequence[float]) -> float:
    return quantile(xs, 0.5)


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (type 7, the R/NumPy default)."""
    s = sorted(xs)
    if not s:
        raise ValueError("empty sample")
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def percentile_rank(xs: Sequence[float], x: float) -> float:
    """Share of the sample strictly below ``x`` plus half the ties, in percent."""
    below = sum(1 for v in xs if v < x)
    ties = sum(1 for v in xs if v == x)
    return 100.0 * (below + 0.5 * ties) / len(xs)


def rankdata(xs: Sequence[float]) -> list[float]:
    """Ranks starting at 1, ties get the average rank."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) < 3:
        return None
    mx, my = mean(x), mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    return pearson(rankdata(x), rankdata(y))


def normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def mann_whitney(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Two-sided Mann–Whitney U test (normal approximation with tie correction).

    Returns (U for sample ``a``, p-value).
    """
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        raise ValueError("empty sample")
    ranks = rankdata(list(a) + list(b))
    u1 = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    n = n1 + n2
    tie_counts: dict[float, int] = {}
    for r in ranks:
        tie_counts[r] = tie_counts.get(r, 0) + 1
    tie_term = sum(t**3 - t for t in tie_counts.values()) / (n * (n - 1))
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - tie_term))
    if sigma == 0:
        return u1, 1.0
    z = (u1 - n1 * n2 / 2) / sigma
    return u1, min(1.0, 2 * (1 - normal_cdf(abs(z))))


def cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float:
    """P(a > b) − P(a < b): effect size in [-1, 1] (Cliff, 1993)."""
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return (gt - lt) / (len(a) * len(b))


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """Area under the ROC curve = P(score of a positive > score of a negative)."""
    return (cliffs_delta(positives, negatives) + 1) / 2


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided binomial sign test (ties excluded)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def benjamini_hochberg(pvalues: Sequence[float]) -> list[float]:
    """Benjamini–Hochberg adjusted p-values (q-values), same order as input."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def bootstrap_ci(
    data: Sequence,
    statistic: Callable[[Sequence], float],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[float, float]:
    """Percentile bootstrap interval for ``statistic(data)``. Deterministic by seed."""
    rng = random.Random(seed)
    n = len(data)
    values = []
    for _ in range(n_boot):
        sample = [data[rng.randrange(n)] for _ in range(n)]
        try:
            value = statistic(sample)
        except (ValueError, ZeroDivisionError, TypeError):
            continue
        if value is not None:
            values.append(value)
    if not values:
        return (math.nan, math.nan)
    return quantile(values, alpha / 2), quantile(values, 1 - alpha / 2)


def bootstrap_diff_ci(
    a: Sequence[float], b: Sequence[float], *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 7
) -> tuple[float, float]:
    """Percentile bootstrap interval for mean(a) − mean(b)."""
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(mean(sa) - mean(sb))
    return quantile(diffs, alpha / 2), quantile(diffs, 1 - alpha / 2)


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve ``a @ x = b`` with Gaussian elimination and partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("Singular matrix")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            factor = m[r][col] / m[col][col]
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def logistic_regression(
    xs: Sequence[Sequence[float]],
    ys: Sequence[int],
    *,
    l2: float = 1.0,
    iterations: int = 50,
    sample_weights: Sequence[float] | None = None,
) -> tuple[float, list[float]]:
    """L2-penalized logistic regression fitted by Newton–Raphson (IRLS).

    The intercept is not penalized. ``sample_weights`` (e.g. recency
    weights) scale each observation's contribution. Returns (intercept, coefficients).
    """
    n, d = len(xs), len(xs[0])
    sw = list(sample_weights) if sample_weights is not None else [1.0] * n
    w = [0.0] * (d + 1)  # w[0] is the intercept
    rows = [[1.0, *x] for x in xs]
    for _ in range(iterations):
        grad = [0.0] * (d + 1)
        hess = [[0.0] * (d + 1) for _ in range(d + 1)]
        for row, y, weight in zip(rows, ys, sw):
            z = sum(wi * xi for wi, xi in zip(w, row))
            p = 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))
            for i in range(d + 1):
                grad[i] += weight * (y - p) * row[i]
                for j in range(i, d + 1):
                    hess[i][j] += weight * p * (1 - p) * row[i] * row[j]
        for i in range(1, d + 1):
            grad[i] -= l2 * w[i]
            hess[i][i] += l2
        for i in range(d + 1):
            for j in range(i):
                hess[i][j] = hess[j][i]
        hess[0][0] += 1e-9
        step = solve(hess, grad)
        w = [wi + si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return w[0], w[1:]


def icc_a1(ratings: Sequence[Sequence[float]]) -> float | None:
    """ICC(2,1): two-way random effects, absolute agreement, single rater
    (Shrout & Fleiss, 1979; McGraw & Wong, 1996).

    ``ratings[i][j]`` is rater j's score for target i; every target needs the
    same number of raters (k ≥ 2) and there must be at least 2 targets.
    """
    n = len(ratings)
    if n < 2:
        return None
    k = len(ratings[0])
    if k < 2 or any(len(r) != k for r in ratings):
        return None
    grand = sum(sum(r) for r in ratings) / (n * k)
    row_means = [sum(r) / k for r in ratings]
    col_means = [sum(ratings[i][j] for i in range(n)) / n for j in range(k)]
    ss_rows = k * sum((m - grand) ** 2 for m in row_means)
    ss_cols = n * sum((m - grand) ** 2 for m in col_means)
    ss_total = sum((x - grand) ** 2 for r in ratings for x in r)
    ss_err = ss_total - ss_rows - ss_cols
    msr = ss_rows / (n - 1)
    msc = ss_cols / (k - 1)
    mse = ss_err / ((n - 1) * (k - 1))
    denom = msr + (k - 1) * mse + k * (msc - mse) / n
    if denom <= 0:
        return None
    return (msr - mse) / denom


def icc_band(icc: float | None) -> str:
    """Reliability bands from Koo & Li (2016)."""
    if icc is None:
        return "нет данных"
    if icc < 0.5:
        return "плохая"
    if icc < 0.75:
        return "умеренная"
    if icc < 0.9:
        return "хорошая"
    return "отличная"


def recency_weight(age_days: float, half_life_days: float) -> float:
    """Exponential decay: an observation ``half_life_days`` old counts half."""
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(age_days, 0.0) / half_life_days)
