"""Replay raw archived WS events back into books (reproducibility guarantee).

Given the archived ``book``/``price_change`` stream for a token, rebuild the book
as it stood at any instant and re-derive snapshot fields — so a stored snapshot
can be verified bit-for-bit (within numeric tolerance) against a fresh rebuild
from raw (mcp_phases.md Phase 5).
"""

from __future__ import annotations

import datetime as dt

from .collectors.orderbook import OrderBook, _parse_ts


def rebuild_book_at(events: list[dict], at_ts: dt.datetime, token_id: str = "t") -> OrderBook:
    """Apply all events with timestamp ≤ ``at_ts`` and return the resulting book."""
    book = OrderBook(token_id=token_id)
    for ev in events:
        ts = ev.get("timestamp")
        if ts is not None and _parse_ts(ts) > at_ts:
            break
        book.handle(ev)
    return book


def diff_books(a: OrderBook, b: OrderBook, tol: float = 1e-9) -> dict:
    """Return per-field differences between two books (empty dict = identical)."""
    diffs: dict = {}

    def _cmp(name, x, y):
        if x is None and y is None:
            return
        if x is None or y is None or abs(x - y) > tol:
            diffs[name] = (x, y)

    _cmp("best_bid", a.best_bid(), b.best_bid())
    _cmp("best_ask", a.best_ask(), b.best_ask())
    if set(a.bids) != set(b.bids):
        diffs["bid_prices"] = (sorted(a.bids), sorted(b.bids))
    if set(a.asks) != set(b.asks):
        diffs["ask_prices"] = (sorted(a.asks), sorted(b.asks))
    return diffs
