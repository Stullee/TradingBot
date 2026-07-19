"""Generic trading-session helpers: a timezone-aware open/close window, or an
always-open mode (for 24/7 assets like crypto) that still applies a daily
flatten/no-new-entries cutoff if configured -- preserving the bot's
no-overnight-risk discipline even for a market that never technically closes.

When a `calendar` (see tradingbot.calendars) is attached, each date's actual
session window comes from the exchange calendar -- so holidays don't trade
and **early closes flatten before the real close** instead of firing a
market order into a closed exchange (which expires unfilled and leaves the
position unprotected overnight once the DAY bracket children expire too).
Without a calendar, behaves exactly as before: static hours, weekend check,
no holiday awareness. Single-window only: does not model split sessions
(e.g. Hong Kong's midday trading halt)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class MarketSession:
    def __init__(
        self,
        timezone: str,
        open_time: str | None,
        close_time: str | None,
        no_new_entries_before_close_min: int,
        flatten_before_close_min: int,
        always_open: bool = False,
        trade_weekends: bool = False,
        calendar=None,
        entry_delay_after_open_min: int = 0,
    ):
        self.tz = ZoneInfo(timezone)
        self.always_open = always_open
        self.trade_weekends = trade_weekends
        self.open_t = parse_hhmm(open_time) if open_time else None
        self.close_t = parse_hhmm(close_time) if close_time else None
        self.no_new_entries_before_close_min = no_new_entries_before_close_min
        self.flatten_before_close_min = flatten_before_close_min
        # Duck-typed: anything with session_window(date) -> (time, time) | None.
        # Ignored for always_open markets (crypto has no exchange calendar).
        self.calendar = None if always_open else calendar
        self.entry_delay_after_open_min = entry_delay_after_open_min

    def now_local(self) -> datetime:
        return datetime.now(self.tz)

    def _window(self, now: datetime) -> tuple[time | None, time | None] | None:
        """Today's actual (open, close) wall-clock window. None = closed all
        day (weekend/holiday). Falls back to the static preset hours if the
        calendar errors out (and disables it, logging once via calendars'
        own warning path is not available here -- so just degrade silently
        to the previous behavior rather than flapping)."""
        if self.calendar is not None:
            try:
                return self.calendar.session_window(now.date())
            except Exception:  # noqa: BLE001 - calendar failure must not stop trading
                self.calendar = None  # degrade permanently to static hours
        if not self.trade_weekends and now.weekday() >= 5:
            return None
        return (self.open_t, self.close_t)

    def _at(self, now: datetime, t: time) -> datetime:
        return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    def is_open(self, now: datetime | None = None) -> bool:
        if self.always_open:
            return True
        now = now or self.now_local()
        window = self._window(now)
        if window is None:
            return False
        open_t, close_t = window
        if open_t is None or close_t is None:
            return False
        return open_t <= now.time() <= close_t

    def _close_today(self, now: datetime) -> time | None:
        if self.always_open:
            return self.close_t  # synthetic daily checkpoint, calendar-free
        window = self._window(now)
        if window is None:
            return None
        return window[1]

    def should_stop_new_entries(self, now: datetime | None = None) -> bool:
        now = now or self.now_local()
        close_t = self._close_today(now)
        if close_t is None:
            return False
        cutoff = self._at(now, close_t) - timedelta(minutes=self.no_new_entries_before_close_min)
        return now >= cutoff

    def should_flatten(self, now: datetime | None = None) -> bool:
        now = now or self.now_local()
        close_t = self._close_today(now)
        if close_t is None:
            return False
        cutoff = self._at(now, close_t) - timedelta(minutes=self.flatten_before_close_min)
        return now >= cutoff

    def in_opening_delay(self, now: datetime | None = None) -> bool:
        """True during the first entry_delay_after_open_min minutes of the
        session -- the opening auction/first bars, where spreads are widest
        and ATR/VWAP haven't settled into the day's regime yet. Entries are
        blocked; exits/flattens are not. Always False for always-open
        markets (no meaningful daily open) or when the delay is 0."""
        if self.always_open or self.entry_delay_after_open_min <= 0:
            return False
        now = now or self.now_local()
        window = self._window(now)
        if window is None or window[0] is None:
            return False
        open_dt = self._at(now, window[0])
        return open_dt <= now < open_dt + timedelta(minutes=self.entry_delay_after_open_min)
