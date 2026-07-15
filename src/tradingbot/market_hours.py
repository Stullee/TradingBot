"""US equity regular trading hours helpers (US/Eastern). Does not account for
market holidays/early closes -- IB will simply reject data/orders on those days;
for full holiday-awareness, plug in `pandas_market_calendars` here later."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("US/Eastern")


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class MarketHours:
    def __init__(
        self,
        market_open: str,
        market_close: str,
        no_new_entries_before_close_min: int,
        flatten_before_close_min: int,
    ):
        self.open_t = _parse_hhmm(market_open)
        self.close_t = _parse_hhmm(market_close)
        self.no_new_entries_before_close_min = no_new_entries_before_close_min
        self.flatten_before_close_min = flatten_before_close_min

    @staticmethod
    def now_eastern() -> datetime:
        return datetime.now(EASTERN)

    def _close_dt(self, now: datetime) -> datetime:
        return now.replace(
            hour=self.close_t.hour, minute=self.close_t.minute, second=0, microsecond=0
        )

    def is_market_open(self, now: datetime | None = None) -> bool:
        now = now or self.now_eastern()
        if now.weekday() >= 5:
            return False
        return self.open_t <= now.time() <= self.close_t

    def should_stop_new_entries(self, now: datetime | None = None) -> bool:
        now = now or self.now_eastern()
        cutoff = self._close_dt(now) - timedelta(minutes=self.no_new_entries_before_close_min)
        return now >= cutoff

    def should_flatten(self, now: datetime | None = None) -> bool:
        now = now or self.now_eastern()
        cutoff = self._close_dt(now) - timedelta(minutes=self.flatten_before_close_min)
        return now >= cutoff
