"""CLOB market-channel WebSocket client.

Maintains live books via :class:`BookManager`, archives every raw message, and
auto-resubscribes with jittered backoff (flagging a coverage gap on every
reconnect). The message-dispatch and reconnect-flagging logic is unit-tested;
the socket loop itself is exercised against a fake WS in tests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random

import orjson

from ..logging_setup import get_logger
from .orderbook import BookManager

log = get_logger("collectors.clob_ws")


class ClobWebSocketClient:
    def __init__(
        self,
        ws_url: str,
        book_manager: BookManager | None = None,
        raw_archive=None,
        health=None,
        max_backoff_s: float = 30.0,
    ):
        self.ws_url = ws_url
        self.books = book_manager or BookManager()
        self.raw_archive = raw_archive
        self.health = health
        self.max_backoff_s = max_backoff_s
        self.asset_ids: set[str] = set()
        self._running = False
        self.reconnects = 0

    def subscribe(self, asset_ids: list[str]) -> None:
        self.asset_ids.update(asset_ids)

    def _subscribe_payload(self) -> dict:
        return {"type": "market", "assets_ids": sorted(self.asset_ids)}

    def dispatch(self, raw: str | bytes | dict) -> None:
        """Parse + apply one raw message (also archives it)."""
        if isinstance(raw, (str, bytes)):
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError:
                log.warning("bad_ws_message")
                return
        else:
            msg = raw
        messages = msg if isinstance(msg, list) else [msg]
        for m in messages:
            if not isinstance(m, dict):
                continue
            if self.raw_archive is not None:
                key = str(m.get("asset_id") or m.get("market") or "unknown")
                self.raw_archive.append("clob_ws", key, m)
            self.books.handle_message(m)

    def on_reconnect(self) -> None:
        """Called after a socket drop: flag all books as gapped until refreshed."""
        self.reconnects += 1
        self.books.mark_all_gap()
        if self.health:
            self.health.warning(
                "clob_ws", f"reconnect #{self.reconnects}", {"assets": len(self.asset_ids)}
            )

    async def _backoff(self, attempt: int) -> None:
        delay = min(self.max_backoff_s, (2 ** attempt)) * (0.5 + random.random())
        await asyncio.sleep(delay)

    async def run(self, connect_factory, stop_event: asyncio.Event | None = None) -> None:
        """Connect/consume loop. ``connect_factory`` yields an async-iterable WS.

        Injected so the socket can be faked in tests. On any exception the loop
        flags a gap, backs off and resubscribes.
        """
        self._running = True
        attempt = 0
        while self._running and not (stop_event and stop_event.is_set()):
            try:
                async with connect_factory(self.ws_url) as ws:
                    await ws.send(orjson.dumps(self._subscribe_payload()).decode())
                    attempt = 0
                    async for raw in ws:
                        self.dispatch(raw)
                        if stop_event and stop_event.is_set():
                            break
            except Exception as exc:  # pragma: no cover - exercised via fake in tests
                log.warning("ws_disconnect", error=str(exc))
                self.on_reconnect()
                attempt += 1
                await self._backoff(attempt)
            else:
                if stop_event and stop_event.is_set():
                    break
                self.on_reconnect()
                attempt += 1
                await self._backoff(attempt)

    def stop(self) -> None:
        self._running = False


def compare_books(ws_book, rest_book: dict, tolerance: float = 1e-6) -> dict:
    """Cross-check a live WS book against a REST ``GET /book`` snapshot.

    Returns a diff report; ``divergent=True`` when best bid/ask disagree beyond
    tolerance (→ health event + stale_book_flag in the caller).
    """
    from .orderbook import OrderBook

    rest = OrderBook(token_id=ws_book.token_id)
    rest.apply_book(rest_book)
    report = {
        "ws_best_bid": ws_book.best_bid(),
        "ws_best_ask": ws_book.best_ask(),
        "rest_best_bid": rest.best_bid(),
        "rest_best_ask": rest.best_ask(),
    }

    def _close(a, b):
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(a - b) <= tolerance

    report["divergent"] = not (
        _close(report["ws_best_bid"], report["rest_best_bid"])
        and _close(report["ws_best_ask"], report["rest_best_ask"])
    )
    report["checked_at"] = dt.datetime.now(dt.UTC).isoformat()
    return report
