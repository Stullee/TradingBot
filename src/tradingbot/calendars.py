"""Holiday and early-close awareness via pandas_market_calendars.

Without this, market_hours only knows each exchange's *usual* open/close.
The dangerous case isn't holidays (no data arrives, nothing trades) -- it's
**early closes**: on e.g. the NYSE's 13:00 half-days the static session kept
"trading" until 16:00, the 15:55 flatten fired a DAY market order into a
closed market (which expired unfilled), and the bracket's DAY children
expired with it -- leaving an *unprotected* position held overnight across a
holiday. With a calendar attached, MarketSession uses each date's actual
close, so the flatten window lands before the real close.

Everything degrades gracefully: if pandas_market_calendars is missing or a
calendar lookup fails, sessions fall back to their static preset hours (the
old behavior) and log a warning once.
"""
from __future__ import annotations

import logging
from datetime import date, time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_CACHE_MAX_DAYS = 64


class ExchangeCalendar:
    """Per-date session windows for one exchange calendar, in that market's
    own timezone, cached per date (the schedule for a given day never
    changes intraday)."""

    def __init__(self, calendar_name: str, timezone: str):
        import pandas_market_calendars as mcal  # deferred: optional at runtime

        self._cal = mcal.get_calendar(calendar_name)
        self.name = calendar_name
        self._tz = ZoneInfo(timezone)
        self._window_cache: dict[date, tuple[time, time] | None] = {}

    def session_window(self, day: date) -> tuple[time, time] | None:
        """(open, close) local wall-clock times for `day`, or None when the
        exchange is closed all day (holiday/weekend). Early closes show up
        here as a close earlier than the usual one."""
        if day in self._window_cache:
            return self._window_cache[day]
        schedule = self._cal.schedule(start_date=day, end_date=day)
        if schedule.empty:
            window = None
        else:
            row = schedule.iloc[0]
            open_local = row["market_open"].tz_convert(self._tz)
            close_local = row["market_close"].tz_convert(self._tz)
            window = (open_local.time(), close_local.time())
        if len(self._window_cache) >= _CACHE_MAX_DAYS:
            self._window_cache.clear()
        self._window_cache[day] = window
        return window


def create_exchange_calendar(calendar_name: str | None, timezone: str) -> ExchangeCalendar | None:
    """Best-effort constructor: returns None (-> static session hours) when
    the market has no calendar, the library isn't installed, or the calendar
    name is unknown to it."""
    if not calendar_name:
        return None
    try:
        return ExchangeCalendar(calendar_name, timezone)
    except ImportError:
        log.warning(
            "pandas_market_calendars is not installed -- %s will use static session "
            "hours with no holiday/early-close awareness. `pip install "
            "pandas_market_calendars` to fix.",
            calendar_name,
        )
    except Exception as exc:  # noqa: BLE001 - a bad calendar must not block trading
        log.warning(
            "Could not load exchange calendar %r (%s) -- falling back to static "
            "session hours with no holiday/early-close awareness.",
            calendar_name,
            exc,
        )
    return None
