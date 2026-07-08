"""Walk-forward evaluation: 14d train / 3d validate, rolling (mcp_plan.md §6.3).

A candidate must be positive (CI-lower net EV > 0) in ≥ 2 consecutive validation
windows. Train/validate market sets are disjoint by construction (time-partitioned)
— a guarantee the leakage test asserts directly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .ev import net_ev_taker
from .stats import wilson_lower


@dataclass
class DatedObs:
    market_id: int
    date: dt.date
    won: int
    entry_price: float
    fee_rate: float = 0.072


@dataclass
class Window:
    train_start: dt.date
    train_end: dt.date  # exclusive
    val_start: dt.date
    val_end: dt.date  # exclusive


@dataclass
class WindowResult:
    window: Window
    n_val: int
    ci_lower_net_ev: float | None
    positive: bool
    train_market_ids: set[int] = field(default_factory=set)
    val_market_ids: set[int] = field(default_factory=set)


@dataclass
class WalkForwardResult:
    passed: bool
    max_consecutive_positive: int
    windows: list[WindowResult]


def rolling_windows(
    start: dt.date, end: dt.date, train_days: int = 14, val_days: int = 3, step_days: int = 3
) -> list[Window]:
    windows: list[Window] = []
    ts = start
    while True:
        train_start = ts
        train_end = train_start + dt.timedelta(days=train_days)
        val_start = train_end
        val_end = val_start + dt.timedelta(days=val_days)
        if val_end > end + dt.timedelta(days=1):
            break
        windows.append(Window(train_start, train_end, val_start, val_end))
        ts = ts + dt.timedelta(days=step_days)
    return windows


class WalkForward:
    def __init__(self, train_days: int = 14, val_days: int = 3, step_days: int = 3,
                 min_consecutive: int = 2):
        self.train_days = train_days
        self.val_days = val_days
        self.step_days = step_days
        self.min_consecutive = min_consecutive

    def evaluate(self, obs: list[DatedObs]) -> WalkForwardResult:
        if not obs:
            return WalkForwardResult(False, 0, [])
        start = min(o.date for o in obs)
        end = max(o.date for o in obs)
        windows = rolling_windows(start, end, self.train_days, self.val_days, self.step_days)
        results: list[WindowResult] = []
        consecutive = 0
        best_consecutive = 0
        for w in windows:
            train = [o for o in obs if w.train_start <= o.date < w.train_end]
            val = [o for o in obs if w.val_start <= o.date < w.val_end]
            if not val:
                continue
            wins = sum(o.won for o in val)
            n = len(val)
            avg_entry = sum(o.entry_price for o in val) / n
            fee_rate = val[0].fee_rate
            wr_lo = wilson_lower(wins, n)
            ci_lower = net_ev_taker(wr_lo, avg_entry, fee_rate)
            positive = ci_lower > 0
            results.append(
                WindowResult(
                    window=w,
                    n_val=n,
                    ci_lower_net_ev=ci_lower,
                    positive=positive,
                    train_market_ids={o.market_id for o in train},
                    val_market_ids={o.market_id for o in val},
                )
            )
            if positive:
                consecutive += 1
                best_consecutive = max(best_consecutive, consecutive)
            else:
                consecutive = 0
        return WalkForwardResult(
            passed=best_consecutive >= self.min_consecutive,
            max_consecutive_positive=best_consecutive,
            windows=results,
        )
