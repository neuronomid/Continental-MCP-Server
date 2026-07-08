"""Session definitions, instant labelling and 90-day calendar materialisation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .calendars import CalendarProvider, default_provider

# Bump when session hours / overlap rules change. Producers stamp this on every
# snapshot; consumers must match or raise.
SESSION_MODEL_VERSION = "sessions-v1"

OFF_SESSION = "off_session"


@dataclass(frozen=True)
class SessionDef:
    name: str
    tz: str
    start: dt.time
    end: dt.time
    # Higher priority wins when several sessions are open simultaneously and a
    # single "primary" must be chosen. New York is the most-watched → highest.
    priority: int

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


# Defaults per mcp_plan.md §6.0 (tune after first weeks of data).
SESSIONS: dict[str, SessionDef] = {
    "tokyo": SessionDef("tokyo", "Asia/Tokyo", dt.time(9, 0), dt.time(15, 0), priority=1),
    "london": SessionDef("london", "Europe/London", dt.time(8, 0), dt.time(16, 30), priority=2),
    "new_york": SessionDef("new_york", "America/New_York", dt.time(9, 30), dt.time(16, 0), priority=3),
}

# Named overlaps: (session_a, session_b) → overlap label. Order of the mapping is
# the resolution priority when more than one overlap is simultaneously active.
OVERLAPS: list[tuple[frozenset[str], str]] = [
    (frozenset({"london", "new_york"}), "london_ny_overlap"),
    (frozenset({"tokyo", "london"}), "tokyo_london_overlap"),
]


@dataclass(frozen=True)
class SessionLabel:
    """The full session stamp attached to a snapshot / response envelope."""

    session_primary: str
    session_overlap: str | None
    session_integrity: str
    session_model_version: str = SESSION_MODEL_VERSION
    # Which sessions were open (clock-wise) at the instant — useful for debugging
    # and for the per-session integrity breakdown.
    open_sessions: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "session_primary": self.session_primary,
            "session_overlap": self.session_overlap,
            "session_integrity": self.session_integrity,
            "session_model_version": self.session_model_version,
            "open_sessions": list(self.open_sessions),
        }


def _ensure_utc(instant: dt.datetime) -> dt.datetime:
    if instant.tzinfo is None:
        return instant.replace(tzinfo=dt.UTC)
    return instant.astimezone(dt.UTC)


def _is_open(sess: SessionDef, instant_utc: dt.datetime) -> tuple[bool, dt.date]:
    """Whether the session's *clock* is open, plus the session-local date.

    The local date is what the exchange calendar is keyed on. Holidays/weekends
    do not affect clock-openness — they only affect integrity — so a Saturday
    afternoon in New York is still ``session_primary=new_york`` but
    ``session_integrity=weekend``.
    """
    local = instant_utc.astimezone(sess.zoneinfo)
    tod = local.timetz().replace(tzinfo=None)
    is_open = sess.start <= tod < sess.end
    return is_open, local.date()


def label_instant(
    instant: dt.datetime, provider: CalendarProvider | None = None
) -> SessionLabel:
    """Label a UTC (or tz-aware) instant with its session state."""
    provider = provider or default_provider()
    instant_utc = _ensure_utc(instant)

    open_map: dict[str, dt.date] = {}
    for name, sess in SESSIONS.items():
        is_open, local_date = _is_open(sess, instant_utc)
        if is_open:
            open_map[name] = local_date

    if not open_map:
        return SessionLabel(
            session_primary=OFF_SESSION,
            session_overlap=None,
            session_integrity="regular",
            open_sessions=(),
        )

    # Primary = highest-priority open session.
    primary = max(open_map, key=lambda n: SESSIONS[n].priority)

    overlap: str | None = None
    open_names = set(open_map)
    for pair, label in OVERLAPS:
        if pair <= open_names:
            overlap = label
            break

    integrity = provider.integrity(primary, open_map[primary])
    return SessionLabel(
        session_primary=primary,
        session_overlap=overlap,
        session_integrity=integrity,
        open_sessions=tuple(sorted(open_map)),
    )


def seconds_to_next_boundary(
    instant: dt.datetime, horizon_hours: int = 48
) -> tuple[int, str]:
    """Seconds until the next session open/close boundary, and what it is.

    Scans forward minute-by-minute up to ``horizon_hours`` for the next change in
    ``session_primary``. Returns ``(seconds, next_primary)``.
    """
    provider = default_provider()
    instant_utc = _ensure_utc(instant).replace(microsecond=0)
    current = label_instant(instant_utc, provider).session_primary
    # Second-resolution scan is cheap enough (session edges are minute-aligned).
    step = dt.timedelta(seconds=30)
    horizon = instant_utc + dt.timedelta(hours=horizon_hours)
    t = instant_utc + step
    while t <= horizon:
        nxt = label_instant(t, provider).session_primary
        if nxt != current:
            # Refine to the second.
            lo = t - step
            while lo < t:
                mid = lo + dt.timedelta(seconds=1)
                if label_instant(mid, provider).session_primary != current:
                    return int((mid - instant_utc).total_seconds()), nxt
                lo = mid
            return int((t - instant_utc).total_seconds()), nxt
        t += step
    return int((horizon - instant_utc).total_seconds()), current


def current_session(provider: CalendarProvider | None = None) -> SessionLabel:
    return label_instant(dt.datetime.now(dt.UTC), provider)


@dataclass
class CalendarRow:
    calendar_date: dt.date
    session: str
    open_utc: dt.datetime | None
    close_utc: dt.datetime | None
    integrity: str
    source_calendar: str
    session_model_version: str = SESSION_MODEL_VERSION


def _session_bounds_utc(
    sess: SessionDef, local_date: dt.date
) -> tuple[dt.datetime, dt.datetime]:
    open_local = dt.datetime.combine(local_date, sess.start, sess.zoneinfo)
    close_local = dt.datetime.combine(local_date, sess.end, sess.zoneinfo)
    return (
        open_local.astimezone(dt.UTC),
        close_local.astimezone(dt.UTC),
    )


def materialize_calendar(
    start: dt.date,
    days: int = 90,
    provider: CalendarProvider | None = None,
) -> list[CalendarRow]:
    """Produce ``session_calendar`` rows for ``days`` days from ``start``.

    One row per (date, session). UTC open/close boundaries are derived from the
    tz-local session hours (so they shift with DST); integrity comes from the
    provider.
    """
    from .calendars import EXCHANGE_FOR_SESSION

    provider = provider or default_provider()
    rows: list[CalendarRow] = []
    for offset in range(days):
        d = start + dt.timedelta(days=offset)
        for name, sess in SESSIONS.items():
            open_utc, close_utc = _session_bounds_utc(sess, d)
            integrity = provider.integrity(name, d)
            rows.append(
                CalendarRow(
                    calendar_date=d,
                    session=name,
                    open_utc=open_utc,
                    close_utc=close_utc,
                    integrity=integrity,
                    source_calendar=EXCHANGE_FOR_SESSION.get(name, ""),
                )
            )
    return rows
