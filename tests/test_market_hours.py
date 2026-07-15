from datetime import datetime
from zoneinfo import ZoneInfo

from tradingbot.market_hours import MarketHours

EASTERN = ZoneInfo("US/Eastern")


def make_mh() -> MarketHours:
    return MarketHours(
        "09:30", "16:00", no_new_entries_before_close_min=15, flatten_before_close_min=5
    )


def dt(h: int, m: int, date: str = "2024-06-10") -> datetime:  # 2024-06-10 is a Monday
    return datetime.fromisoformat(f"{date}T{h:02d}:{m:02d}:00").replace(tzinfo=EASTERN)


def test_market_open_during_hours():
    assert make_mh().is_market_open(dt(10, 0)) is True


def test_market_closed_before_open():
    assert make_mh().is_market_open(dt(9, 0)) is False


def test_market_closed_after_close():
    assert make_mh().is_market_open(dt(16, 30)) is False


def test_market_closed_on_weekend():
    assert make_mh().is_market_open(dt(10, 0, date="2024-06-08")) is False  # Saturday


def test_stop_new_entries_near_close():
    mh = make_mh()
    assert mh.should_stop_new_entries(dt(15, 50)) is True
    assert mh.should_stop_new_entries(dt(15, 40)) is False


def test_should_flatten_near_close():
    mh = make_mh()
    assert mh.should_flatten(dt(15, 56)) is True
    assert mh.should_flatten(dt(15, 40)) is False
