"""Daily job: materialise ``session_calendar`` 90 days ahead."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from pm_sessions import materialize_calendar
from pm_sessions.calendars import CalendarProvider

from ..db.models import SessionCalendar


class CalendarMaterializer:
    def __init__(self, session_factory, provider: CalendarProvider | None = None):
        self.session_factory = session_factory
        self.provider = provider

    def run(self, start: dt.date | None = None, days: int = 90) -> int:
        start = start or dt.datetime.now(dt.UTC).date()
        rows = materialize_calendar(start, days=days, provider=self.provider)
        written = 0
        with self.session_factory() as s:
            for r in rows:
                existing = s.execute(
                    select(SessionCalendar).where(
                        SessionCalendar.calendar_date == r.calendar_date,
                        SessionCalendar.session == r.session,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    s.add(
                        SessionCalendar(
                            calendar_date=r.calendar_date,
                            session=r.session,
                            open_utc=r.open_utc,
                            close_utc=r.close_utc,
                            integrity=r.integrity,
                            source_calendar=r.source_calendar,
                            session_model_version=r.session_model_version,
                        )
                    )
                    written += 1
                else:
                    existing.open_utc = r.open_utc
                    existing.close_utc = r.close_utc
                    existing.integrity = r.integrity
                    existing.session_model_version = r.session_model_version
            s.commit()
        return written

    def integrity_on(self, session: str, calendar_date: dt.date) -> str | None:
        with self.session_factory() as s:
            row = s.execute(
                select(SessionCalendar).where(
                    SessionCalendar.calendar_date == calendar_date,
                    SessionCalendar.session == session,
                )
            ).scalar_one_or_none()
            return row.integrity if row else None
