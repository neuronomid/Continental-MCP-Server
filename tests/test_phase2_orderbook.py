"""Phase 2 — book engine replay, integrity flags, VWAP/slippage properties."""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from pmre.collectors.orderbook import BookManager, OrderBook
from tests.helpers import load_json_fixture


def _book_from_levels(bids, asks, tick=0.001):
    b = OrderBook(token_id="t", tick_size=tick)
    b.apply_book({"bids": bids, "asks": asks, "timestamp": 1783447000000})
    return b


# --- replay golden --------------------------------------------------------
def test_replay_sequence_matches_expected_rest_book():
    fx = load_json_fixture("raw/clob_ws_book_sequence.json")
    book = OrderBook(token_id="tokUP")
    for ev in fx["events"]:
        book.handle(ev)
    exp = fx["expected_rest_book"]
    exp_book = OrderBook(token_id="tokUP")
    exp_book.apply_book(exp)
    assert book.bids == exp_book.bids
    assert book.asks == exp_book.asks
    assert book.last_trade_price == fx["expected_last_trade_price"]
    # best levels
    assert book.best_bid() == 0.58
    assert book.best_ask() == 0.59


def test_manager_dispatches_by_asset_id():
    mgr = BookManager()
    mgr.handle_message({"event_type": "book", "asset_id": "A", "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]})
    mgr.handle_message({"event_type": "book", "asset_id": "B", "bids": [{"price": "0.3", "size": "5"}], "asks": [{"price": "0.7", "size": "5"}]})
    assert set(mgr.books) == {"A", "B"}
    assert mgr.books["A"].best_ask() == 0.6


# --- integrity ------------------------------------------------------------
def test_crossed_book_flag():
    b = _book_from_levels([{"price": "0.62", "size": "10"}], [{"price": "0.60", "size": "10"}])
    assert b.is_crossed() is True


def test_negative_size_detected():
    b = OrderBook(token_id="t")
    b.bids = {0.5: -3.0}
    assert b.has_negative_size() is True


def test_stale_book_flag():
    b = _book_from_levels([{"price": "0.5", "size": "10"}], [{"price": "0.6", "size": "10"}])
    now = dt.datetime(2026, 7, 7, 18, 0, 30, tzinfo=dt.UTC)
    assert b.is_stale(now, max_age_s=10.0) is True  # book ts is 1783447000 ~ far earlier
    fresh = b.last_update_ts + dt.timedelta(seconds=5)
    assert b.is_stale(fresh, max_age_s=10.0) is False


def test_zero_size_removes_level():
    b = _book_from_levels([{"price": "0.5", "size": "10"}], [{"price": "0.6", "size": "10"}, {"price": "0.61", "size": "5"}])
    b.apply_price_change({"changes": [{"price": "0.6", "side": "SELL", "size": "0"}]})
    assert 0.6 not in b.asks
    assert b.best_ask() == 0.61


# --- VWAP / slippage property tests --------------------------------------
_prices = st.lists(
    st.tuples(
        st.floats(min_value=0.5, max_value=0.95),
        st.floats(min_value=1.0, max_value=1000.0),
    ),
    min_size=1,
    max_size=8,
)


@given(_prices, st.floats(min_value=1.0, max_value=50.0), st.floats(min_value=1.0, max_value=50.0))
def test_vwap_monotone_and_slippage_nonneg(levels, n1, n2):
    # Build asks from distinct prices.
    asks = {}
    for p, s in levels:
        asks[round(p, 4)] = asks.get(round(p, 4), 0.0) + s
    b = OrderBook(token_id="t")
    b.asks = asks
    lo, hi = sorted((n1, n2))
    v_lo, sh_lo, _ = b.vwap_buy(lo)
    v_hi, sh_hi, _ = b.vwap_buy(hi)
    ba = b.best_ask()
    if v_lo is not None and v_hi is not None:
        # VWAP monotone non-decreasing in notional (within float tolerance).
        assert v_hi >= v_lo - 1e-9
        # VWAP >= best ask, slippage >= 0.
        assert v_lo >= ba - 1e-9
        assert b.slippage_buy(lo) >= -1e-9


@given(_prices)
def test_shares_increase_with_notional(levels):
    asks = {}
    for p, s in levels:
        asks[round(p, 4)] = asks.get(round(p, 4), 0.0) + s
    b = OrderBook(token_id="t")
    b.asks = asks
    _, sh_small, _ = b.vwap_buy(1.0)
    _, sh_big, _ = b.vwap_buy(1000000.0)
    assert sh_big >= sh_small - 1e-9


def test_usd_within_2c_respects_cap():
    b = _book_from_levels(
        [{"price": "0.5", "size": "10"}],
        [{"price": "0.60", "size": "100"}, {"price": "0.61", "size": "50"}, {"price": "0.63", "size": "200"}],
    )
    # within 2c of 0.60 → include 0.60 and 0.61, exclude 0.63
    usd = b.usd_within(0.02)
    assert abs(usd - (0.60 * 100 + 0.61 * 50)) < 1e-9
