"""Snapshotter: fires at t_270…t_30 and builds snapshot rows from live books."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from .. import COLLECTOR_VERSION, FEATURE_VERSION, NET_EV_INPUTS_VERSION
from ..feecurve import taker_fee_per_share
from ..logging_setup import get_logger
from .orderbook import OrderBook
from .slugs import parse_slug_start, start_dt

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


# --- live run-loop ---------------------------------------------------------
class ClobBookClient:
    """CLOB REST ``GET /book`` — one call per token, materialised into an OrderBook.

    A separate snapshotter process cannot see the ``clob_ws`` collector's live
    in-memory books, so it pulls a point-in-time REST snapshot at each fire time.
    The response shape (``bids``/``asks`` of ``{price,size}`` + ``tick_size`` +
    ``last_trade_price``) is exactly what :meth:`OrderBook.apply_book` consumes.
    """

    def __init__(self, base_url: str, user_agent: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": user_agent}
        self.timeout = timeout

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=0.3, max=5))
    async def fetch_book(self, token_id: str, outcome: str | None = None) -> OrderBook:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as c:
            resp = await c.get(f"{self.base_url}/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
        book = OrderBook(
            token_id=str(token_id), outcome=outcome, tick_size=float(data.get("tick_size") or 0.001)
        )
        book.apply_book(data)
        ltp = data.get("last_trade_price")
        if ltp not in (None, ""):
            try:
                book.last_trade_price = float(ltp)
            except (TypeError, ValueError):
                pass
        return book


@dataclass
class _SnapMarket:
    """Everything the snapshot loop needs about one market (no live ORM object)."""

    id: int
    slug: str
    resolution: dt.datetime
    window_start: dt.datetime | None
    up_token: str
    down_token: str
    price_to_beat: float | None
    tick_size: float | None


class SnapshotCollector:
    """Live snapshotter: tracks each upcoming market and captures t_270…t_30 rows.

    Polls for markets whose window is open/upcoming and, per market, drives the
    tested :class:`SnapshotScheduler`. At each fire it pulls both order books via
    REST, reconstructs BTC/fair-value state from ``btc_ticks``, stamps the session,
    and persists a full snapshot (+ its top-10 levels).
    """

    def __init__(self, session_factory, settings, health=None, book_client: ClobBookClient | None = None):
        self.session_factory = session_factory
        self.settings = settings
        self.health = health
        self.book_client = book_client or ClobBookClient(settings.clob_base_url, settings.user_agent)
        self.scheduler = SnapshotScheduler(settings.snapshot_offsets_s)
        self.builder = SnapshotBuilder()
        self.fee_rate = settings.default_fee_rate
        self.poll_s = 5.0
        self.max_offset = max(settings.snapshot_offsets_s)
        self.log = get_logger("collectors.snapshotter")
        self._tracked: set[int] = set()

    # -- market selection --------------------------------------------------
    def _resolution_time(self, slug: str, fallback: dt.datetime | None) -> dt.datetime | None:
        try:
            return start_dt(parse_slug_start(slug) + 300)
        except ValueError:
            return fallback

    def _window_start(self, slug: str, fallback: dt.datetime | None) -> dt.datetime | None:
        try:
            return start_dt(parse_slug_start(slug))
        except ValueError:
            return fallback

    def due_markets(self, now: dt.datetime) -> list[_SnapMarket]:
        from ..db.models import Market, MarketResolution, MarketToken

        due: list[_SnapMarket] = []
        with self.session_factory() as s:
            resolved = {r.market_id for r in s.execute(select(MarketResolution)).scalars()}
            for m in s.execute(select(Market).where(Market.closed.is_(False))).scalars():
                if m.id in resolved:
                    continue
                res = self._resolution_time(m.slug, m.expected_resolution_time_utc)
                if res is None or res <= now:
                    continue  # already past this window's close
                earliest = res - dt.timedelta(seconds=self.max_offset)
                # Track once the first snapshot is within one poll of firing.
                if earliest > now + dt.timedelta(seconds=self.poll_s):
                    continue
                tokens = {
                    t.outcome: t.token_id
                    for t in s.execute(
                        select(MarketToken).where(MarketToken.market_id == m.id)
                    ).scalars()
                }
                if "UP" not in tokens or "DOWN" not in tokens:
                    continue
                due.append(
                    _SnapMarket(
                        id=m.id,
                        slug=m.slug,
                        resolution=res,
                        window_start=self._window_start(m.slug, m.slug_derived_start_utc),
                        up_token=tokens["UP"],
                        down_token=tokens["DOWN"],
                        price_to_beat=m.price_to_beat,
                        tick_size=m.tick_size,
                    )
                )
        return due

    # -- capture -----------------------------------------------------------
    def _persist_price_to_beat(self, market_id: int, ptb: float) -> None:
        from ..db.models import Market

        with self.session_factory() as s:
            m = s.get(Market, market_id)
            if m is not None and m.price_to_beat is None:
                m.price_to_beat = ptb
                m.price_to_beat_source = "btc_proxy_open"
                s.commit()

    async def capture(self, market: _SnapMarket, target: SnapshotTarget, fire_time: dt.datetime,
                      actual_seconds_left: float) -> int | None:
        from pm_sessions import label_instant

        from ..features.btc_history import btc_price_at, build_btc_feature_state

        try:
            up_book = await self.book_client.fetch_book(market.up_token, "UP")
            down_book = await self.book_client.fetch_book(market.down_token, "DOWN")
        except Exception as exc:  # pragma: no cover - live network
            if self.health:
                self.health.warning(
                    "snapshotter", f"book fetch failed m{market.id} {target.label}", {"error": str(exc)}
                )
            return None

        # price-to-beat = platform value, else the BTC proxy price at the window open.
        ptb = market.price_to_beat
        if ptb is None and market.window_start is not None:
            ptb = btc_price_at(self.session_factory, market.window_start)
            if ptb is not None:
                self._persist_price_to_beat(market.id, ptb)
                market.price_to_beat = ptb

        fs, secondary = build_btc_feature_state(self.session_factory, fire_time)
        btc = fs.build_btc_state(ptb, actual_seconds_left, secondary)
        session_label = label_instant(fire_time)
        meta = MarketMeta(
            market_id=market.id,
            price_to_beat=ptb,
            fee_rate=self.fee_rate,
            up_tick_size=market.tick_size,
        )
        built = self.builder.build(
            meta, up_book, down_book, target.label, target.offset, fire_time,
            actual_seconds_left, session_label=session_label, btc=btc,
        )
        return persist_snapshot(self.session_factory, built)

    async def _snapshot_market(self, market: _SnapMarket) -> None:
        async def on_fire(target, fire_time, actual):
            await self.capture(market, target, fire_time, actual)

        try:
            await self.scheduler.run(market.resolution, on_fire)
            self.log.info("snapshot_market_done", market_id=market.id, slug=market.slug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            if self.health:
                self.health.warning("snapshotter", f"snapshot market {market.id} failed", {"error": str(exc)})
        finally:
            self._tracked.discard(market.id)

    async def run(self, stop: asyncio.Event) -> None:
        tasks: set[asyncio.Task] = set()
        try:
            while not stop.is_set():
                for market in self.due_markets(dt.datetime.now(dt.UTC)):
                    if market.id in self._tracked:
                        continue
                    self._tracked.add(market.id)
                    task = asyncio.create_task(self._snapshot_market(market))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
                except TimeoutError:
                    pass
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
