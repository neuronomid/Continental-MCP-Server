"""Async collector supervisor: runs a named collector with heartbeats + watchdog.

Each collector coroutine reuses the already-unit-tested client/logic modules; the
supervisor adds heartbeat rows (→ system_health_events, watched by the ops
watchdog) and systemd ``WATCHDOG=1`` pings so a hung collector is restarted.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from ..logging_setup import get_logger
from ..ops.health import HealthMonitor
from ..ops.systemd_notify import notify_ready, notify_watchdog, watchdog_interval_s

log = get_logger("collectors.supervisor")

COLLECTOR_NAMES = ("discovery", "clob_ws", "snapshotter", "btc_feed", "resolution")


async def _heartbeat_loop(health: HealthMonitor, service: str, stop: asyncio.Event,
                          interval_s: float = 60.0) -> None:
    wd_interval = watchdog_interval_s()
    ping_at = 0.0
    while not stop.is_set():
        health.heartbeat(service, {"ts": dt.datetime.now(dt.UTC).isoformat()})
        if wd_interval is not None:
            notify_watchdog()
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(interval_s, wd_interval or interval_s))
        except TimeoutError:
            pass
        ping_at += 1


async def run_supervised(service: str, coro_factory, health: HealthMonitor,
                         stop: asyncio.Event | None = None, heartbeat_s: float = 60.0) -> None:
    """Run ``coro_factory(stop)`` alongside a heartbeat loop until ``stop`` is set."""
    stop = stop or asyncio.Event()
    notify_ready()
    hb = asyncio.create_task(_heartbeat_loop(health, service, stop, heartbeat_s))
    try:
        await coro_factory(stop)
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except Exception as exc:  # pragma: no cover - collector crash → alert + reraise
        health.critical(service, f"collector crashed: {exc}")
        raise
    finally:
        stop.set()
        await hb


def build_collector_coro(name: str, settings, db, health: HealthMonitor):
    """Return an ``async def coro(stop)`` for the named collector (live network)."""
    import websockets

    if name == "btc_feed":  # pragma: no cover - live network
        from .btc_feed import BtcFeed

        feed = BtcFeed(session_factory=db.session_factory, health=health)

        async def coro(stop):
            def binance_connect():
                return websockets.connect(settings.binance_spot_ws)

            def coinbase_connect():
                return websockets.connect(settings.coinbase_ws)

            await asyncio.gather(
                feed.run_binance(binance_connect, stop_event=stop),
                feed.run_coinbase(coinbase_connect, stop_event=stop),
            )

        return coro

    if name == "clob_ws":  # pragma: no cover - live network
        from .clob_ws import ClobWebSocketClient

        client = ClobWebSocketClient(settings.clob_ws_url, health=health)

        async def coro(stop):
            def connect_factory(url):
                return websockets.connect(url)

            await client.run(connect_factory, stop_event=stop)

        return coro

    if name == "discovery":  # pragma: no cover - live network
        from .discovery import DiscoveryCollector

        collector = DiscoveryCollector(db.session_factory, settings, health=health)

        async def coro(stop):
            await collector.run(stop)

        return coro

    if name == "snapshotter":  # pragma: no cover - live network
        from .snapshotter import SnapshotCollector

        snap = SnapshotCollector(db.session_factory, settings, health=health)

        async def coro(stop):
            await snap.run(stop)

        return coro

    if name == "resolution":  # pragma: no cover - live network
        from .resolution import ResolutionCollector

        res = ResolutionCollector(db.session_factory, settings, health=health)

        async def coro(stop):
            await res.run(stop)

        return coro

    # Fallback (unreached: all five collectors have real loops) — heartbeat-only.
    async def periodic(stop):  # pragma: no cover - defensive
        while not stop.is_set():
            try:
                await asyncio.sleep(min(settings.market_period_s, 30))
            except asyncio.CancelledError:
                break

    return periodic


def run_collector(name: str, settings, db) -> None:  # pragma: no cover - runtime entry
    if name not in COLLECTOR_NAMES:
        raise SystemExit(f"unknown collector '{name}'; choose from {COLLECTOR_NAMES}")
    db.create_all()
    health = HealthMonitor(db.session_factory)
    coro = build_collector_coro(name, settings, db, health)
    asyncio.run(run_supervised(name, coro, health))
