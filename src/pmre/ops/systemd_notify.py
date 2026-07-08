"""Minimal sd_notify support (READY / WATCHDOG) for systemd Type=notify units."""

from __future__ import annotations

import os
import socket


def sd_notify(state: str) -> bool:
    """Send a notification to systemd via ``$NOTIFY_SOCKET``. No-op if unset."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr[0] == "@":  # abstract namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
        return True
    except OSError:  # pragma: no cover
        return False


def notify_ready() -> bool:
    return sd_notify("READY=1")


def notify_watchdog() -> bool:
    return sd_notify("WATCHDOG=1")


def watchdog_interval_s() -> float | None:
    """Half of ``WATCHDOG_USEC`` (recommended ping cadence), or None."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return (int(usec) / 1_000_000.0) / 2.0
    except ValueError:  # pragma: no cover
        return None
