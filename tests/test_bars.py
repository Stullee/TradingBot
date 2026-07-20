import asyncio
from datetime import datetime, timezone

import pytest

from tradingbot.data.bars import BarStream, parse_bar_size_seconds


@pytest.mark.parametrize(
    "bar_size,expected_seconds",
    [
        ("5 mins", 300),
        ("1 min", 60),
        ("15 mins", 900),
        ("1 hour", 3600),
        ("2 hours", 7200),
        ("1 day", 86400),
        ("30 secs", 30),
        ("garbage", 300),  # unrecognized -> falls back to the standard 5-min bar
        ("5", 300),
    ],
)
def test_parse_bar_size_seconds(bar_size, expected_seconds):
    assert parse_bar_size_seconds(bar_size) == expected_seconds


class FakeBar:
    def __init__(self, date, close=100.0):
        self.date = date
        self.open = self.high = self.low = close
        self.close = close
        self.volume = 1000


class FakeContract:
    def __init__(self, symbol):
        self.symbol = symbol


class FakeIB:
    def __init__(self):
        self.cancel_calls: list[object] = []
        self.req_calls = 0

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.req_calls += 1
        # Each call returns a fresh bar list with the current call count baked
        # into the close price, so tests can tell "old" bars from "new" ones.
        return [FakeBar(datetime.now(timezone.utc), close=float(self.req_calls))]

    def cancelHistoricalData(self, bars):
        self.cancel_calls.append(bars)


def run(coro):
    return asyncio.run(coro)


def test_subscribe_live_tracks_subscription_info():
    ib = FakeIB()
    stream = BarStream(ib, "5 mins")
    run(stream.subscribe("AAPL", FakeContract("AAPL"), live_updates=True))
    assert stream.has_live_subscription("AAPL")


def test_subscribe_polled_is_not_a_live_subscription():
    ib = FakeIB()
    stream = BarStream(ib, "5 mins")
    run(stream.subscribe("BTC", FakeContract("BTC"), live_updates=False))
    assert not stream.has_live_subscription("BTC")


def test_latest_bar_time_returns_none_before_any_subscription():
    stream = BarStream(FakeIB(), "5 mins")
    assert stream.latest_bar_time("AAPL") is None


def test_latest_bar_time_reflects_the_most_recent_bar():
    ib = FakeIB()
    stream = BarStream(ib, "5 mins")
    run(stream.subscribe("AAPL", FakeContract("AAPL"), live_updates=True))
    latest = stream.latest_bar_time("AAPL")
    assert latest is not None
    assert (datetime.now(timezone.utc) - latest).total_seconds() < 5


def test_resubscribe_live_cancels_old_stream_and_requests_a_new_one():
    ib = FakeIB()
    stream = BarStream(ib, "5 mins")
    run(stream.subscribe("AAPL", FakeContract("AAPL"), live_updates=True))
    old_bars = stream._bar_lists["AAPL"]
    assert ib.req_calls == 1

    run(stream.resubscribe_live("AAPL"))

    assert ib.cancel_calls == [old_bars]
    assert ib.req_calls == 2
    assert stream._bar_lists["AAPL"] is not old_bars
    assert stream.has_live_subscription("AAPL")  # still tracked as live after resubscribing


def test_resubscribe_live_is_a_no_op_for_a_polled_symbol():
    ib = FakeIB()
    stream = BarStream(ib, "5 mins")
    run(stream.subscribe("BTC", FakeContract("BTC"), live_updates=False))
    calls_before = ib.req_calls

    run(stream.resubscribe_live("BTC"))

    assert ib.req_calls == calls_before  # nothing happened -- BTC isn't a live subscription


def test_unsubscribe_all_clears_live_tracking():
    ib = FakeIB()
    stream = BarStream(ib, "5 mins")
    run(stream.subscribe("AAPL", FakeContract("AAPL"), live_updates=True))
    stream.unsubscribe_all()
    assert not stream.has_live_subscription("AAPL")


def test_historical_request_pacer_blocks_after_budget(monkeypatch):
    """Confirmed live: restart storms + a resubscribe burst exceeded IB's
    ~60-per-10-min historical-data budget and every further request came
    back empty. The shared pacer must delay requests past the budget."""
    import asyncio

    from tradingbot.data.bars import _HistoricalRequestPacer

    pacer = _HistoricalRequestPacer(max_requests=2, window_sec=100.0)
    clock = {"now": 0.0}
    pacer._now = lambda: clock["now"]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr("tradingbot.data.bars.asyncio.sleep", fake_sleep)

    async def scenario():
        await pacer.wait_turn()
        clock["now"] += 1
        await pacer.wait_turn()
        clock["now"] += 1
        await pacer.wait_turn()  # over budget -- must wait out the window

    asyncio.run(scenario())
    assert sleeps  # the third request had to wait
    assert clock["now"] >= 100.0  # ...until the first request aged out of the window


def test_pacer_try_turn_is_non_blocking():
    from tradingbot.data.bars import _HistoricalRequestPacer

    pacer = _HistoricalRequestPacer(max_requests=1, window_sec=600.0)
    assert pacer.try_turn() is True
    assert pacer.try_turn() is False  # budget spent -- caller must skip, not wait
