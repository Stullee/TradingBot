"""Generic trading-session helpers: a timezone-aware open/close window, or an
always-open mode (for 24/7 assets like crypto) that still applies a daily
flatten/no-new-entries cutoff if configured -- preserving the bot's
no-overnight-risk discipline even for a market that never technically closes.

Does not account for exchange holidays/early closes -- IB will simply reject
data/orders on those days; for full holiday-awareness, plug in
`pandas_market_calendars` here later. Single-window only: does not model
split sessions (e.g. Hong Kong's midday trading halt)."""
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
    ):
        self.tz = ZoneInfo(timezone)
        self.always_open = always_open
        self.trade_weekends = trade_weekends
        self.open_t = parse_hhmm(open_time) if open_time else None
        self.close_t = parse_hhmm(close_time) if close_time else None
        self.no_new_entries_before_close_min = no_new_entries_before_close_min
        self.flatten_before_close_min = flatten_before_close_min

    def now_local(self) -> datetime:
        return datetime.now(self.tz)

    def _close_dt(self, now: datetime) -> datetime:
        assert self.close_t is not None
        return now.replace(
            hour=self.close_t.hour, minute=self.close_t.minute, second=0, microsecond=0
        )

    def is_open(self, now: datetime | None = None) -> bool:
        if self.always_open:
            return True
        now = now or self.now_local()
        if not self.trade_weekends and now.weekday() >= 5:
            return False
        return self.open_t <= now.time() <= self.close_t

    def should_stop_new_entries(self, now: datetime | None = None) -> bool:
        if self.close_t is None:
            return False
        now = now or self.now_local()
        cutoff = self._close_dt(now) - timedelta(minutes=self.no_new_entries_before_close_min)
        return now >= cutoff

    def should_flatten(self, now: datetime | None = None) -> bool:
        if self.close_t is None:
            return False
        now = now or self.now_local()
        cutoff = self._close_dt(now) - timedelta(minutes=self.flatten_before_close_min)
        return now >= cutoff
