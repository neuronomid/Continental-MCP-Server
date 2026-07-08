"""Phase 11 — live run-loops for the snapshotter and resolution collectors.

These exercise the loop logic (market selection, REST-book capture, BTC-state
reconstruction from ``btc_ticks``, and platform-truth resolution + back-labeling)
with faked network clients over a SQLite DB — no live sockets.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from pmre.collectors.orderbook import OrderBook
from pmre.collectors.resolution import ResolutionCollector
from pmre.collectors.slugs import parse_slug_start, start_dt
from pmre.collectors.snapshotter import SnapshotCollector, SnapshotTarget, _SnapMarket
from pmre.config import Settings
from pmre.db.models import (
    BtcTick,
    Market,
    MarketResolution,
    MarketToken,
    Snapshot,
)
from tests.helpers import load_json_fixture


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.UTC)


def _settings(**over):
    return Settings(env="dev", **over)


# --- fakes -----------------------------------------------------------------
class FakeBookClient:
    """Returns pre-canned books keyed by token id (mirrors ClobBookClient.fetch_book)."""

    def __init__(self, books: dict[str, dict]):
        self._books = books
        self.calls: list[str] = []

    async def fetch_book(self, token_id: str, outcome: str | None = None) -> OrderBook:
        self.calls.append(str(token_id))
        book = OrderBook(token_id=str(token_id), outcome=outcome)
        book.apply_book(self._books[str(token_id)])
        return book


class FakeGamma:
    def __init__(self, by_slug: dict[str, dict]):
        self._by_slug = by_slug
        self.closed_args: list[bool | None] = []

    async def get_market_by_slug(self, slug: str, closed: bool | None = None):
        self.closed_args.append(closed)
        return self._by_slug.get(slug)


def _seed_btc(session_factory, center: dt.datetime, price: float = 60000.0, n: int = 40):
    """Seed n one-second binance+coinbase ticks ending at ``center``."""
    with session_factory() as s:
        for i in range(n):
            ts = center - dt.timedelta(seconds=(n - 1 - i))
            s.add(BtcTick(ts=ts, source="binance_spot", open=price, high=price,
                          low=price, close=price + i, volume=1.0, trade_count=3))
            s.add(BtcTick(ts=ts, source="coinbase", open=price, high=price,
                          low=price, close=price + i - 2, volume=1.0, trade_count=2))
        s.commit()


# --- snapshotter -----------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshot_capture_persists_full_row_with_fair_value(db):
    slug = "btc-updown-5m-1783447200"
    res = start_dt(parse_slug_start(slug) + 300)
    fire = res - dt.timedelta(seconds=240)
    with db.session_factory() as s:
        m = Market(slug=slug, price_to_beat=60000.0, tick_size=0.01,
                   expected_resolution_time_utc=res)
        s.add(m)
        s.flush()
        s.add_all([
            MarketToken(market_id=m.id, token_id="UPTOK", outcome="UP", outcome_index=0),
            MarketToken(market_id=m.id, token_id="DNTOK", outcome="DOWN", outcome_index=1),
        ])
        mid = m.id
        s.commit()
    _seed_btc(db.session_factory, fire)

    books = {
        "UPTOK": {"bids": [{"price": "0.59", "size": "100"}], "asks": [{"price": "0.61", "size": "100"}]},
        "DNTOK": {"bids": [{"price": "0.39", "size": "100"}], "asks": [{"price": "0.41", "size": "100"}]},
    }
    collector = SnapshotCollector(db.session_factory, _settings(), book_client=FakeBookClient(books))
    market = _SnapMarket(id=mid, slug=slug, resolution=res, window_start=start_dt(parse_slug_start(slug)),
                         up_token="UPTOK", down_token="DNTOK", price_to_beat=60000.0, tick_size=0.01)
    target = SnapshotTarget(label="t_240", offset=240, target_time=fire)

    sid = await collector.capture(market, target, fire, 240.0)
    assert sid is not None
    with db.session() as s:
        snap = s.get(Snapshot, sid)
        assert snap.label == "t_240"
        assert snap.dominant_side == "UP"            # up mid 0.60 > down mid 0.40
        assert snap.up_mid == pytest.approx(0.60)
        assert snap.p_fair is not None               # ptb + btc history present
        assert snap.feature_quality == "ok"          # >= 10 samples
        assert snap.session_primary is not None
        levels = s.execute(select(Snapshot).where(Snapshot.id == sid)).scalar_one()
        assert levels is not None


def test_due_markets_selects_upcoming_only(db):
    now = utc(2026, 7, 8, 12, 0)
    collector = SnapshotCollector(db.session_factory, _settings())

    def slug_for_res(res_dt):
        return f"btc-updown-5m-{int((res_dt - dt.timedelta(seconds=300)).timestamp())}"

    with db.session_factory() as s:
        # (a) upcoming, first snapshot due now -> selected
        upcoming = Market(slug=slug_for_res(now + dt.timedelta(seconds=200)), closed=False)
        # (b) far future, first snapshot not due for a while -> excluded
        far = Market(slug=slug_for_res(now + dt.timedelta(hours=1)), closed=False)
        # (c) already past -> excluded
        past = Market(slug=slug_for_res(now - dt.timedelta(seconds=60)), closed=False)
        # (d) closed -> excluded
        closed = Market(slug=slug_for_res(now + dt.timedelta(seconds=200)) + "-x", closed=True)
        s.add_all([upcoming, far, past, closed])
        s.flush()
        for m in (upcoming, far, past, closed):
            s.add_all([
                MarketToken(market_id=m.id, token_id=f"U{m.id}", outcome="UP", outcome_index=0),
                MarketToken(market_id=m.id, token_id=f"D{m.id}", outcome="DOWN", outcome_index=1),
            ])
        up_id = upcoming.id
        s.commit()

    due_ids = {d.id for d in collector.due_markets(now)}
    assert up_id in due_ids
    assert len(due_ids) == 1


# --- resolution ------------------------------------------------------------
def _seed_market_with_snapshots(db, slug: str, res: dt.datetime):
    with db.session_factory() as s:
        m = Market(slug=slug, price_to_beat=108000.0, expected_resolution_time_utc=res)
        s.add(m)
        s.flush()
        s.add_all([
            MarketToken(market_id=m.id, token_id="U", outcome="UP", outcome_index=0),
            MarketToken(market_id=m.id, token_id="D", outcome="DOWN", outcome_index=1),
        ])
        s.add(Snapshot(market_id=m.id, label="t_240", target_seconds_left=240,
                       captured_at=res - dt.timedelta(seconds=240),
                       up_mid=0.62, down_mid=0.40, up_best_ask=0.63, down_best_ask=0.41,
                       dominant_side="UP", last_trade_price=0.62))
        s.add(Snapshot(market_id=m.id, label="t_30", target_seconds_left=30,
                       captured_at=res - dt.timedelta(seconds=30),
                       up_mid=0.35, down_mid=0.66, up_best_ask=0.37, down_best_ask=0.67,
                       dominant_side="DOWN", last_trade_price=0.66))
        mid = m.id
        s.commit()
    return mid


@pytest.mark.asyncio
async def test_resolution_collector_resolves_and_backlabels(db):
    raw = load_json_fixture("raw/gamma_resolved_up.json")
    slug = raw["slug"]
    res = start_dt(parse_slug_start(slug) + 300)
    mid = _seed_market_with_snapshots(db, slug, res)
    _seed_btc(db.session_factory, res, price=108000.0)
    now = res + dt.timedelta(minutes=10)

    gamma = FakeGamma({slug: raw})
    collector = ResolutionCollector(db.session_factory, _settings(), gamma=gamma)
    n = await collector.resolve_due_once(now=now)
    assert n == 1
    assert gamma.closed_args == [True]  # resolution must request the closed market

    with db.session() as s:
        r = s.execute(select(MarketResolution).where(MarketResolution.market_id == mid)).scalar_one()
        assert r.winning_outcome == "UP"
        up = s.execute(select(MarketToken).where(
            MarketToken.market_id == mid, MarketToken.outcome == "UP")).scalar_one()
        assert up.is_winner is True
        snaps = {sn.label: sn for sn in s.execute(
            select(Snapshot).where(Snapshot.market_id == mid)).scalars()}
        assert snaps["t_240"].was_correct_mid is True     # UP-favored, UP won
        assert snaps["t_30"].was_correct_mid is False     # DOWN-favored, UP won
        m = s.get(Market, mid)
        assert m.closed is True and m.active is False


@pytest.mark.asyncio
async def test_resolution_collector_skips_unresolved_and_not_due(db):
    raw_open = {"slug": "btc-updown-5m-1783447200", "closed": False,
                "outcomes": '["Up", "Down"]', "outcomePrices": '["0.5", "0.5"]'}
    slug = raw_open["slug"]
    res = start_dt(parse_slug_start(slug) + 300)
    _seed_market_with_snapshots(db, slug, res)
    collector = ResolutionCollector(db.session_factory, _settings(), gamma=FakeGamma({slug: raw_open}))

    # Not yet past grace -> not even fetched.
    assert await collector.resolve_due_once(now=res - dt.timedelta(minutes=1)) == 0
    # Past grace but market not resolved on platform -> skipped.
    assert await collector.resolve_due_once(now=res + dt.timedelta(minutes=10)) == 0
    with db.session() as s:
        assert s.execute(select(MarketResolution)).first() is None
