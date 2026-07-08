"""Watchdog: disk, snapshot miss-rate, WS reconnect storms, clock drift → alerts.

Each check is a pure function returning a structured verdict; :class:`Watchdog`
wires them to the :class:`HealthMonitor` so every breach produces a durable event
*and* a Telegram alert (Phase-10 "zero silent failures").
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass
class DiskStatus:
    used_pct: float
    warn: bool
    critical: bool


def check_disk(path: str = "/", warn_pct: float = 80.0, critical_pct: float = 90.0) -> DiskStatus:
    usage = shutil.disk_usage(path)
    used_pct = usage.used / usage.total * 100.0
    return DiskStatus(used_pct=used_pct, warn=used_pct >= warn_pct, critical=used_pct >= critical_pct)


def snapshot_miss_rate(expected: int, actual: int) -> float:
    if expected <= 0:
        return 0.0
    return max(0.0, (expected - actual) / expected)


def is_reconnect_storm(reconnects: int, window_minutes: float, threshold_per_hour: float = 6.0) -> bool:
    if window_minutes <= 0:
        return False
    per_hour = reconnects / (window_minutes / 60.0)
    return per_hour > threshold_per_hour


class Watchdog:
    def __init__(self, health, settings=None):
        self.health = health
        self.settings = settings

    def run_disk_check(self, path: str = "/", warn_pct: float = 80.0, critical_pct: float = 90.0) -> DiskStatus:
        st = check_disk(path, warn_pct, critical_pct)
        if st.critical:
            self.health.critical("watchdog", f"disk critical {st.used_pct:.1f}%", {"used_pct": st.used_pct})
        elif st.warn:
            self.health.warning("watchdog", f"disk high {st.used_pct:.1f}%", {"used_pct": st.used_pct})
        return st

    def run_miss_rate_check(self, expected: int, actual: int, threshold: float = 0.02) -> float:
        rate = snapshot_miss_rate(expected, actual)
        if rate > threshold:
            self.health.warning(
                "watchdog", f"snapshot miss rate {rate:.1%}", {"expected": expected, "actual": actual}
            )
        return rate

    def run_reconnect_check(self, reconnects: int, window_minutes: float, threshold_per_hour: float = 6.0) -> bool:
        storm = is_reconnect_storm(reconnects, window_minutes, threshold_per_hour)
        if storm:
            self.health.warning("watchdog", f"WS reconnect storm: {reconnects} in {window_minutes}m")
        return storm

    def run_clock_check(self, offset_ms: float, abort_ms: float = 250.0) -> bool:
        if offset_ms > abort_ms:
            self.health.critical("watchdog", f"clock drift {offset_ms:.0f}ms > {abort_ms}ms")
            return False
        return True

    def run_silence_check(self, services: list[str], max_silence_s: float = 180.0) -> list[str]:
        silent = self.health.silent_services(services, max_silence_s)
        for svc in silent:
            self.health.critical("watchdog", f"collector silent > {max_silence_s}s: {svc}")
        return silent
