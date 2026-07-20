import asyncio
import time
from datetime import datetime, timedelta, timezone

from tradingbot.config import Settings
from tradingbot.engine import RESUBSCRIBE_COOLDOWN_SEC, TradingEngine


class FakeContract:
    def __init__(self, symbol: str):
        self.symbol = symbol


class FakeBars:
    """Stands in for BarStream -- _check_stale_bars only ever calls these
    three methods, so a real BarStream/IB connection isn't needed."""

    def __init__(self):
        self.live_symbols: set[str] = set()
        self.latest_times: dict[str, datetime] = {}
        self.resubscribe_calls: list[str] = []

    def has_live_subscription(self, symbol: str) -> bool:
        return symbol in self.live_symbols

    def latest_bar_time(self, symbol: str):
        return self.latest_times.get(symbol)

    async def resubscribe_live(self, symbol: str) -> None:
        self.resubscribe_calls.append(symbol)


class AlwaysOpenSession:
    def is_open(self) -> bool:
        return True

    def today_open(self):
        return None  # no session-open reference -> age from the last bar (old behavior)


class OpenedRecentlySession:
    """Session that opened `minutes_ago` -- for open-aware staleness tests."""

    def __init__(self, minutes_ago: float):
        self._open = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)

    def is_open(self) -> bool:
        return True

    def today_open(self):
        return self._open


def make_engine(symbols: list[str], stale_minutes: float = 30.0) -> TradingEngine:
    settings = Settings(ib_port=7497, symbols=",".join(symbols))
    engine = TradingEngine(settings)
    engine.bars = FakeBars()
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    for spec in engine.symbol_specs:
        engine.contracts[spec.symbol] = FakeContract(spec.symbol)
        engine.bars.live_symbols.add(spec.symbol)
        engine.bars.latest_times[spec.symbol] = stale_time
    for market in list(engine.market_sessions):
        engine.market_sessions[market] = AlwaysOpenSession()
    return engine


def run(coro):
    return asyncio.run(coro)


async def _fast_sleep(_seconds):
    pass


def test_stale_symbols_get_resubscribed(monkeypatch):
    monkeypatch.setattr("tradingbot.engine.asyncio.sleep", _fast_sleep)
    engine = make_engine(["AAPL", "MSFT"])
    run(engine._check_stale_bars())
    assert set(engine.bars.resubscribe_calls) == {"AAPL", "MSFT"}


def test_fresh_symbols_are_left_alone():
    engine = make_engine(["AAPL"], stale_minutes=1.0)  # well under the threshold
    run(engine._check_stale_bars())
    assert engine.bars.resubscribe_calls == []


def test_cooldown_blocks_an_immediate_second_attempt(monkeypatch):
    # Reproduces the live bug: a genuine farm-wide outage keeps every
    # symbol stale check after check, and without a cooldown that meant a
    # full resubscribe burst every single STALE_BAR_CHECK_SEC forever --
    # ~20 reqHistoricalDataAsync calls/min, well over IB's pacing budget,
    # which appears to have been enough to choke the shared connection and
    # take the dashboard down with it.
    monkeypatch.setattr("tradingbot.engine.asyncio.sleep", _fast_sleep)
    engine = make_engine(["AAPL", "MSFT"])
    run(engine._check_stale_bars())
    assert len(engine.bars.resubscribe_calls) == 2

    engine.bars.resubscribe_calls.clear()
    run(engine._check_stale_bars())  # still stale, called again immediately
    assert engine.bars.resubscribe_calls == []  # cooldown blocks the retry


def test_cooldown_expires_after_the_window():
    engine = make_engine(["AAPL"])
    run(engine._check_stale_bars())
    assert engine.bars.resubscribe_calls == ["AAPL"]

    engine.bars.resubscribe_calls.clear()
    engine._last_resubscribe_attempt["AAPL"] = time.monotonic() - RESUBSCRIBE_COOLDOWN_SEC - 1
    run(engine._check_stale_bars())
    assert engine.bars.resubscribe_calls == ["AAPL"]


def test_each_symbol_has_its_own_cooldown(monkeypatch):
    monkeypatch.setattr("tradingbot.engine.asyncio.sleep", _fast_sleep)
    engine = make_engine(["AAPL", "MSFT"])
    run(engine._check_stale_bars())
    assert set(engine.bars.resubscribe_calls) == {"AAPL", "MSFT"}

    # AAPL's cooldown expired, MSFT's didn't.
    engine._last_resubscribe_attempt["AAPL"] = time.monotonic() - RESUBSCRIBE_COOLDOWN_SEC - 1
    engine.bars.resubscribe_calls.clear()
    run(engine._check_stale_bars())
    assert engine.bars.resubscribe_calls == ["AAPL"]


def test_resubscribe_bursts_are_paced_not_simultaneous(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("tradingbot.engine.asyncio.sleep", fake_sleep)

    engine = make_engine(["AAPL", "MSFT", "NVDA"])
    run(engine._check_stale_bars())

    assert set(engine.bars.resubscribe_calls) == {"AAPL", "MSFT", "NVDA"}
    assert len(sleeps) == 2  # a gap before the 2nd and 3rd, none before the 1st


def test_previous_session_bars_are_not_stale_right_after_the_open(monkeypatch):
    """Confirmed live (Monday US open): every symbol's last bar was
    Friday's close, the checker declared all 22 'stale 3936 min' and fired
    a resubscribe burst that blew IB's pacing budget. Bars cannot exist
    minutes into a new session -- age must be measured from today's open."""
    monkeypatch.setattr("tradingbot.engine.asyncio.sleep", _fast_sleep)
    engine = make_engine(["AAPL", "MSFT"], stale_minutes=3936.0)  # Friday's close
    for market in list(engine.market_sessions):
        engine.market_sessions[market] = OpenedRecentlySession(minutes_ago=2.0)
    run(engine._check_stale_bars())
    assert engine.bars.resubscribe_calls == []


def test_empty_live_subscription_recovers_after_the_open_threshold(monkeypatch):
    """The recovery path that was missing: a live subscription whose bar
    list came back empty (pacing-wiped) was skipped by the checker forever.
    It must become a resubscribe candidate once the session has been open
    longer than the staleness threshold."""
    monkeypatch.setattr("tradingbot.engine.asyncio.sleep", _fast_sleep)
    engine = make_engine(["AAPL"])
    engine.bars.latest_times.clear()  # empty bar list -> latest_bar_time() is None
    for market in list(engine.market_sessions):
        engine.market_sessions[market] = OpenedRecentlySession(minutes_ago=30.0)
    run(engine._check_stale_bars())
    assert engine.bars.resubscribe_calls == ["AAPL"]

    # ...but not in the first minutes after the open (grace period)
    engine2 = make_engine(["AAPL"])
    engine2.bars.latest_times.clear()
    for market in list(engine2.market_sessions):
        engine2.market_sessions[market] = OpenedRecentlySession(minutes_ago=5.0)
    run(engine2._check_stale_bars())
    assert engine2.bars.resubscribe_calls == []
