"""Snapshotter: fires at t_270…t_30 and builds snapshot rows from live books."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

from .. import COLLECTOR_VERSION, FEATURE_VERSION, NET_EV_INPUTS_VERSION
from ..feecurve import taker_fee_per_share
from .orderbook import OrderBook

DEFAULT_OFFSETS = (270, 240, 210, 180, 150, 120, 90, 60, 30)
DEFAULT_NOTIONALS = (1, 2, 5, 10)


def label_for_offset(offset: int) -> str:
    return f"t_{offset}"


@dataclass
class SnapshotTarget:
    label: str
    offset: int
    target_time: dt.datetime


class SnapshotScheduler:
    """Computes fire times and drives firing on a monotonic clock.

    The firing loop uses injectable ``now_fn``/``sleep_fn`` so tests can advance a
    virtual clock deterministically and assert both firing tolerance and the
    truthfully-recorded ``snapshot_actual_seconds_left``.
    """

    def __init__(self, offsets: tuple[int, ...] = DEFAULT_OFFSETS):
        self.offsets = tuple(sorted(offsets, reverse=True))

    def targets(self, resolution: dt.datetime) -> list[SnapshotTarget]:
        return [
            SnapshotTarget(label_for_offset(o), o, resolution - dt.timedelta(seconds=o))
            for o in self.offsets
        ]

    @staticmethod
    def actual_seconds_left(fire_time: dt.datetime, resolution: dt.datetime) -> float:
        return (resolution - fire_time).total_seconds()

    async def run(
        self,
        resolution: dt.datetime,
        on_fire,
        now_fn=lambda: dt.datetime.now(dt.UTC),
        sleep_fn=asyncio.sleep,
        poll_s: float = 0.25,
    ) -> None:
        for target in self.targets(resolution):
            while True:
                now = now_fn()
                delta = (target.target_time - now).total_seconds()
                if delta <= 0:
                    break
                await sleep_fn(min(delta, poll_s))
            fire_time = now_fn()
            actual = self.actual_seconds_left(fire_time, resolution)
            await on_fire(target, fire_time, actual)


@dataclass
class MarketMeta:
    market_id: int
    price_to_beat: float | None = None
    fee_rate: float = 0.072
    up_tick_size: float | None = 0.001


@dataclass
class BtcState:
    price: float | None = None
    sigma_1s: float | None = None
    z_score: float | None = None
    p_fair: float | None = None
    ret_5s: float | None = None
    ret_30s: float | None = None
    ret_60s: float | None = None
    divergence_bps: float | None = None
    distance_from_start: float | None = None
    distance_bps: float | None = None
    quality: str = "ok"


@dataclass
class SnapshotFields:
    fields: dict = field(default_factory=dict)
    up_levels: list = field(default_factory=list)
    down_levels: list = field(default_factory=list)


class SnapshotBuilder:
    """Pure computation of all snapshot fields from two books + BTC + session."""

    def __init__(self, notionals: tuple[int, ...] = DEFAULT_NOTIONALS, stale_s: float = 10.0):
        self.notionals = notionals
        self.stale_s = stale_s

    def build(
        self,
        market: MarketMeta,
        up_book: OrderBook,
        down_book: OrderBook,
        label: str,
        offset: int,
        captured_at: dt.datetime,
        actual_seconds_left: float,
        session_label=None,
        btc: BtcState | None = None,
    ) -> SnapshotFields:
        up_bid, up_ask = up_book.best_bid(), up_book.best_ask()
        dn_bid, dn_ask = down_book.best_bid(), down_book.best_ask()
        up_mid, dn_mid = up_book.mid(), down_book.mid()

        market_spread_proxy = None
        if up_ask is not None and dn_ask is not None:
            market_spread_proxy = up_ask + dn_ask - 1.0

        # Dominant side = higher mid.
        dominant_side = None
        dominant_book = up_book
        dominant_mid = up_mid
        dominant_ask = up_ask
        if up_mid is not None and dn_mid is not None:
            if dn_mid > up_mid:
                dominant_side, dominant_book, dominant_mid, dominant_ask = (
                    "DOWN",
                    down_book,
                    dn_mid,
                    dn_ask,
                )
            else:
                dominant_side = "UP"
        elif up_mid is not None:
            dominant_side = "UP"
        elif dn_mid is not None:
            dominant_side, dominant_book, dominant_mid, dominant_ask = (
                "DOWN",
                down_book,
                dn_mid,
                dn_ask,
            )

        f: dict = {
            "market_id": market.market_id,
            "label": label,
            "target_seconds_left": offset,
            "snapshot_actual_seconds_left": actual_seconds_left,
            "captured_at": captured_at,
            "up_best_bid": up_bid,
            "up_best_ask": up_ask,
            "down_best_bid": dn_bid,
            "down_best_ask": dn_ask,
            "up_mid": up_mid,
            "down_mid": dn_mid,
            "up_spread": up_book.spread(),
            "market_spread_proxy": market_spread_proxy,
            "dominant_side": dominant_side,
            "dominant_mid": dominant_mid,
            "dominant_ask": dominant_ask,
            "last_trade_price": dominant_book.last_trade_price,
            "up_tick_size": market.up_tick_size or up_book.tick_size,
            "net_ev_inputs_version": NET_EV_INPUTS_VERSION,
            "collector_version": COLLECTOR_VERSION,
            "feature_version": FEATURE_VERSION,
        }

        # Simulated execution on the dominant side.
        for n in self.notionals:
            vwap, _, _ = dominant_book.vwap_buy(float(n))
            f[f"vwap_buy_{n}"] = vwap
        f["slippage_10"] = dominant_book.slippage_buy(10.0)
        f["max_usd_buy_within_2c"] = dominant_book.usd_within(0.02)
        f["depth_up_2c"] = up_book.usd_within(0.02)
        f["depth_down_2c"] = down_book.usd_within(0.02)

        if dominant_ask is not None:
            f["taker_fee_est_dominant"] = taker_fee_per_share(dominant_ask, market.fee_rate)

        # Quality flags.
        f["crossed_book_flag"] = up_book.is_crossed() or down_book.is_crossed()
        f["stale_book_flag"] = up_book.is_stale(captured_at, self.stale_s) or down_book.is_stale(
            captured_at, self.stale_s
        ) or up_book.seq_gap or down_book.seq_gap
        # A sum of best asks far below 1 usually means a stale book, not free money.
        f["bad_sum_flag"] = market_spread_proxy is not None and market_spread_proxy < -0.10

        # BTC / fair value.
        if btc is not None:
            f.update(
                {
                    "btc_price": btc.price,
                    "btc_distance_from_start": btc.distance_from_start,
                    "btc_distance_bps": btc.distance_bps,
                    "sigma_1s": btc.sigma_1s,
                    "z_score": btc.z_score,
                    "p_fair": btc.p_fair,
                    "ret_5s": btc.ret_5s,
                    "ret_30s": btc.ret_30s,
                    "ret_60s": btc.ret_60s,
                    "btc_source_divergence_bps": btc.divergence_bps,
                    "feature_quality": btc.quality,
                    "divergence_flag": (
                        btc.divergence_bps is not None and abs(btc.divergence_bps) > 10.0
                    ),
                }
            )
            # model_edge = dominant-signed market mid − signed p_fair.
            if btc.p_fair is not None and dominant_mid is not None and dominant_side is not None:
                # p_fair is P(UP). Convert to the dominant side's probability.
                p_fair_dom = btc.p_fair if dominant_side == "UP" else 1.0 - btc.p_fair
                f["model_edge"] = dominant_mid - p_fair_dom
        else:
            f["feature_quality"] = "missing"

        # Session stamp.
        if session_label is not None:
            f.update(
                {
                    "session_primary": session_label.session_primary,
                    "session_overlap": session_label.session_overlap,
                    "session_integrity": session_label.session_integrity,
                    "session_model_version": session_label.session_model_version,
                }
            )

        up_levels = _levels(up_book, "UP")
        down_levels = _levels(down_book, "DOWN")
        return SnapshotFields(fields=f, up_levels=up_levels, down_levels=down_levels)


def _levels(book: OrderBook, outcome: str, n: int = 10) -> list[dict]:
    bids, asks = book.top_n(n)
    out = []
    for i, lvl in enumerate(bids):
        out.append(
            {"token_id": book.token_id, "outcome": outcome, "side": "bid", "level": i,
             "price": lvl.price, "size": lvl.size}
        )
    for i, lvl in enumerate(asks):
        out.append(
            {"token_id": book.token_id, "outcome": outcome, "side": "ask", "level": i,
             "price": lvl.price, "size": lvl.size}
        )
    return out


def persist_snapshot(session_factory, built: SnapshotFields) -> int:
    """Write a snapshot + its top-10 orderbook levels; returns snapshot id."""
    from ..db.models import OrderbookLevel, Snapshot

    with session_factory() as s:
        snap = Snapshot(**built.fields)
        s.add(snap)
        s.flush()
        for lvl in built.up_levels + built.down_levels:
            s.add(OrderbookLevel(snapshot_id=snap.id, **lvl))
        sid = snap.id
        s.commit()
        return sid
