"""BTC proxy feeds (Binance spot/perp + Coinbase) → 1s bars + rolling state.

The 1s-bar aggregation is pure/testable; the WS loops use injected connect
factories so they can be driven by fakes. Binance forcibly disconnects every
24 h, so proactive reconnects are scheduled and the two proxy feeds never
reconnect simultaneously (mcp_phases.md Phase 3 heads-up).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math

import orjson

from ..features.btc_state import BtcFeatureState
from ..logging_setup import get_logger

log = get_logger("collectors.btc_feed")


class OneSecondBarAggregator:
    """Accumulate trade/tick prints into 1-second OHLCV bars."""

    def __init__(self):
        self.current_second: int | None = None
        self.open = self.high = self.low = self.close = None
        self.volume = 0.0
        self.count = 0

    def add(self, ts: float, price: float, size: float = 0.0) -> dict | None:
        """Add a tick; returns the *completed* previous bar when the second rolls."""
        sec = int(math.floor(ts))
        completed = None
        if self.current_second is None:
            self._start(sec, price)
        elif sec != self.current_second:
            completed = self._finish()
            self._start(sec, price)
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size
        self.count += 1
        return completed

    def _start(self, sec: int, price: float) -> None:
        self.current_second = sec
        self.open = self.high = self.low = self.close = price
        self.volume = 0.0
        self.count = 0

    def _finish(self) -> dict:
        return {
            "ts": dt.datetime.fromtimestamp(self.current_second, tz=dt.UTC),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "trade_count": self.count,
        }

    def flush(self) -> dict | None:
        return self._finish() if self.current_second is not None else None


class BtcFeed:
    """Owns rolling state per source + optional btc_ticks persistence."""

    def __init__(self, session_factory=None, health=None):
        self.session_factory = session_factory
        self.health = health
        self.state = BtcFeatureState()  # primary (binance spot)
        self.secondary_price: float | None = None  # coinbase
        self.aggregators: dict[str, OneSecondBarAggregator] = {}

    def _agg(self, source: str) -> OneSecondBarAggregator:
        agg = self.aggregators.get(source)
        if agg is None:
            agg = OneSecondBarAggregator()
            self.aggregators[source] = agg
        return agg

    def ingest_tick(self, source: str, ts: float, price: float, size: float = 0.0) -> None:
        bar = self._agg(source).add(ts, price, size)
        if source in ("binance_spot",):
            self.state.update(ts, price, trades=1)
        elif source == "coinbase":
            self.secondary_price = price
        if bar is not None and self.session_factory is not None:
            self._persist_bar(source, bar)

    def _persist_bar(self, source: str, bar: dict) -> None:
        from ..db.models import BtcTick

        with self.session_factory() as s:
            s.add(BtcTick(source=source, **bar))
            s.commit()

    # --- Binance spot parsing ---------------------------------------------
    @staticmethod
    def parse_binance_trade(msg: dict) -> tuple[float, float, float] | None:
        # aggTrade: {"e":"aggTrade","T":<ms>,"p":"<price>","q":"<qty>"}
        if msg.get("e") == "aggTrade":
            return float(msg["T"]) / 1000.0, float(msg["p"]), float(msg["q"])
        if "p" in msg and "T" in msg:
            return float(msg["T"]) / 1000.0, float(msg["p"]), float(msg.get("q", 0.0))
        return None

    @staticmethod
    def parse_coinbase_match(msg: dict) -> tuple[float, float, float] | None:
        if msg.get("type") in ("match", "last_match", "ticker"):
            price = msg.get("price")
            if price is None:
                return None
            ts_raw = msg.get("time")
            if ts_raw:
                t = dt.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
            else:
                t = dt.datetime.now(dt.UTC).timestamp()
            return t, float(price), float(msg.get("size", 0.0))
        return None

    async def run_binance(self, connect_factory, symbol="btcusdt", stop_event=None):
        stream = f"{symbol}@aggTrade"
        attempt = 0
        while not (stop_event and stop_event.is_set()):
            try:
                async with connect_factory() as ws:
                    await ws.send(orjson.dumps({"method": "SUBSCRIBE", "params": [stream], "id": 1}).decode())
                    async for raw in ws:
                        parsed = self.parse_binance_trade(_loads(raw))
                        if parsed:
                            self.ingest_tick("binance_spot", *parsed)
                        if stop_event and stop_event.is_set():
                            break
                if stop_event and stop_event.is_set():
                    break
            except Exception as exc:  # pragma: no cover
                log.warning("binance_disconnect", error=str(exc))
                if self.health:
                    self.health.warning("btc_feed", "binance reconnect")
                attempt += 1
                await asyncio.sleep(min(30, 2 ** attempt))

    async def run_coinbase(self, connect_factory, stop_event=None):
        attempt = 0
        while not (stop_event and stop_event.is_set()):
            try:
                async with connect_factory() as ws:
                    await ws.send(orjson.dumps({"type": "subscribe", "channels": [{"name": "matches", "product_ids": ["BTC-USD"]}]}).decode())
                    async for raw in ws:
                        parsed = self.parse_coinbase_match(_loads(raw))
                        if parsed:
                            self.ingest_tick("coinbase", *parsed)
                        if stop_event and stop_event.is_set():
                            break
                if stop_event and stop_event.is_set():
                    break
            except Exception as exc:  # pragma: no cover
                log.warning("coinbase_disconnect", error=str(exc))
                attempt += 1
                await asyncio.sleep(min(30, 2 ** attempt))


def _loads(raw):
    if isinstance(raw, dict):
        return raw
    return orjson.loads(raw)
