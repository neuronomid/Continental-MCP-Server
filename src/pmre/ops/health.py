"""Heartbeat writer + health-event recorder + watchdog evaluation."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ..db.models import SystemHealthEvent


class HealthMonitor:
    """Writes heartbeats/warnings/criticals to ``system_health_events``.

    A single alerter (optional) fires on warning/critical. This class is the
    single choke-point so every incident produces a durable row *and* a
    notification — the Phase-10 "zero silent failures" contract.
    """

    def __init__(self, session_factory, alerter=None):
        self.session_factory = session_factory
        self.alerter = alerter

    def _write(
        self, service: str, kind: str, severity: str, message: str, details: dict | None
    ) -> SystemHealthEvent:
        ev = SystemHealthEvent(
            service=service,
            kind=kind,
            severity=severity,
            message=message,
            details_json=details,
            ts=dt.datetime.now(dt.UTC),
        )
        with self.session_factory() as s:  # type: Session
            s.add(ev)
            s.commit()
            s.refresh(ev)
        return ev

    def heartbeat(self, service: str, details: dict | None = None) -> SystemHealthEvent:
        return self._write(service, "heartbeat", "info", "alive", details)

    def warning(self, service: str, message: str, details: dict | None = None):
        ev = self._write(service, "warning", "warning", message, details)
        if self.alerter:
            from .alerts import AlertLevel

            self.alerter.send_sync(AlertLevel.WARNING, service, message)
        return ev

    def critical(self, service: str, message: str, details: dict | None = None):
        ev = self._write(service, "critical", "critical", message, details)
        if self.alerter:
            from .alerts import AlertLevel

            self.alerter.send_sync(AlertLevel.CRITICAL, service, message)
        return ev

    def last_heartbeat(self, service: str) -> dt.datetime | None:
        with self.session_factory() as s:
            row = s.execute(
                select(SystemHealthEvent)
                .where(
                    SystemHealthEvent.service == service,
                    SystemHealthEvent.kind == "heartbeat",
                )
                .order_by(SystemHealthEvent.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
            return row.ts if row else None

    def silent_services(
        self, services: list[str], max_silence_s: float, now: dt.datetime | None = None
    ) -> list[str]:
        """Return services whose last heartbeat is older than ``max_silence_s``."""
        now = now or dt.datetime.now(dt.UTC)
        silent = []
        for svc in services:
            last = self.last_heartbeat(svc)
            if last is None or (now - last).total_seconds() > max_silence_s:
                silent.append(svc)
        return silent
