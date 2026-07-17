import asyncio
from types import SimpleNamespace

import pandas as pd

from tradingbot.config import Settings
from tradingbot.engine import TradingEngine


class FakeContract:
    def __init__(self, symbol):
        self.symbol = symbol
        self.conId = 1


class FakeBars:
    def __init__(self):
        self.frames: dict[str, pd.DataFrame] = {}

    def dataframe(self, symbol):
        return self.frames.get(symbol)


def make_df(timestamps) -> pd.DataFrame:
    n = len(timestamps)
    return pd.DataFrame(
        {"open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
         "close": [100.0] * n, "volume": [1] * n},
        index=pd.DatetimeIndex(timestamps, tz="UTC"),
    )


def run(coro):
    return asyncio.run(coro)


def make_engine():
    settings = Settings(ib_port=7497, symbols="AAPL")
    engine = TradingEngine(settings)
    engine.bars = FakeBars()
    # _process_symbol only reaches order placement if the strategy ever
    # returns non-FLAT; these tests use constant-price data specifically so
    # it won't, but stub orders anyway rather than rely on that.
    engine.orders = SimpleNamespace(place_bracket=lambda *a, **kw: None)
    spec = engine.symbol_specs[0]
    engine.contracts[spec.symbol] = FakeContract(spec.symbol)
    engine._last_bar_start[spec.symbol] = None
    return engine, spec.symbol


def test_shrinking_bar_list_after_resubscribe_still_detects_a_newer_bar():
    """The bug this guards against: bars.resubscribe_live()/refresh_polled()
    replace the whole bar list wholesale rather than appending to it, so its
    length can go down even when the newest bar is genuinely newer than
    before (confirmed live: a fresh "1 D" pull right at a session's open
    returns far fewer bars than the prior session's full count). Comparing
    list lengths made that dip look identical to "no new bar yet" forever
    after -- this symbol would silently stop getting new signals until its
    bar count organically regrew past the old high-water mark."""
    engine, symbol = make_engine()

    old_timestamps = pd.date_range("2026-07-16 09:00", periods=100, freq="5min", tz="UTC")
    engine.bars.frames[symbol] = make_df(old_timestamps)
    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine._last_bar_start[symbol] == old_timestamps[-1]

    # A fresh resubscribe pull: far fewer total bars, but a genuinely newer
    # last one -- exactly what a session-open resubscribe looks like.
    new_timestamps = pd.date_range(
        old_timestamps[-1] + pd.Timedelta(minutes=5), periods=5, freq="5min", tz="UTC"
    )
    engine.bars.frames[symbol] = make_df(new_timestamps)
    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine._last_bar_start[symbol] == new_timestamps[-1]


def test_same_latest_bar_is_not_reprocessed():
    engine, symbol = make_engine()
    timestamps = pd.date_range("2026-07-16 09:00", periods=100, freq="5min", tz="UTC")
    engine.bars.frames[symbol] = make_df(timestamps)
    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine._last_bar_start[symbol] == timestamps[-1]

    # Same dataframe again (e.g. a tick before the next bar has closed) --
    # must not be treated as a new bar.
    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine._last_bar_start[symbol] == timestamps[-1]
