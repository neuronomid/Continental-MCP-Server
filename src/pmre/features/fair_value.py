"""Fair-value model — an *independent* structural benchmark (mcp_plan.md §1.6).

For a ~driftless diffusion the fair UP probability at time t is::

    p_fair = Φ(z),   z = ln(S / S_ptb) / (σ_1s · √τ)

where ``S`` is the current oracle-proxy price, ``S_ptb`` the price-to-beat,
``σ_1s`` the short-horizon (EWMA of 1s returns) volatility and ``τ`` the seconds
remaining. σ is floored (BTC can go dead-quiet → σ≈0 makes z explode) and τ is
clamped so the model degrades gracefully rather than producing NaN/inf.
"""

from __future__ import annotations

import math

from scipy.stats import norm

SIGMA_FLOOR = 1e-6
TAU_FLOOR = 1e-3


def compute_z(
    price: float,
    price_to_beat: float,
    sigma_1s: float,
    tau_s: float,
    sigma_floor: float = SIGMA_FLOOR,
    tau_floor: float = TAU_FLOOR,
) -> float:
    if price <= 0 or price_to_beat <= 0:
        raise ValueError("prices must be positive")
    sigma = max(sigma_1s, sigma_floor)
    tau = max(tau_s, tau_floor)
    denom = sigma * math.sqrt(tau)
    return math.log(price / price_to_beat) / denom


def p_fair_from_z(z: float) -> float:
    """Φ(z), naturally clamped to (0, 1) — large |z| → 0/1."""
    return float(norm.cdf(z))


def p_fair(
    price: float,
    price_to_beat: float,
    sigma_1s: float,
    tau_s: float,
    sigma_floor: float = SIGMA_FLOOR,
    tau_floor: float = TAU_FLOOR,
) -> float:
    """Fair UP probability. τ ≤ 0 collapses to the deterministic outcome.

    At τ→0 the diffusion has no time to move: UP is certain if S>S_ptb, DOWN if
    S<S_ptb, and exactly 0.5 at S==S_ptb (the structural tie-UP bias is handled
    separately at resolution, not baked into the fair model).
    """
    if tau_s <= 0:
        if price > price_to_beat:
            return 1.0
        if price < price_to_beat:
            return 0.0
        return 0.5
    z = compute_z(price, price_to_beat, sigma_1s, tau_s, sigma_floor, tau_floor)
    return p_fair_from_z(z)


def model_edge(market_mid_up: float, price: float, price_to_beat: float, sigma_1s: float, tau_s: float) -> float:
    """market UP mid − fair UP probability (positive → market rich on UP)."""
    return market_mid_up - p_fair(price, price_to_beat, sigma_1s, tau_s)
