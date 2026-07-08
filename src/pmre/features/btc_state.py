"""Rolling BTC proxy state → distance/vol/momentum features + p_fair.

Fed 1-second samples (from the aggregated Binance/Coinbase feeds). Computes:
returns over 5/15/30/60 s, EWMA σ_1s, realized vol over 30/60/300 s, distance
from the **price-to-beat** (never Binance-at-open — mcp_plan.md §1.2), high/low
since window start, trend/reversal flags, and trade intensity. Insufficient
history degrades ``feature_quality`` rather than fabricating values.
"""

from __future__ import annotations

import math
from collections import deque

from ..collectors.snapshotter import BtcState
from .fair_value import compute_z
from .fair_value import p_fair as _p_fair


def divergence_bps(price_a: float | None, price_b: float | None) -> float | None:
    """|a − b| in basis points of their mean (proxy-feed divergence flag)."""
    if not price_a or not price_b:
        return None
    mean = (price_a + price_b) / 2.0
    if mean <= 0:
        return None
    return abs(price_a - price_b) / mean * 1e4


class BtcFeatureState:
    def __init__(
        self,
        ewma_lambda: float = 0.94,
        sigma_floor: float = 1e-6,
        history_s: int = 600,
        min_samples: int = 10,
    ):
        self.ewma_lambda = ewma_lambda
        self.sigma_floor = sigma_floor
        self.history_s = history_s
        self.min_samples = min_samples
        self.samples: deque[tuple[float, float]] = deque()  # (ts, price)
        self.ewma_var: float | None = None
        self._sq_returns: deque[tuple[float, float]] = deque()  # (ts, r^2)
        self.window_high: float | None = None
        self.window_low: float | None = None
        self.window_start_price: float | None = None
        self.trade_intensity: float = 0.0

    def reset_window(self, start_price: float | None = None) -> None:
        self.window_high = start_price
        self.window_low = start_price
        self.window_start_price = start_price

    def update(self, ts: float, price: float, trades: int = 0) -> None:
        if self.samples:
            _, prev = self.samples[-1]
            if prev > 0 and price > 0:
                r = math.log(price / prev)
                if self.ewma_var is None:
                    self.ewma_var = r * r
                else:
                    self.ewma_var = (
                        self.ewma_lambda * self.ewma_var + (1 - self.ewma_lambda) * r * r
                    )
                self._sq_returns.append((ts, r * r))
        self.samples.append((ts, price))
        self.trade_intensity = 0.9 * self.trade_intensity + 0.1 * trades

        if self.window_high is None or price > self.window_high:
            self.window_high = price
        if self.window_low is None or price < self.window_low:
            self.window_low = price

        # prune history
        cutoff = ts - self.history_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        while self._sq_returns and self._sq_returns[0][0] < cutoff:
            self._sq_returns.popleft()

    @property
    def last_price(self) -> float | None:
        return self.samples[-1][1] if self.samples else None

    @property
    def last_ts(self) -> float | None:
        return self.samples[-1][0] if self.samples else None

    def sigma_1s(self) -> float:
        if self.ewma_var is None:
            return self.sigma_floor
        return max(math.sqrt(self.ewma_var), self.sigma_floor)

    def ret(self, seconds: float) -> float | None:
        """Log return over the last ``seconds`` (None if history too short)."""
        if not self.samples:
            return None
        now_ts, now_px = self.samples[-1]
        target = now_ts - seconds
        # earliest sample at or before target
        ref = None
        for ts, px in self.samples:
            if ts <= target:
                ref = px
            else:
                break
        if ref is None or ref <= 0:
            return None
        return math.log(now_px / ref)

    def realized_vol(self, window_s: float) -> float | None:
        if not self._sq_returns:
            return None
        now_ts = self.samples[-1][0]
        cutoff = now_ts - window_s
        total = sum(sq for ts, sq in self._sq_returns if ts >= cutoff)
        return math.sqrt(total)

    def distance(self, price_to_beat: float) -> tuple[float | None, float | None]:
        px = self.last_price
        if px is None or not price_to_beat:
            return None, None
        return px - price_to_beat, (px / price_to_beat - 1.0) * 1e4

    def has_enough_history(self) -> bool:
        return len(self.samples) >= self.min_samples

    def build_btc_state(
        self,
        price_to_beat: float | None,
        tau_s: float,
        secondary_price: float | None = None,
    ) -> BtcState:
        px = self.last_price
        quality = "ok" if self.has_enough_history() else "degraded"
        if px is None:
            return BtcState(quality="missing")

        sigma = self.sigma_1s()
        dist_lvl, dist_bps = self.distance(price_to_beat) if price_to_beat else (None, None)
        z = None
        pf = None
        if price_to_beat:
            try:
                z = compute_z(px, price_to_beat, sigma, tau_s, sigma_floor=self.sigma_floor)
                pf = _p_fair(px, price_to_beat, sigma, tau_s, sigma_floor=self.sigma_floor)
            except ValueError:
                quality = "degraded"

        return BtcState(
            price=px,
            sigma_1s=sigma,
            z_score=z,
            p_fair=pf,
            ret_5s=self.ret(5),
            ret_30s=self.ret(30),
            ret_60s=self.ret(60),
            divergence_bps=divergence_bps(px, secondary_price),
            distance_from_start=dist_lvl,
            distance_bps=dist_bps,
            quality=quality,
        )
