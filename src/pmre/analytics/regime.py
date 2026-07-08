"""Rules-based (quantile) regime labeler over σ_1s — versioned.

Starts rules-based per mcp_plan.md §6.2; the k-means upgrade is a later, still
``regime_model_version``-stamped, change so analyses stay comparable within a
version.
"""

from __future__ import annotations

import numpy as np

from .. import REGIME_MODEL_VERSION


class RegimeLabeler:
    LABELS = ("calm", "normal", "volatile")

    def __init__(self, low: float | None = None, high: float | None = None,
                 version: str = REGIME_MODEL_VERSION):
        self.low = low
        self.high = high
        self.version = version

    def fit(self, sigmas: list[float]) -> RegimeLabeler:
        vals = np.asarray([s for s in sigmas if s is not None and np.isfinite(s)], dtype=float)
        if vals.size >= 3:
            self.low = float(np.quantile(vals, 1 / 3))
            self.high = float(np.quantile(vals, 2 / 3))
        return self

    def label(self, sigma: float | None) -> str:
        if sigma is None or self.low is None or self.high is None:
            return "normal"
        if sigma <= self.low:
            return "calm"
        if sigma >= self.high:
            return "volatile"
        return "normal"
