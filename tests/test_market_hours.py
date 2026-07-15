from datetime import datetime
from zoneinfo import ZoneInfo

from tradingbot.market_hours import MarketSession

EASTERN = ZoneInfo("US/Eastern")


def make_mh() -> MarketSession:
    return MarketSession(
        "US/Eastern",
        "09:30",
        "16:00",
        no_new_entries_before_close_min=15,
        flatten_before_close_min=5,
    )


def dt(h: int, m: int, date: str = "2024-06-10") -> datetime:  # 2024-06-10 is a Monday
    return datetime.fromisoformat(f"{date}T{h:02d}:{m:02d}:00").replace(tzinfo=EASTERN)


def test_market_open_during_hours():
    assert make_mh().is_open(dt(10, 0)) is True


def test_market_closed_before_open():
    assert make_mh().is_open(dt(9, 0)) is False


def test_market_closed_after_close():
    assert make_mh().is_open(dt(16, 30)) is False


def test_market_closed_on_weekend():
    assert make_mh().is_open(dt(10, 0, date="2024-06-08")) is False  # Saturday


def test_stop_new_entries_near_close():
    mh = make_mh()
    assert mh.should_stop_new_entries(dt(15, 50)) is True
    assert mh.should_stop_new_entries(dt(15, 40)) is False


def test_should_flatten_near_close():
    mh = make_mh()
    assert mh.should_flatten(dt(15, 56)) is True
    assert mh.should_flatten(dt(15, 40)) is False


def test_always_open_market_is_always_open():
    mh = MarketSession(
        "UTC",
        None,
        "23:55",
        no_new_entries_before_close_min=15,
        flatten_before_close_min=5,
        always_open=True,
        trade_weekends=True,
    )
    # Saturday, and well outside any conventional exchange hours
    saturday_night = datetime.fromisoformat("2024-06-08T03:00:00").replace(tzinfo=ZoneInfo("UTC"))
    assert mh.is_open(saturday_night) is True


def test_always_open_market_still_has_a_daily_flatten_checkpoint():
    mh = MarketSession(
        "UTC",
        None,
        "23:55",
        no_new_entries_before_close_min=15,
        flatten_before_close_min=5,
        always_open=True,
        trade_weekends=True,
    )
    utc = ZoneInfo("UTC")
    just_before = datetime.fromisoformat("2024-06-08T23:49:00").replace(tzinfo=utc)
    just_after = datetime.fromisoformat("2024-06-08T23:56:00").replace(tzinfo=utc)
    assert mh.should_flatten(just_before) is False
    assert mh.should_flatten(just_after) is True
    # still "open" (continues trading) even while flagged for flattening
    assert mh.is_open(just_after) is True


def test_market_with_no_close_time_never_flattens():
    mh = MarketSession(
        "UTC", None, None, no_new_entries_before_close_min=15, flatten_before_close_min=5,
        always_open=True, trade_weekends=True,
    )
    now = datetime.fromisoformat("2024-06-08T23:56:00").replace(tzinfo=ZoneInfo("UTC"))
    assert mh.should_flatten(now) is False
    assert mh.should_stop_new_entries(now) is False
