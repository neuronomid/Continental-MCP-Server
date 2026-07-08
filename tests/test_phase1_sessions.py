"""Phase 1 — session labelling across DST, holiday/half-day/weekend integrity."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from pm_sessions import (
    SESSION_MODEL_VERSION,
    label_instant,
    materialize_calendar,
    seconds_to_next_boundary,
    sessions_open_for,
)
from pm_sessions.calendars import StaticCalendarProvider, WeekendOnlyProvider
from pmre.collectors.calendar_job import CalendarMaterializer
from pmre.db.models import SessionCalendar


def utc(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.UTC)


PROVIDER = WeekendOnlyProvider()


# --- primary session by clock --------------------------------------------
def test_new_york_afternoon_is_ny_primary():
    # 2026-07-07 (Tue) 15:00 UTC → 11:00 ET → NY open; London also open → overlap.
    lbl = label_instant(utc(2026, 7, 7, 15, 0), PROVIDER)
    assert lbl.session_primary == "new_york"
    assert lbl.session_overlap == "london_ny_overlap"
    assert lbl.session_integrity == "regular"
    assert lbl.session_model_version == SESSION_MODEL_VERSION


def test_sessions_open_for_recovers_overlap_members():
    # From a persisted (primary, overlap) stamp we must recover every open session,
    # so analytics can credit the non-primary session it would otherwise lose.
    assert sessions_open_for("new_york", "london_ny_overlap") == frozenset(
        {"new_york", "london"}
    )
    assert sessions_open_for("london", "tokyo_london_overlap") == frozenset(
        {"london", "tokyo"}
    )
    assert sessions_open_for("new_york", None) == frozenset({"new_york"})
    assert sessions_open_for("off_session", None) == frozenset()


def test_tokyo_morning_primary():
    # 2026-07-07 01:00 UTC → 10:00 JST → Tokyo open; London/NY closed.
    lbl = label_instant(utc(2026, 7, 7, 1, 0), PROVIDER)
    assert lbl.session_primary == "tokyo"
    assert lbl.session_overlap is None


def test_transitional_gap_tokyo_to_london():
    # 2026-07-07 06:30 UTC (July→BST): Tokyo 15:30 JST closed (06:00 UTC), London
    # 07:30 BST not yet open (opens 08:00 BST = 07:00 UTC) → Tokyo→London handover.
    lbl = label_instant(utc(2026, 7, 7, 6, 30), PROVIDER)
    assert lbl.session_primary == "transitional"
    assert lbl.session_overlap is None


def test_pacific_gap_ny_to_tokyo():
    # 2026-07-07 22:00 UTC: NY closed (20:00 UTC summer), Tokyo not open until
    # 00:00 UTC → New York→Tokyo handover = pacific.
    lbl = label_instant(utc(2026, 7, 7, 22, 0), PROVIDER)
    assert lbl.session_primary == "pacific"
    assert lbl.session_overlap is None


def test_sessions_cover_24h_without_off_session():
    # Every minute of a weekday is a named session — no off_session holes.
    start = utc(2026, 7, 8, 0, 0)
    seen = set()
    for m in range(24 * 60):
        lbl = label_instant(start + dt.timedelta(minutes=m), PROVIDER)
        assert lbl.session_primary != "off_session"
        seen.add(lbl.session_primary)
    assert seen == {"tokyo", "transitional", "london", "new_york", "pacific"}


def test_london_only():
    # 07:30 UTC winter? use a clearly London-only slot: 2026-01-06 09:00 UTC.
    # London 09:00 GMT open; Tokyo 18:00 JST closed; NY 04:00 ET closed.
    lbl = label_instant(utc(2026, 1, 6, 9, 0), PROVIDER)
    assert lbl.session_primary == "london"


# --- DST correctness ------------------------------------------------------
def test_us_dst_shifts_ny_open_in_utc():
    # NY opens 09:30 ET. In winter (EST=UTC-5) that's 14:30 UTC; summer (EDT=UTC-4) 13:30 UTC.
    winter = label_instant(utc(2026, 1, 15, 14, 0), PROVIDER)  # 09:00 ET, before open
    assert winter.session_primary != "new_york"
    winter_open = label_instant(utc(2026, 1, 15, 14, 45), PROVIDER)  # 09:45 ET
    assert winter_open.session_primary == "new_york"

    summer_before = label_instant(utc(2026, 7, 15, 13, 0), PROVIDER)  # 09:00 EDT
    assert summer_before.session_primary != "new_york"
    summer_open = label_instant(utc(2026, 7, 15, 13, 45), PROVIDER)  # 09:45 EDT
    assert summer_open.session_primary == "new_york"


def test_uk_dst_shifts_london_open():
    # London opens 08:00 local. Winter 08:00 UTC; summer (BST) 07:00 UTC.
    # Winter 07:30 UTC is still the Tokyo→London gap (London not open until 08:00).
    assert label_instant(utc(2026, 1, 15, 7, 30), PROVIDER).session_primary == "transitional"
    assert label_instant(utc(2026, 7, 15, 7, 30), PROVIDER).session_primary == "london"


# --- integrity: holiday / half_day / weekend ------------------------------
def test_july4_ny_is_holiday_integrity():
    # 2026-07-03 is the observed US holiday (Jul 4 is Sat). Use static provider.
    provider = StaticCalendarProvider(holidays={"XNYS": {dt.date(2026, 7, 3)}})
    # 2026-07-03 15:00 UTC → 11:00 ET Friday → NY clock open, but holiday.
    lbl = label_instant(utc(2026, 7, 3, 15, 0), provider)
    assert lbl.session_primary == "new_york"
    assert lbl.session_integrity == "holiday"


def test_half_day_integrity():
    provider = StaticCalendarProvider(half_days={"XNYS": {dt.date(2026, 11, 27)}})
    lbl = label_instant(utc(2026, 11, 27, 15, 0), provider)  # Fri after Thanksgiving
    assert lbl.session_integrity == "half_day"


def test_weekend_integrity_even_when_clock_open():
    # Saturday 2026-07-11 15:00 UTC → 11:00 ET → NY clock open but weekend.
    lbl = label_instant(utc(2026, 7, 11, 15, 0), PROVIDER)
    assert lbl.session_primary == "new_york"
    assert lbl.session_integrity == "weekend"


def test_jpx_holiday_tokyo():
    provider = StaticCalendarProvider(holidays={"XTKS": {dt.date(2026, 5, 4)}})  # Greenery Day
    # 2026-05-04 01:00 UTC → 10:00 JST Monday → Tokyo open, holiday.
    lbl = label_instant(utc(2026, 5, 4, 1, 0), provider)
    assert lbl.session_primary == "tokyo"
    assert lbl.session_integrity == "holiday"


# --- next boundary --------------------------------------------------------
def test_seconds_to_next_boundary_positive():
    secs, nxt = seconds_to_next_boundary(utc(2026, 7, 7, 7, 0))  # London just opened
    assert secs > 0
    assert nxt in {"london", "tokyo", "new_york", "pacific", "transitional"}


# --- materialization ------------------------------------------------------
def test_materialize_calendar_90_days_three_sessions():
    rows = materialize_calendar(dt.date(2026, 7, 1), days=90, provider=PROVIDER)
    assert len(rows) == 90 * 3
    dates = {r.calendar_date for r in rows}
    assert len(dates) == 90
    for r in rows:
        assert r.session_model_version == SESSION_MODEL_VERSION
        assert r.open_utc is not None


def test_calendar_materializer_persists_and_is_idempotent(db):
    provider = StaticCalendarProvider(holidays={"XNYS": {dt.date(2026, 7, 3)}})
    mat = CalendarMaterializer(db.session_factory, provider=provider)
    written = mat.run(start=dt.date(2026, 7, 1), days=10)
    assert written == 10 * 3
    # idempotent: second run writes no *new* rows
    written2 = mat.run(start=dt.date(2026, 7, 1), days=10)
    assert written2 == 0
    with db.session() as s:
        total = s.execute(select(SessionCalendar)).scalars().all()
        assert len(total) == 10 * 3
    assert mat.integrity_on("new_york", dt.date(2026, 7, 3)) == "holiday"
    assert mat.integrity_on("new_york", dt.date(2026, 7, 4)) == "weekend"  # Saturday
