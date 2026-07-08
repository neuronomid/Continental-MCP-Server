"""Phase 10 — collector supervisor heartbeats, sd_notify no-op, CLI wiring."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from pmre.cli import build_parser
from pmre.collectors.supervisor import COLLECTOR_NAMES, run_supervised
from pmre.db.models import SystemHealthEvent
from pmre.ops.health import HealthMonitor
from pmre.ops.systemd_notify import notify_ready, sd_notify, watchdog_interval_s


def test_sd_notify_is_noop_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False
    assert notify_ready() is False


def test_watchdog_interval_parsing(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "6000000")
    assert watchdog_interval_s() == 3.0  # half of 6s
    monkeypatch.delenv("WATCHDOG_USEC")
    assert watchdog_interval_s() is None


async def test_supervised_emits_heartbeats_and_stops(db):
    hm = HealthMonitor(db.session_factory)
    stop = asyncio.Event()

    async def coro(stop_event):
        # do a little work then request stop
        await asyncio.sleep(0.05)
        stop_event.set()

    await asyncio.wait_for(
        run_supervised("test_collector", coro, hm, stop=stop, heartbeat_s=0.01), timeout=3
    )
    with db.session() as s:
        hbs = s.execute(
            select(SystemHealthEvent).where(
                SystemHealthEvent.service == "test_collector",
                SystemHealthEvent.kind == "heartbeat",
            )
        ).scalars().all()
    assert len(hbs) >= 1


async def test_supervised_crash_alerts_and_reraises(db):
    hm = HealthMonitor(db.session_factory)

    async def boom(stop_event):
        raise RuntimeError("kaboom")

    try:
        await run_supervised("crasher", boom, hm, heartbeat_s=0.01)
    except RuntimeError:
        pass
    with db.session() as s:
        crit = s.execute(
            select(SystemHealthEvent).where(SystemHealthEvent.severity == "critical")
        ).scalars().all()
    assert any("collector crashed" in e.message for e in crit)


def test_cli_parser_has_all_commands():
    parser = build_parser()
    # argparse subparsers registered
    subs = parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
    for cmd in ["migrate", "materialize-calendar", "analytics-hourly", "analytics-daily",
                "analytics-weekly", "daily-report", "export", "pipeline-demo",
                "collector", "watchdog", "serve-rest", "serve-mcp"]:
        assert cmd in subs


def test_collector_names_constant():
    assert set(COLLECTOR_NAMES) == {"discovery", "clob_ws", "snapshotter", "btc_feed", "resolution"}
