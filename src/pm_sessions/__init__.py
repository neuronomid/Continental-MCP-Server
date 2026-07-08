"""pm_sessions — shared, versioned trading-session & calendar model.

This package is intentionally free of any project-specific imports so it can be
vendored/imported by *both* the research engine (``pmre``) and the trading bot
repo. Producers and consumers therefore share a single source of truth for
session labels; a ``SESSION_MODEL_VERSION`` mismatch between machines is an
*error* condition, never a warning (see mcp_plan.md §1.11 / §6.0).

Three canonical sessions defined in their native time zones (so DST is handled
automatically):

    * Tokyo      09:00–15:00  Asia/Tokyo
    * London     08:00–16:30  Europe/London
    * New York   09:30–16:00  America/New_York

Derived labels: ``london_ny_overlap``, ``tokyo_london_overlap`` and
``off_session``. Holidays never *close* a 24/7 crypto market — they change
*participation* — so they are modelled as **session integrity** labels
(``regular`` | ``holiday`` | ``half_day`` | ``weekend``) driven by the exchange
calendars (XNYS→New York, XLON→London, XTKS→Tokyo).
"""

from __future__ import annotations

from .calendars import (
    CalendarProvider,
    ExchangeCalendarsProvider,
    StaticCalendarProvider,
    WeekendOnlyProvider,
    default_provider,
)
from .model import (
    OFF_SESSION,
    OVERLAPS,
    SESSION_MODEL_VERSION,
    SESSIONS,
    SessionDef,
    SessionLabel,
    current_session,
    label_instant,
    materialize_calendar,
    seconds_to_next_boundary,
)

__all__ = [
    "SESSION_MODEL_VERSION",
    "SESSIONS",
    "OVERLAPS",
    "OFF_SESSION",
    "SessionDef",
    "SessionLabel",
    "label_instant",
    "current_session",
    "seconds_to_next_boundary",
    "materialize_calendar",
    "CalendarProvider",
    "ExchangeCalendarsProvider",
    "StaticCalendarProvider",
    "WeekendOnlyProvider",
    "default_provider",
]
