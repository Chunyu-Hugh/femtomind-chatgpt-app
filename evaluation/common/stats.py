"""The statistics the paper reports, with the standard library only.

Success counts come with two 95% intervals (Wilson, and the exact
Clopper-Pearson), and with pass@k (the chance that at least one of k
independent attempts succeeds) and pass^k (the chance that all k do), both
estimated from c successes in n runs. Means and medians of costs and times
get a seeded bootstrap interval. Nothing here needs numpy.
"""
import math
import random
from statistics import NormalDist, mean, median

Z95 = NormalDist().inv_cdf(0.975)


def wilson(c, n, z=Z95):
    """Wilson score interval for c successes in n trials: (lower, upper)."""
    if n <= 0:
        return (0.0, 1.0)
    p = c / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def _binom_cdf(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p), exactly (math.comb), for the sizes here."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    total = 0.0
    for i in range(0, k + 1):
        total += math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    return min(1.0, total)


def _solve(f, target, lo=0.0, hi=1.0, iterations=200):
    """p in [lo, hi] with f(p) == target, f decreasing in p (bisection)."""
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(c, n, alpha=0.05):
    """Exact (Clopper-Pearson) interval for c successes in n trials."""
    if n <= 0:
        return (0.0, 1.0)
    # Both binomial CDFs fall as p rises, which is what _solve assumes. Lower
    # bound: P(X >= c | p) = alpha/2, i.e. cdf(c-1; p) = 1 - alpha/2. Upper
    # bound: P(X <= c | p) = alpha/2.
    lower = 0.0 if c == 0 else _solve(lambda p: _binom_cdf(c - 1, n, p), 1 - alpha / 2, 0.0, 1.0)
    upper = 1.0 if c == n else _solve(lambda p: _binom_cdf(c, n, p), alpha / 2, 0.0, 1.0)
    return (lower, upper)


def pass_at_k(c, n, k):
    """pass@k = 1 - C(n-c, k)/C(n, k): the chance that k draws without
    replacement from the n runs contain a success (Chen et al. 2021)."""
    if k > n:
        raise ValueError("k must not exceed n")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_power_k(c, n, k):
    """pass^k = (c/n)^k with the Clopper-Pearson endpoints raised to k:
    the chance that k independent attempts all succeed (Yao et al. 2024)."""
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = c / n
    lo, hi = clopper_pearson(c, n)
    return (p ** k, lo ** k, hi ** k)


def bootstrap_ci(values, statistic=mean, resamples=2000, seed=0, alpha=0.05):
    """(statistic, lower, upper) by a seeded percentile bootstrap."""
    values = [float(v) for v in values if v is not None]
    if not values:
        return (None, None, None)
    rng = random.Random(seed)
    estimates = sorted(statistic([rng.choice(values) for _ in values]) for _ in range(resamples))
    lo = estimates[int(alpha / 2 * (resamples - 1))]
    hi = estimates[int((1 - alpha / 2) * (resamples - 1))]
    return (statistic(values), lo, hi)


def summarize_successes(c, n):
    """Everything the paper prints about c successes in n runs."""
    lo_w, hi_w = wilson(c, n)
    lo_cp, hi_cp = clopper_pearson(c, n)
    return {"successes": c, "runs": n, "rate": c / n if n else None,
            "wilson95": [lo_w, hi_w], "clopper_pearson95": [lo_cp, hi_cp],
            "pass_at_k": {str(k): pass_at_k(c, n, k) for k in range(1, n + 1)} if n else {},
            "pass_power_k": {str(k): pass_power_k(c, n, k)[0] for k in range(1, n + 1)} if n else {}}


def summarize_values(values, seed=0):
    """Mean and median with bootstrap intervals, for costs, times and turns."""
    values = [float(v) for v in values if v is not None]
    if not values:
        return {"n": 0}
    m, m_lo, m_hi = bootstrap_ci(values, mean, seed=seed)
    d, d_lo, d_hi = bootstrap_ci(values, median, seed=seed)
    return {"n": len(values), "mean": m, "mean95": [m_lo, m_hi], "median": d, "median95": [d_lo, d_hi],
            "min": min(values), "max": max(values), "values": values}
