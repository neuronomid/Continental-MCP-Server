"""Phase 1 — session labelling across DST, holiday/half-day/weekend integrity."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from pm_sessions import (
    SESSION_MODEL_VERSION,
    label_instant,
    materialize_calendar,
    seconds_to_next_boundary,
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


def test_tokyo_morning_primary():
    # 2026-07-07 01:00 UTC → 10:00 JST → Tokyo open; London/NY closed.
    lbl = label_instant(utc(2026, 7, 7, 1, 0), PROVIDER)
    assert lbl.session_primary == "tokyo"
    assert lbl.session_overlap is None


def test_off_session_dead_hours():
    # 2026-07-07 06:30 UTC (July→BST): Tokyo 15:30 JST closed, London 07:30 BST not
    # yet open (opens 08:00 BST = 07:00 UTC), NY 02:30 EDT closed → off_session.
    lbl = label_instant(utc(2026, 7, 7, 6, 30), PROVIDER)
    assert lbl.session_primary == "off_session"


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
    assert label_instant(utc(2026, 1, 15, 7, 30), PROVIDER).session_primary == "off_session"
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
    secs, nxt = seconds_to_next_boundary(utc(2026, 7, 7, 7, 0))  # off_session
    assert secs > 0
    assert nxt in {"london", "tokyo", "new_york"}


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
