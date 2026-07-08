"""Deterministic BTC-5m market slug math.

Slugs follow ``btc-updown-5m-<unix_start>`` with one market every 300 s. Windows
are aligned to 5-minute boundaries; because ET is a whole-hour offset from UTC,
multiples of 300 s of unix time are simultaneously aligned to ET 5-minute
boundaries (mcp_phases.md Phase 1 heads-up).
"""

from __future__ import annotations

import datetime as dt

SLUG_PREFIX = "btc-updown-5m-"
PERIOD_S = 300


def window_start_unix(instant: dt.datetime | int | float) -> int:
    """Unix start of the window containing ``instant`` (floor to 300 s)."""
    if isinstance(instant, dt.datetime):
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=dt.UTC)
        ts = instant.timestamp()
    else:
        ts = float(instant)
    return int(ts // PERIOD_S) * PERIOD_S


def next_window_start_unix(instant: dt.datetime | int | float) -> int:
    return window_start_unix(instant) + PERIOD_S


def slug_for(unix_start: int) -> str:
    return f"{SLUG_PREFIX}{unix_start}"


def parse_slug_start(slug: str) -> int:
    if not slug.startswith(SLUG_PREFIX):
        raise ValueError(f"not a btc-5m slug: {slug!r}")
    tail = slug[len(SLUG_PREFIX):]
    try:
        return int(tail)
    except ValueError as e:
        raise ValueError(f"bad unix_start in slug: {slug!r}") from e


def start_dt(unix_start: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(unix_start, tz=dt.UTC)


def resolution_dt(unix_start: int) -> dt.datetime:
    return start_dt(unix_start + PERIOD_S)


def expected_windows(
    now: dt.datetime | int | float, n_windows: int, include_current: bool = True
) -> list[tuple[int, str]]:
    """The next ``n_windows`` (unix_start, slug), oldest first.

    With ``include_current`` the window currently open is the first element.
    """
    start = window_start_unix(now)
    first = start if include_current else start + PERIOD_S
    return [(u, slug_for(u)) for u in range(first, first + n_windows * PERIOD_S, PERIOD_S)]
