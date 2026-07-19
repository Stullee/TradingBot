from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from tradingbot.calendars import ExchangeCalendar, create_exchange_calendar
from tradingbot.market_hours import MarketSession

EASTERN = ZoneInfo("US/Eastern")


# --- real pandas_market_calendars data ------------------------------------

def test_nyse_regular_day_window():
    cal = ExchangeCalendar("NYSE", "US/Eastern")
    assert cal.session_window(date(2026, 7, 17)) == (time(9, 30), time(16, 0))  # a Friday


def test_nyse_early_close_is_reflected():
    cal = ExchangeCalendar("NYSE", "US/Eastern")
    # Day after Thanksgiving 2026: 13:00 ET close
    assert cal.session_window(date(2026, 11, 27)) == (time(9, 30), time(13, 0))


def test_nyse_holiday_and_weekend_are_closed():
    cal = ExchangeCalendar("NYSE", "US/Eastern")
    assert cal.session_window(date(2026, 12, 25)) is None  # Christmas
    assert cal.session_window(date(2026, 7, 18)) is None  # a Saturday


def test_hong_kong_half_day():
    cal = ExchangeCalendar("XHKG", "Asia/Hong_Kong")
    # Christmas Eve 2026: morning session only (12:00 HKT close)
    window = cal.session_window(date(2026, 12, 24))
    assert window is not None
    assert window[1] == time(12, 0)


def test_create_exchange_calendar_degrades_gracefully():
    assert create_exchange_calendar(None, "UTC") is None
    assert create_exchange_calendar("NO-SUCH-CALENDAR", "UTC") is None


# --- MarketSession using a calendar ---------------------------------------

class FakeCalendar:
    def __init__(self, windows: dict):
        self.windows = windows

    def session_window(self, day):
        return self.windows.get(day)


def dt(h: int, m: int, day: str = "2026-11-27") -> datetime:
    return datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00").replace(tzinfo=EASTERN)


def make_session(calendar, entry_delay: int = 0) -> MarketSession:
    return MarketSession(
        "US/Eastern",
        "09:30",
        "16:00",
        no_new_entries_before_close_min=15,
        flatten_before_close_min=5,
        calendar=calendar,
        entry_delay_after_open_min=entry_delay,
    )


def test_early_close_moves_the_flatten_window():
    """The bug this guards against: on a 13:00 early close the static 16:00
    session kept 'trading' all afternoon and only tried to flatten at 15:55
    -- a DAY market order into a closed exchange, which expired unfilled and
    left the position (whose DAY bracket children also expired) unprotected
    overnight."""
    early_close = FakeCalendar({date(2026, 11, 27): (time(9, 30), time(13, 0))})
    session = make_session(early_close)
    assert session.is_open(dt(12, 0)) is True
    assert session.should_stop_new_entries(dt(12, 46)) is True  # 13:00 - 15min
    assert session.should_flatten(dt(12, 56)) is True  # 13:00 - 5min
    assert session.is_open(dt(13, 30)) is False  # static hours would say True
    # ...whereas a static session would still think it's mid-session
    static = make_session(calendar=None)
    assert static.should_flatten(dt(12, 56)) is False


def test_holiday_closes_the_whole_day():
    session = make_session(FakeCalendar({}))  # no session window today
    assert session.is_open(dt(10, 0)) is False
    assert session.should_flatten(dt(15, 56)) is False
    assert session.should_stop_new_entries(dt(15, 50)) is False


def test_opening_delay_blocks_early_entries_only():
    session = make_session(
        FakeCalendar({date(2026, 11, 27): (time(9, 30), time(16, 0))}), entry_delay=15
    )
    assert session.in_opening_delay(dt(9, 40)) is True
    assert session.in_opening_delay(dt(9, 45)) is False
    assert session.in_opening_delay(dt(9, 20)) is False  # before the open
    assert session.is_open(dt(9, 40)) is True  # open (exits allowed), just no entries


def test_calendar_failure_falls_back_to_static_hours():
    class BrokenCalendar:
        def session_window(self, day):
            raise RuntimeError("calendar data unavailable")

    session = make_session(BrokenCalendar())
    # first call degrades to static behavior instead of raising
    assert session.is_open(dt(10, 0)) is True
    assert session.calendar is None  # permanently disabled after the failure
