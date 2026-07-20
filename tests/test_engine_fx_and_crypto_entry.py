"""Regression tests for two live-observed entry-path behaviors:

1. An FX failure while sizing (e.g. weekend forex market, quoteless fresh
   ticker) must DEFER the bar's signal to the next tick, not silently drop
   it -- confirmed live: the session's first real entry signal (XRP SHORT
   on a EUR-base account) was lost because the new-bar latch had already
   advanced when the FX RuntimeError aborted the tick.

2. SHORT signals on spot crypto must be skipped outright -- IB has no
   spot-crypto shorting, the order would only come back rejected.
"""
import asyncio
from types import SimpleNamespace

import pandas as pd

from tradingbot.config import Settings
from tradingbot.engine import TradingEngine
from tradingbot.strategy.base import Signal, Strategy


class StubSignalStrategy(Strategy):
    def __init__(self, signal: Signal):
        self._signal = signal

    @property
    def min_bars(self) -> int:
        return 1

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return self._signal

    def is_exit_signal(self, df: pd.DataFrame, position_is_long: bool) -> bool:
        return False


class FakeContract:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.conId = 1


class FakeBars:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def dataframe(self, symbol: str) -> pd.DataFrame:
        return self._df


class FakeOrders:
    def __init__(self):
        self.placed = []
        self.kwargs = []

    def has_pending_entry(self, con_id: int) -> bool:
        return False

    def place_bracket(self, contract, action, quantity, stop, target, **kwargs):
        self.placed.append((contract.symbol, action, quantity))
        self.kwargs.append(kwargs)
        return SimpleNamespace()


class FailingFx:
    async def convert(self, amount, from_ccy, to_ccy):
        raise RuntimeError(f"Could not determine FX rate for {from_ccy}->{to_ccy}")


class WorkingFx:
    def __init__(self, rate: float):
        self._rate = rate

    async def convert(self, amount, from_ccy, to_ccy):
        return amount * self._rate


def make_df(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-07-19 12:00", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
         "close": [100.0] * n, "volume": [1000] * n},
        index=idx,
    )


def make_engine(signal: Signal):
    settings = Settings(ib_port=7497, markets="CRYPTO:ETH", max_weekly_loss_pct=0)
    engine = TradingEngine(settings, strategy=StubSignalStrategy(signal))
    symbol = engine.symbol_specs[0].symbol
    engine.bars = FakeBars(make_df())
    engine.orders = FakeOrders()
    engine.contracts[symbol] = FakeContract(symbol)
    engine._last_bar_start[symbol] = None
    engine.journal = SimpleNamespace(
        record_entry_context=lambda *a, **kw: None, stop_price_for=lambda s: None
    )
    engine.risk.start_new_session(100_000)
    return engine, symbol


def run(coro):
    return asyncio.run(coro)


def test_fx_failure_defers_the_bar_instead_of_dropping_it():
    engine, symbol = make_engine(Signal.LONG)
    engine.base_currency = "EUR"  # ETH is USD -> conversion required
    engine.fx = FailingFx()

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine.orders.placed == []
    # latch reverted -> the same bar is seen as "new" again next tick
    assert engine._last_bar_start[symbol] is None

    engine.fx = WorkingFx(rate=0.9)
    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert len(engine.orders.placed) == 1
    symbol_placed, action, quantity = engine.orders.placed[0]
    assert (symbol_placed, action) == (symbol, "BUY")
    assert quantity > 0
    # crypto entries are marketable limits, a small buffer through the
    # close (IB rejects unit-denominated crypto market buys, error 10289)
    limit = engine.orders.kwargs[0]["entry_limit_price"]
    assert limit == 100.0 * 1.003
    # ...and without a native stop leg (ZeroHash rejects STP, error 387)
    assert engine.orders.kwargs[0]["native_stop"] is False
    # latch advanced -- the bar is consumed after the successful attempt
    assert engine._last_bar_start[symbol] == engine.bars.dataframe(symbol).index[-1]


def test_crypto_short_signal_is_skipped_not_ordered():
    engine, symbol = make_engine(Signal.SHORT)
    engine.base_currency = "USD"  # no FX involved

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine.orders.placed == []
    # deliberately skipped, not deferred: the bar is consumed...
    assert engine._last_bar_start[symbol] == engine.bars.dataframe(symbol).index[-1]
    # ...and the dashboard still shows the SHORT reading it acted on
    assert engine.latest_signals[symbol]["signal"] == "SHORT"


def test_stock_short_still_places_orders():
    settings = Settings(
        ib_port=7497, symbols="AAPL", allow_shorting=True, max_weekly_loss_pct=0
    )
    engine = TradingEngine(settings, strategy=StubSignalStrategy(Signal.SHORT))
    engine.bars = FakeBars(make_df())
    engine.orders = FakeOrders()
    engine.contracts["AAPL"] = FakeContract("AAPL")
    engine._last_bar_start["AAPL"] = None
    engine.journal = SimpleNamespace(
        record_entry_context=lambda *a, **kw: None, stop_price_for=lambda s: None
    )
    engine.risk.start_new_session(100_000)

    run(engine._process_symbol("AAPL", 100_000.0, 0, False, False))
    assert len(engine.orders.placed) == 1
    assert engine.orders.placed[0][1] == "SELL"
    assert engine.orders.placed[0][2] > 0
    # stocks keep plain market entries with a native exchange-side stop
    assert engine.orders.kwargs[0]["entry_limit_price"] is None
    assert engine.orders.kwargs[0]["native_stop"] is True


def test_synthetic_crypto_stop_flattens_when_crossed():
    """ZeroHash has no native stops -- the engine must flatten a crypto
    position itself once price crosses the recorded stop level."""
    engine, symbol = make_engine(Signal.FLAT)
    contract = engine.contracts[symbol]
    engine.broker.ib.positions = lambda: [
        SimpleNamespace(contract=SimpleNamespace(conId=contract.conId), position=121.8)
    ]
    flattened = []
    engine.orders.has_pending_close = lambda c, q: False
    engine.orders.flatten_position = lambda c, q: flattened.append((c.symbol, q))

    # last close in make_df is 100.0; stop above it -> hit for a long
    engine.journal = SimpleNamespace(
        stop_price_for=lambda s: 101.0, record_entry_context=lambda *a, **kw: None
    )
    engine._check_synthetic_crypto_stops()
    assert flattened == [(symbol, 121.8)]

    # stop safely below the price -> untouched
    flattened.clear()
    engine.journal = SimpleNamespace(
        stop_price_for=lambda s: 99.0, record_entry_context=lambda *a, **kw: None
    )
    engine._check_synthetic_crypto_stops()
    assert flattened == []
