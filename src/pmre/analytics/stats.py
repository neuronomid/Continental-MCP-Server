"""Statistical primitives: Wilson CI, BH-FDR, Brier/log-loss, calibration p-value.

Decisions are always made on the **CI lower bound**, never the point estimate
(mcp_plan.md §1.4). All bucket-level significance passes through Benjamini–Hochberg
FDR (q = 0.10) so hundreds of tested cells don't manufacture fake edges (§1.7).
"""

from __future__ import annotations

import math

import numpy as np
from scipy.stats import binomtest, norm
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.proportion import proportion_confint

EPS = 1e-12


def wilson_interval(wins: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (0.0, 1.0)
    lo, hi = proportion_confint(wins, n, alpha=alpha, method="wilson")
    return float(lo), float(hi)


def calibration_pvalue(wins: int, n: int, p0: float) -> float:
    """Two-sided binomial p-value that the win rate differs from ``p0``.

    Under the null (market is calibrated) the empirical win rate equals the
    market-implied probability ``p0``; a small p-value is evidence of
    miscalibration.
    """
    if n <= 0:
        return 1.0
    p0 = min(max(p0, EPS), 1 - EPS)
    return float(binomtest(wins, n, p0, alternative="two-sided").pvalue)


def benjamini_hochberg(pvalues: list[float], q: float = 0.10) -> list[bool]:
    """BH-FDR: return a boolean 'passes' mask at level ``q``."""
    if not pvalues:
        return []
    reject, _, _, _ = multipletests(pvalues, alpha=q, method="fdr_bh")
    return [bool(x) for x in reject]


def brier_score(forecasts: list[float], outcomes: list[int]) -> float | None:
    if not forecasts:
        return None
    f = np.asarray(forecasts, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((f - y) ** 2))


def log_loss(forecasts: list[float], outcomes: list[int]) -> float | None:
    if not forecasts:
        return None
    f = np.clip(np.asarray(forecasts, dtype=float), EPS, 1 - EPS)
    y = np.asarray(outcomes, dtype=float)
    return float(-np.mean(y * np.log(f) + (1 - y) * np.log(1 - f)))


def normal_cdf(x: float) -> float:
    return float(norm.cdf(x))


def wilson_lower(wins: int, n: int, alpha: float = 0.05) -> float:
    return wilson_interval(wins, n, alpha)[0]


def is_finite(x) -> bool:
    return x is not None and math.isfinite(x)
