"""Phase 2 — scheduler firing, snapshot building/persistence, reconnect, x-check."""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from pm_sessions import label_instant
from pm_sessions.calendars import WeekendOnlyProvider
from pmre.collectors.clob_ws import ClobWebSocketClient, compare_books
from pmre.collectors.orderbook import BookManager, OrderBook
from pmre.collectors.snapshotter import (
    BtcState,
    MarketMeta,
    SnapshotBuilder,
    SnapshotScheduler,
    persist_snapshot,
)
from pmre.db.models import OrderbookLevel, Snapshot


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.UTC)


# --- scheduler ------------------------------------------------------------
def test_scheduler_targets():
    sched = SnapshotScheduler(offsets=(270, 240, 30))
    res = utc(2026, 7, 7, 18, 5)
    targets = sched.targets(res)
    assert [t.label for t in targets] == ["t_270", "t_240", "t_30"]
    assert targets[0].target_time == utc(2026, 7, 7, 18, 0, 30)
    assert targets[-1].target_time == utc(2026, 7, 7, 18, 4, 30)


async def test_scheduler_fires_within_tolerance_virtual_clock():
    sched = SnapshotScheduler(offsets=(270, 240, 210))
    res = utc(2026, 7, 7, 18, 5)
    # Virtual clock: start well before first target.
    clock = {"now": utc(2026, 7, 7, 18, 0, 0, 0)}

    async def sleep_fn(secs):
        # advance virtual clock by requested sleep (+ tiny jitter under tolerance)
        clock["now"] = clock["now"] + dt.timedelta(seconds=secs + 0.01)

    fired = []

    async def on_fire(target, fire_time, actual):
        fired.append((target.label, fire_time, actual))

    await sched.run(
        res, on_fire, now_fn=lambda: clock["now"], sleep_fn=sleep_fn, poll_s=0.25
    )
    assert [f[0] for f in fired] == ["t_270", "t_240", "t_210"]
    for label, fire_time, actual in fired:
        target = res - dt.timedelta(seconds=int(label[2:]))
        # fired within 150 ms of target
        assert abs((fire_time - target).total_seconds()) < 0.15
        # actual seconds-left recorded truthfully
        assert abs(actual - (res - fire_time).total_seconds()) < 1e-6


# --- snapshot builder -----------------------------------------------------
def _mk_book(token, bids, asks, ts=1783447000000):
    b = OrderBook(token_id=token)
    b.apply_book({"bids": bids, "asks": asks, "timestamp": ts})
    return b


def test_snapshot_builder_fields_and_dominant_side():
    up = _mk_book("U", [{"price": "0.60", "size": "100"}], [{"price": "0.62", "size": "200"}, {"price": "0.63", "size": "300"}])
    dn = _mk_book("D", [{"price": "0.37", "size": "100"}], [{"price": "0.40", "size": "200"}])
    market = MarketMeta(market_id=1, price_to_beat=108000.0, fee_rate=0.072)
    builder = SnapshotBuilder()
    cap = utc(2026, 7, 7, 18, 1)
    # make books fresh at capture time
    up.last_update_ts = cap
    dn.last_update_ts = cap
    session = label_instant(cap, WeekendOnlyProvider())
    built = builder.build(market, up, dn, "t_240", 240, cap, 239.9, session_label=session)
    f = built.fields
    assert f["dominant_side"] == "UP"
    assert f["up_best_ask"] == 0.62
    assert f["down_best_ask"] == 0.40
    # market_spread_proxy = 0.62 + 0.40 - 1 = 0.02
    assert abs(f["market_spread_proxy"] - 0.02) < 1e-9
    # taker fee at 0.62 = 0.072 * 0.62 * 0.38
    assert abs(f["taker_fee_est_dominant"] - 0.072 * 0.62 * 0.38) < 1e-9
    assert f["vwap_buy_1"] == 0.62  # $1 fills at top level
    assert f["session_primary"] == session.session_primary
    assert f["crossed_book_flag"] is False
    assert f["stale_book_flag"] is False
    # top-of-book levels captured (1 bid + 2 asks)
    assert len(built.up_levels) == 3


def test_snapshot_builder_bad_sum_flag():
    up = _mk_book("U", [{"price": "0.30", "size": "100"}], [{"price": "0.35", "size": "200"}])
    dn = _mk_book("D", [{"price": "0.30", "size": "100"}], [{"price": "0.35", "size": "200"}])
    cap = utc(2026, 7, 7, 18, 1)
    up.last_update_ts = cap
    dn.last_update_ts = cap
    built = SnapshotBuilder().build(MarketMeta(1), up, dn, "t_240", 240, cap, 240.0)
    # sum of asks = 0.70 → proxy = -0.30 < -0.10 → bad_sum
    assert built.fields["bad_sum_flag"] is True


def test_snapshot_builder_model_edge_uses_dominant_signed_pfair():
    up = _mk_book("U", [{"price": "0.60", "size": "100"}], [{"price": "0.62", "size": "200"}])
    dn = _mk_book("D", [{"price": "0.37", "size": "100"}], [{"price": "0.40", "size": "200"}])
    cap = utc(2026, 7, 7, 18, 1)
    up.last_update_ts = cap
    dn.last_update_ts = cap
    btc = BtcState(price=108100, sigma_1s=0.0005, z_score=0.4, p_fair=0.60, quality="ok")
    built = SnapshotBuilder().build(MarketMeta(1), up, dn, "t_240", 240, cap, 240.0, btc=btc)
    # dominant UP, up_mid=0.61, p_fair_dom=0.60 → model_edge=0.01
    assert abs(built.fields["model_edge"] - 0.01) < 1e-9
    assert built.fields["feature_quality"] == "ok"


def test_persist_snapshot_writes_levels(db):
    from pmre.db.models import Market

    with db.session() as s:
        m = Market(slug="btc-updown-5m-1")
        s.add(m)
        s.flush()
        mid = m.id
    up = _mk_book("U", [{"price": "0.60", "size": "100"}], [{"price": "0.62", "size": "200"}])
    dn = _mk_book("D", [{"price": "0.37", "size": "100"}], [{"price": "0.40", "size": "200"}])
    cap = utc(2026, 7, 7, 18, 1)
    up.last_update_ts = cap
    dn.last_update_ts = cap
    built = SnapshotBuilder().build(MarketMeta(mid), up, dn, "t_240", 240, cap, 240.0)
    sid = persist_snapshot(db.session_factory, built)
    with db.session() as s:
        snap = s.execute(select(Snapshot).where(Snapshot.id == sid)).scalar_one()
        assert snap.label == "t_240"
        levels = s.execute(select(OrderbookLevel).where(OrderbookLevel.snapshot_id == sid)).scalars().all()
        assert len(levels) == 4


# --- reconnect chaos ------------------------------------------------------
def test_reconnect_flags_gap_then_recovers():
    mgr = BookManager()
    client = ClobWebSocketClient("wss://x", book_manager=mgr)
    client.subscribe(["tokUP"])
    client.dispatch({"event_type": "book", "asset_id": "tokUP", "bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}], "timestamp": 1783447000000})
    assert mgr.books["tokUP"].seq_gap is False
    # simulate reconnect → gap flagged
    client.on_reconnect()
    assert mgr.books["tokUP"].seq_gap is True
    assert client.reconnects == 1
    # fresh full book clears the gap
    client.dispatch({"event_type": "book", "asset_id": "tokUP", "bids": [{"price": "0.51", "size": "9"}], "asks": [{"price": "0.61", "size": "9"}], "timestamp": 1783447005000})
    assert mgr.books["tokUP"].seq_gap is False
    assert mgr.books["tokUP"].best_ask() == 0.61


async def test_ws_run_loop_with_fake_socket_recovers_from_drop():
    mgr = BookManager()
    client = ClobWebSocketClient("wss://x", book_manager=mgr)
    client.subscribe(["tokUP"])
    stop = asyncio.Event()

    class FakeWS:
        def __init__(self, messages, drop=False):
            self.messages = messages
            self.drop = drop
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, payload):
            self.sent.append(payload)

        async def __aiter__(self):
            for m in self.messages:
                yield m
            if self.drop:
                raise ConnectionError("socket dropped")

    calls = {"n": 0}

    def connect_factory(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeWS(['{"event_type":"book","asset_id":"tokUP","bids":[{"price":"0.5","size":"10"}],"asks":[{"price":"0.6","size":"10"}],"timestamp":1783447000000}'], drop=True)
        # second connection delivers a fresh book then we stop
        stop.set()
        return FakeWS(['{"event_type":"book","asset_id":"tokUP","bids":[{"price":"0.55","size":"8"}],"asks":[{"price":"0.65","size":"8"}],"timestamp":1783447009000}'])

    client.max_backoff_s = 0.01
    await asyncio.wait_for(client.run(connect_factory, stop_event=stop), timeout=5)
    assert client.reconnects >= 1
    assert mgr.books["tokUP"].best_ask() == 0.65  # recovered with fresh book


# --- REST cross-check -----------------------------------------------------
def test_compare_books_divergence():
    ws = OrderBook(token_id="t")
    ws.apply_book({"bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]})
    rest_same = {"bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]}
    rest_diff = {"bids": [{"price": "0.52", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]}
    assert compare_books(ws, rest_same)["divergent"] is False
    assert compare_books(ws, rest_diff)["divergent"] is True
