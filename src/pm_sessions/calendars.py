"""Exchange-calendar integrity providers.

An *integrity* label answers "was this a *real* trading session on this local
date?" for the exchange that anchors a market session:

    regular   — normal full trading day
    half_day  — early close (e.g. day after US Thanksgiving)
    holiday   — weekday the exchange is closed (e.g. July 4)
    weekend   — Saturday/Sunday

The providers are abstracted so tests can inject a deterministic
:class:`StaticCalendarProvider` and so the pure clock-based session labeller can
run without the (heavy) ``exchange_calendars`` dependency being importable.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

# exchange_calendars codes for the three anchoring exchanges.
EXCHANGE_FOR_SESSION: dict[str, str] = {
    "new_york": "XNYS",
    "london": "XLON",
    "tokyo": "XTKS",
}

Integrity = str  # "regular" | "half_day" | "holiday" | "weekend"


class CalendarProvider(Protocol):
    """Return the integrity of ``session_name`` on ``local_date``."""

    def integrity(self, session_name: str, local_date: dt.date) -> Integrity: ...


class WeekendOnlyProvider:
    """Zero-dependency fallback: only weekends are non-regular.

    Used when ``exchange_calendars`` is unavailable or when holiday awareness is
    not required. Weekdays are always ``regular``.
    """

    def integrity(self, session_name: str, local_date: dt.date) -> Integrity:
        return "weekend" if local_date.weekday() >= 5 else "regular"


class StaticCalendarProvider:
    """Deterministic provider for tests.

    ``holidays`` / ``half_days`` are keyed by exchange code (XNYS/XLON/XTKS) so a
    test can plant "July 4 is an XNYS holiday" without pulling real calendars.
    """

    def __init__(
        self,
        holidays: dict[str, set[dt.date]] | None = None,
        half_days: dict[str, set[dt.date]] | None = None,
    ) -> None:
        self.holidays = holidays or {}
        self.half_days = half_days or {}

    def integrity(self, session_name: str, local_date: dt.date) -> Integrity:
        if local_date.weekday() >= 5:
            return "weekend"
        code = EXCHANGE_FOR_SESSION.get(session_name, "")
        if local_date in self.half_days.get(code, set()):
            return "half_day"
        if local_date in self.holidays.get(code, set()):
            return "holiday"
        return "regular"


class ExchangeCalendarsProvider:
    """Real integrity via the ``exchange_calendars`` package (lazily loaded)."""

    def __init__(self) -> None:
        self._cals: dict[str, object] = {}

    def _cal(self, code: str):
        cal = self._cals.get(code)
        if cal is None:
            import exchange_calendars as xcals

            cal = xcals.get_calendar(code)
            self._cals[code] = cal
        return cal

    def integrity(self, session_name: str, local_date: dt.date) -> Integrity:
        if local_date.weekday() >= 5:
            return "weekend"
        code = EXCHANGE_FOR_SESSION.get(session_name)
        if code is None:
            return "regular"
        import pandas as pd

        cal = self._cal(code)
        ts = pd.Timestamp(local_date)
        if not cal.is_session(ts):
            # Weekday that is not a trading session → exchange holiday.
            return "holiday"
        try:
            if ts in cal.early_closes:
                return "half_day"
        except Exception:  # pragma: no cover - defensive against API drift
            pass
        return "regular"


_DEFAULT: CalendarProvider | None = None


def default_provider() -> CalendarProvider:
    """Process-wide default provider.

    Prefers the real exchange calendars; degrades to weekend-only if the package
    cannot be imported so that session labelling never hard-fails.
    """
    global _DEFAULT
    if _DEFAULT is None:
        try:
            import exchange_calendars  # noqa: F401

            _DEFAULT = ExchangeCalendarsProvider()
        except Exception:  # pragma: no cover
            _DEFAULT = WeekendOnlyProvider()
    return _DEFAULT
