"""Net expected-value math for taker and maker entry styles.

Buying a $1-payout share at price ``p`` has gross EV = ``win_rate − p`` (win →
+(1−p), lose → −p). Taker subtracts the dynamic fee; maker pays zero fee but
only realises EV when the passive order fills (``p_fill``).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..feecurve import taker_fee_per_share
from .stats import wilson_lower


@dataclass
class EVResult:
    net_ev_taker: float | None
    net_ev_maker: float | None
    net_ev_ci_lower_95: float | None
    gross_ev: float | None


def gross_ev(win_rate: float, entry_price: float) -> float:
    return win_rate - entry_price


def net_ev_taker(win_rate: float, entry_price: float, fee_rate: float = 0.072) -> float:
    return gross_ev(win_rate, entry_price) - taker_fee_per_share(entry_price, fee_rate)


def net_ev_maker(
    win_rate: float, limit_price: float, p_fill: float, rebate_per_share: float = 0.0
) -> float:
    # Unfilled → 0 EV (opportunity cost noted, not charged).
    return p_fill * (gross_ev(win_rate, limit_price) + rebate_per_share)


def compute_ev(
    wins: int,
    n: int,
    entry_price_taker: float,
    entry_price_maker: float | None = None,
    p_fill: float = 1.0,
    fee_rate: float = 0.072,
    rebate_per_share: float = 0.0,
    alpha: float = 0.05,
) -> EVResult:
    if n <= 0:
        return EVResult(None, None, None, None)
    win_rate = wins / n
    taker = net_ev_taker(win_rate, entry_price_taker, fee_rate)
    maker = None
    if entry_price_maker is not None:
        maker = net_ev_maker(win_rate, entry_price_maker, p_fill, rebate_per_share)
    # CI-lower net EV (taker): use Wilson lower bound on win rate, worst-case.
    wr_lo = wilson_lower(wins, n, alpha)
    ci_lower = net_ev_taker(wr_lo, entry_price_taker, fee_rate)
    return EVResult(
        net_ev_taker=taker,
        net_ev_maker=maker,
        net_ev_ci_lower_95=ci_lower,
        gross_ev=gross_ev(win_rate, entry_price_taker),
    )
