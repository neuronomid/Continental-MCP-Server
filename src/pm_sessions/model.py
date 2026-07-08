"""Session definitions, instant labelling and 90-day calendar materialisation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .calendars import CalendarProvider, default_provider

# Bump when session hours / overlap rules change. Producers stamp this on every
# snapshot; consumers must match or raise.
# v2: added the pacific (NY close→Tokyo open) and transitional (Tokyo close→London
#     open) gap sessions so the clock is covered 24/7; off_session is now only a
#     defensive fallback and is no longer emitted under the default configuration.
SESSION_MODEL_VERSION = "sessions-v2"

# The two dead windows between the three formal exchange sessions are named so the
# clock is covered 24/7 with no ``off_session`` holes:
#   pacific       — New York close → Tokyo open (US-Pacific / pre-Asia hours)
#   transitional  — Tokyo close → London open (Asia→Europe handover)
# They are *derived* from the formal sessions' boundaries (not fixed local hours),
# so they track DST automatically. ``off_session`` is kept only as a defensive
# fallback and does not occur under the current three-session configuration.
PACIFIC = "pacific"
TRANSITIONAL = "transitional"
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


def _next_open_utc(sess: SessionDef, instant_utc: dt.datetime) -> dt.datetime:
    """The session's next clock-open boundary strictly after ``instant_utc``."""
    local = instant_utc.astimezone(sess.zoneinfo)
    open_utc = instant_utc
    for add in (0, 1, 2):
        d = local.date() + dt.timedelta(days=add)
        open_utc = dt.datetime.combine(d, sess.start, sess.zoneinfo).astimezone(dt.UTC)
        if open_utc > instant_utc:
            return open_utc
    return open_utc  # pragma: no cover - a daily session always opens within 2 days


# The session that opens *next* is the one whose opening ends the current dead
# window, which uniquely identifies the gap: Pacific ends when Tokyo opens,
# Transitional ends when London opens. No gap ever ends with New York opening
# (NY opens while London is still open — that is the london_ny overlap, not a gap).
_GAP_BEFORE_OPEN: dict[str, str] = {"tokyo": PACIFIC, "london": TRANSITIONAL}


def _classify_gap(instant_utc: dt.datetime) -> str:
    """Name the dead window between formal sessions (pacific/transitional)."""
    next_to_open = min(
        SESSIONS, key=lambda n: _next_open_utc(SESSIONS[n], instant_utc)
    )
    return _GAP_BEFORE_OPEN.get(next_to_open, OFF_SESSION)


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
        # No formal exchange session is open → we are in a named gap. Gaps have no
        # anchoring exchange, so integrity is weekend-aware on the UTC date only
        # (keeps the regular/weekend split consistent with the formal sessions so
        # weekend filtering does not silently leak gap data into the regular pool).
        gap_integrity = "weekend" if instant_utc.weekday() >= 5 else "regular"
        return SessionLabel(
            session_primary=_classify_gap(instant_utc),
            session_overlap=None,
            session_integrity=gap_integrity,
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


def sessions_open_for(
    session_primary: str | None, session_overlap: str | None
) -> frozenset[str]:
    """Recover every session that was open from a persisted ``(primary, overlap)`` stamp.

    Snapshots store only ``session_primary`` + ``session_overlap`` (not the full
    ``open_sessions`` tuple), because ``session_primary`` keeps only the
    highest-priority session when several are open. But every simultaneously-open
    pair is a *named* overlap, so the primary and the overlap label together
    recover the complete open-session set.

    Analytics uses this so a session is credited for **every** instant it was open,
    not only when it happened to win the priority tie-break. Without it the London
    session bucket silently loses its entire London/NY overlap window (its most
    active hours) to ``new_york``, and Tokyo loses the Tokyo/London overlap to
    ``london`` — i.e. those sessions look like they "never recorded" there.
    """
    open_: set[str] = set()
    if session_primary and session_primary != OFF_SESSION:
        open_.add(session_primary)
    if session_overlap:
        for pair, label in OVERLAPS:
            if label == session_overlap:
                open_.update(pair)
                break
    return frozenset(open_)


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
