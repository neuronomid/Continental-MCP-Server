"""In-memory order book maintained from the CLOB market-channel event stream.

Pure logic — no I/O — so recorded ``book``/``price_change`` sequences can be
replayed in tests and asserted bit-for-bit against a recorded REST book.

Event shapes are tolerant to the documented Polymarket variants:
    * ``book``            full snapshot: ``bids``/``asks`` (or ``buys``/``sells``)
    * ``price_change``    delta: ``changes`` list of ``{price, side, size}``
    * ``last_trade_price``trade print
    * ``tick_size_change``tick size update
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


def _f(x) -> float:
    return float(x)


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    outcome: str | None = None
    tick_size: float = 0.001
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_trade_price: float | None = None
    last_update_ts: dt.datetime | None = None
    seq_gap: bool = False

    # --- event application -------------------------------------------------
    def apply_book(self, event: dict) -> None:
        bids = event.get("bids", event.get("buys", []))
        asks = event.get("asks", event.get("sells", []))
        self.bids = {}
        self.asks = {}
        for lvl in bids:
            p, sz = _f(lvl["price"]), _f(lvl["size"])
            if sz > 0:
                self.bids[p] = sz
        for lvl in asks:
            p, sz = _f(lvl["price"]), _f(lvl["size"])
            if sz > 0:
                self.asks[p] = sz
        self._touch(event)
        self.seq_gap = False

    def apply_price_change(self, event: dict) -> None:
        changes = event.get("changes")
        if changes is None:
            # single-change variant
            changes = [event]
        for ch in changes:
            side = str(ch.get("side", ch.get("side_name", ""))).upper()
            price = _f(ch["price"])
            size = _f(ch["size"])
            book = self.bids if side in {"BUY", "BID", "B"} else self.asks
            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size
        self._touch(event)

    def apply_last_trade(self, event: dict) -> None:
        self.last_trade_price = _f(event["price"])
        self._touch(event)

    def apply_tick_size_change(self, event: dict) -> None:
        new = event.get("new_tick_size", event.get("tick_size"))
        if new is not None:
            self.tick_size = _f(new)
        self._touch(event)

    def handle(self, event: dict) -> None:
        et = event.get("event_type") or event.get("type")
        if et == "book":
            self.apply_book(event)
        elif et == "price_change":
            self.apply_price_change(event)
        elif et == "last_trade_price":
            self.apply_last_trade(event)
        elif et == "tick_size_change":
            self.apply_tick_size_change(event)

    def _touch(self, event: dict) -> None:
        ts = event.get("timestamp")
        if ts is not None:
            self.last_update_ts = _parse_ts(ts)
        else:
            self.last_update_ts = dt.datetime.now(dt.UTC)

    # --- reads -------------------------------------------------------------
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    def spread(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return ba - bb

    def asks_sorted(self) -> list[Level]:
        return [Level(p, self.asks[p]) for p in sorted(self.asks)]

    def bids_sorted(self) -> list[Level]:
        return [Level(p, self.bids[p]) for p in sorted(self.bids, reverse=True)]

    def top_n(self, n: int = 10) -> tuple[list[Level], list[Level]]:
        return self.bids_sorted()[:n], self.asks_sorted()[:n]

    # --- integrity ---------------------------------------------------------
    def is_crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb >= ba

    def has_negative_size(self) -> bool:
        return any(v < 0 for v in self.bids.values()) or any(
            v < 0 for v in self.asks.values()
        )

    def is_stale(self, now: dt.datetime, max_age_s: float = 10.0) -> bool:
        if self.last_update_ts is None:
            return True
        return (now - self.last_update_ts).total_seconds() > max_age_s

    # --- simulated execution ----------------------------------------------
    def vwap_buy(self, usd_notional: float) -> tuple[float | None, float, bool]:
        """Buy ``usd_notional`` dollars by lifting asks (price ascending).

        Returns ``(vwap, shares_filled, fully_filled)``. VWAP is monotone
        non-decreasing in notional and ≥ best ask; slippage = vwap − best_ask ≥ 0.
        """
        remaining = usd_notional
        cost = 0.0
        shares = 0.0
        for lvl in self.asks_sorted():
            level_cost = lvl.price * lvl.size
            if level_cost <= remaining:
                cost += level_cost
                shares += lvl.size
                remaining -= level_cost
            else:
                take_shares = remaining / lvl.price if lvl.price > 0 else 0.0
                cost += take_shares * lvl.price
                shares += take_shares
                remaining = 0.0
                break
        if shares <= 0:
            return None, 0.0, False
        return cost / shares, shares, remaining <= 1e-9

    def slippage_buy(self, usd_notional: float) -> float | None:
        vwap, shares, _ = self.vwap_buy(usd_notional)
        ba = self.best_ask()
        if vwap is None or ba is None:
            return None
        return max(0.0, vwap - ba)

    def usd_within(self, cents: float) -> float:
        """Total USD of ask liquidity within ``cents`` of best ask."""
        ba = self.best_ask()
        if ba is None:
            return 0.0
        limit = ba + cents
        return sum(lvl.price * lvl.size for lvl in self.asks_sorted() if lvl.price <= limit + 1e-12)


def _parse_ts(ts) -> dt.datetime:
    if isinstance(ts, dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)
    if isinstance(ts, (int, float)):
        # Polymarket timestamps are ms epoch.
        val = float(ts)
        if val > 1e12:
            val /= 1000.0
        return dt.datetime.fromtimestamp(val, tz=dt.UTC)
    s = str(ts).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.UTC)
    except ValueError:
        return dt.datetime.now(dt.UTC)


class BookManager:
    """Owns the live books for all subscribed tokens and dispatches events."""

    def __init__(self):
        self.books: dict[str, OrderBook] = {}

    def ensure(self, token_id: str, outcome: str | None = None) -> OrderBook:
        book = self.books.get(token_id)
        if book is None:
            book = OrderBook(token_id=token_id, outcome=outcome)
            self.books[token_id] = book
        elif outcome and book.outcome is None:
            book.outcome = outcome
        return book

    def handle_message(self, msg: dict) -> None:
        asset_id = msg.get("asset_id") or msg.get("token_id")
        if asset_id is None:
            return
        self.ensure(str(asset_id)).handle(msg)

    def mark_gap(self, token_id: str) -> None:
        """Flag a book as having a coverage gap (e.g. after reconnect)."""
        book = self.books.get(token_id)
        if book:
            book.seq_gap = True

    def mark_all_gap(self) -> None:
        for b in self.books.values():
            b.seq_gap = True
