"""Command-line entrypoint: `python -m pmre <command>`.

Wires the pieces into runnable services/jobs so the same code that tests exercise
is what systemd runs (see deploy/). Network-touching collectors are thin shells
around the tested logic; offline commands (migrate, materialize-calendar,
analytics, pipeline-demo, watchdog) run without external services.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from .config import load_settings
from .db.engine import Database
from .logging_setup import get_logger

log = get_logger("cli")


def _db(settings) -> Database:
    return Database(settings.database_url)


def cmd_migrate(args, settings) -> int:
    db = _db(settings)
    db.create_all()
    db.apply_timescale()
    log.info("migrate_done", url=settings.database_url)
    print("schema created")
    return 0


def cmd_materialize_calendar(args, settings) -> int:
    from .collectors.calendar_job import CalendarMaterializer

    db = _db(settings)
    db.create_all()
    mat = CalendarMaterializer(db.session_factory)
    n = mat.run(days=args.days)
    print(f"calendar rows written: {n}")
    return 0


def cmd_analytics_hourly(args, settings) -> int:
    from .analytics.runner import HourlyAnalytics

    db = _db(settings)
    run_id = HourlyAnalytics(db.session_factory, settings).run()
    print(f"hourly run: {run_id}")
    return 0


def cmd_daily_report(args, settings) -> int:
    from .analytics.reports import generate_daily_report

    db = _db(settings)
    print(generate_daily_report(db.session_factory))
    return 0


def _push_telegram(settings, text: str) -> None:
    from .ops.alerts import AlertLevel, TelegramAlerter

    alerter = TelegramAlerter(settings.alert_telegram_bot_token, settings.alert_telegram_chat_id)
    if alerter.enabled:
        alerter.send_sync(AlertLevel.INFO, "daily-report", text[:3500])


def cmd_analytics_daily(args, settings) -> int:
    from .analytics.reports import generate_daily_report
    from .analytics.runner import HourlyAnalytics
    from .parquet_export import ParquetExporter
    from .registry.extractor import CandidateExtractor

    db = _db(settings)
    run_id = HourlyAnalytics(db.session_factory, settings).run()
    created = CandidateExtractor(db.session_factory, settings).extract_from_run(run_id)
    report = generate_daily_report(db.session_factory, run_id)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    try:
        ParquetExporter(db.session_factory, settings.parquet_dir).export_day(yesterday)
    except Exception as exc:  # pragma: no cover - export best-effort
        log.warning("daily_export_failed", error=str(exc))
    _push_telegram(settings, report)
    print(f"daily run {run_id}: {len(created)} candidates; report generated")
    return 0


def cmd_analytics_weekly(args, settings) -> int:
    from .analytics.runner import HourlyAnalytics
    from .registry.extractor import CandidateExtractor

    db = _db(settings)
    run_id = HourlyAnalytics(db.session_factory, settings).run()
    # Extraction runs the walk-forward evaluator per candidate family.
    created = CandidateExtractor(db.session_factory, settings).extract_from_run(run_id)
    print(f"weekly walk-forward run {run_id}: {len(created)} candidates validated")
    return 0


def cmd_export(args, settings) -> int:
    from .parquet_export import ParquetExporter

    db = _db(settings)
    date = dt.date.fromisoformat(args.date) if args.date else (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1))
    manifest = ParquetExporter(db.session_factory, settings.parquet_dir).export_day(date)
    print(manifest)
    return 0


def cmd_pipeline_demo(args, settings) -> int:
    from .demo import build_demo_dataset, run_full_pipeline

    db = _db(settings)
    db.create_all()
    ds = build_demo_dataset(db.session_factory, days=args.days)
    result = run_full_pipeline(db.session_factory, settings)
    print(f"seeded {ds['markets']} markets over {ds['days']} days")
    print(f"analysis run: {result['run_id']}")
    print(f"candidates discovered: {result['n_candidates']} -> {result['candidates_created']}")
    return 0


def cmd_watchdog(args, settings) -> int:
    from .ops.alerts import TelegramAlerter
    from .ops.health import HealthMonitor
    from .ops.watchdog import Watchdog

    db = _db(settings)
    db.create_all()
    alerter = TelegramAlerter(settings.alert_telegram_bot_token, settings.alert_telegram_chat_id)
    hm = HealthMonitor(db.session_factory, alerter=alerter)
    wd = Watchdog(hm, settings)
    disk = wd.run_disk_check(settings.data_dir)
    print(f"disk used: {disk.used_pct:.1f}% (warn={disk.warn} crit={disk.critical})")
    return 0


def cmd_collector(args, settings) -> int:  # pragma: no cover - runtime service
    from .collectors.supervisor import run_collector

    run_collector(args.name, settings, _db(settings))
    return 0


def cmd_serve_rest(args, settings) -> int:  # pragma: no cover - runtime server
    import uvicorn

    from .serving.rest import create_app

    db = _db(settings)
    app = create_app(db.session_factory, settings)
    uvicorn.run(app, host=settings.serving_host, port=settings.rest_port)
    return 0


def cmd_serve_mcp(args, settings) -> int:  # pragma: no cover - runtime server
    import uvicorn

    from .serving.mcp.server import create_http_app

    db = _db(settings)
    # Streamable-HTTP ASGI app wrapped with bearer-auth middleware (bind overlay IP).
    app = create_http_app(db.session_factory, settings)
    uvicorn.run(app, host=settings.serving_host, port=settings.mcp_port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmre", description="pm-research-engine CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create schema (+ timescale on postgres)").set_defaults(fn=cmd_migrate)

    mc = sub.add_parser("materialize-calendar", help="fill session_calendar N days ahead")
    mc.add_argument("--days", type=int, default=90)
    mc.set_defaults(fn=cmd_materialize_calendar)

    sub.add_parser("analytics-hourly", help="run the hourly analytics job").set_defaults(fn=cmd_analytics_hourly)
    sub.add_parser("analytics-daily", help="daily analytics + extraction + report + export").set_defaults(fn=cmd_analytics_daily)
    sub.add_parser("analytics-weekly", help="weekly walk-forward validation").set_defaults(fn=cmd_analytics_weekly)
    sub.add_parser("daily-report", help="print the daily report markdown").set_defaults(fn=cmd_daily_report)

    ex = sub.add_parser("export", help="export a day's parquet partitions")
    ex.add_argument("--date", default=None)
    ex.set_defaults(fn=cmd_export)

    pd = sub.add_parser("pipeline-demo", help="seed synthetic data + run full analytics pipeline")
    pd.add_argument("--days", type=int, default=25)
    pd.set_defaults(fn=cmd_pipeline_demo)

    col = sub.add_parser("collector", help="run a named collector as a supervised service")
    col.add_argument("name", choices=["discovery", "clob_ws", "snapshotter", "btc_feed", "resolution"])
    col.set_defaults(fn=cmd_collector)

    sub.add_parser("watchdog", help="run watchdog checks once").set_defaults(fn=cmd_watchdog)
    sub.add_parser("serve-rest", help="run the REST facade").set_defaults(fn=cmd_serve_rest)
    sub.add_parser("serve-mcp", help="run the MCP server").set_defaults(fn=cmd_serve_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    return args.fn(args, settings)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
