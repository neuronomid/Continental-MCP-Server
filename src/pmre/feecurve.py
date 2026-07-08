"""Polymarket dynamic taker-fee curve (shared by snapshotter and analytics).

Per mcp_plan.md §6.1 the per-share taker fee is modelled as::

    fee_per_share ≈ rate · price · (1 − price)

which peaks at price = 0.5 and is zero at 0 and 1. With ``rate = 0.072`` the peak
is ``0.072 · 0.25 = 0.018`` / share ≈ $1.80 per 100 shares, matching the observed
peak taker cost on crypto Up/Down markets. ``FEE_MODEL_VERSION`` is stamped
alongside every number derived from this curve so old EV figures are never
silently reinterpreted when the schedule drifts.
"""

from __future__ import annotations

from . import FEE_MODEL_VERSION

__all__ = ["taker_fee_per_share", "taker_fee_for_notional", "FEE_MODEL_VERSION"]


def taker_fee_per_share(price: float, rate: float = 0.072) -> float:
    """Per-share taker fee at ``price`` (both symmetric around 0.5, ≥ 0)."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return rate * price * (1.0 - price)


def taker_fee_for_notional(price: float, shares: float, rate: float = 0.072) -> float:
    """Total taker fee for ``shares`` bought at ``price``."""
    return taker_fee_per_share(price, rate) * max(shares, 0.0)
