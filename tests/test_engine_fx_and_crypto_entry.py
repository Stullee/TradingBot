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
import json
from types import SimpleNamespace

import pandas as pd

from tradingbot.config import Settings
from tradingbot.engine import TradingEngine
from tradingbot.news.shadow_trade import ShadowTradeTracker
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
    # Ends at "now", not a fixed historical date -- the engine's own
    # staleness gate (MAX_FRESH_ENTRY_BAR_AGE_SEC) would otherwise treat
    # every fixture bar as too old to trade on and shadow it instead,
    # which isn't what these FX/shorting tests are exercising.
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=n, freq="5min")
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
    assert engine.latest_signals[symbol]["gate"] == "waiting for FX rate"

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
    # ...bare, with engine-managed exits (ZeroHash: no stops, no children)
    assert engine.orders.kwargs[0]["attach_exits"] is False
    # latch advanced -- the bar is consumed after the successful attempt
    assert engine._last_bar_start[symbol] == engine.bars.dataframe(symbol).index[-1]


def test_crypto_short_signal_is_skipped_not_ordered():
    engine, symbol = make_engine(Signal.SHORT)
    engine.base_currency = "USD"  # no FX involved

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine.orders.placed == []
    # deliberately skipped, not deferred: the bar is consumed...
    assert engine._last_bar_start[symbol] == engine.bars.dataframe(symbol).index[-1]
    # ...and the dashboard still shows the SHORT reading it acted on,
    # with the "why idle" gate explaining the skip
    assert engine.latest_signals[symbol]["signal"] == "SHORT"
    assert engine.latest_signals[symbol]["gate"] == "short unsupported (crypto)"


def make_stale_df(n: int = 30, age_min: float = 20.0) -> pd.DataFrame:
    idx = pd.date_range(
        end=pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=age_min), periods=n, freq="5min"
    )
    return pd.DataFrame(
        {"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
         "close": [100.0] * n, "volume": [1000] * n},
        index=idx,
    )


def test_stale_bar_shadows_instead_of_placing_a_real_order(tmp_path):
    """The core of the delayed-data mode: a signal computed off a bar
    running well behind wall-clock time must not become a real order --
    the real fill could already be at a very different price by the time
    it reaches the exchange. Tracked as a shadow trade instead."""
    engine, symbol = make_engine(Signal.LONG)
    engine.base_currency = "USD"
    engine.bars = FakeBars(make_stale_df(age_min=20.0))
    engine.delayed_shadow = ShadowTradeTracker(tmp_path / "delayed.jsonl", max_hold_min=240)

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))

    assert engine.orders.placed == []
    assert engine.delayed_shadow.has_open(symbol)
    trade = engine.delayed_shadow.open_trades[symbol]
    assert trade.direction == "LONG"
    assert trade.entry_price == 100.0
    assert "shadow only" in engine.latest_signals[symbol]["gate"]
    # the bar is still consumed, same as any other acted-on signal -- this
    # doesn't loop retrying the same bar forever
    assert engine._last_bar_start[symbol] == engine.bars.dataframe(symbol).index[-1]


def test_stale_bar_does_not_stack_a_second_shadow_trade(tmp_path):
    engine, symbol = make_engine(Signal.LONG)
    engine.base_currency = "USD"
    engine.bars = FakeBars(make_stale_df(age_min=20.0))
    engine.delayed_shadow = ShadowTradeTracker(tmp_path / "delayed.jsonl", max_hold_min=240)
    engine.delayed_shadow.open(
        symbol, "LONG", 99.0, 98.0, 101.0, headline="x", confidence=0.0, rationale="x"
    )

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))

    assert engine.orders.placed == []
    assert engine.delayed_shadow.open_trades[symbol].entry_price == 99.0  # unchanged


def test_fresh_bar_places_a_real_order_not_a_shadow(tmp_path):
    """The boundary case: a bar just under the staleness cutoff must still
    go through the normal real-order path, unaffected by this gate."""
    engine, symbol = make_engine(Signal.LONG)
    engine.base_currency = "USD"
    engine.bars = FakeBars(make_stale_df(age_min=1.0))
    engine.delayed_shadow = ShadowTradeTracker(tmp_path / "delayed.jsonl", max_hold_min=240)

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))

    assert len(engine.orders.placed) == 1
    assert not engine.delayed_shadow.open_trades


class FakeSession:
    def __init__(self, is_open: bool = True, should_flatten: bool = False):
        self._is_open = is_open
        self._should_flatten = should_flatten

    def is_open(self) -> bool:
        return self._is_open

    def should_flatten(self) -> bool:
        return self._should_flatten


def test_update_delayed_shadow_trades_closes_on_target_hit(tmp_path):
    engine, symbol = make_engine(Signal.LONG)
    engine.delayed_shadow = ShadowTradeTracker(tmp_path / "delayed.jsonl", max_hold_min=240)
    engine.delayed_shadow.open(
        symbol, "LONG", 100.0, 98.0, 102.0, headline="x", confidence=0.0, rationale="x"
    )
    spec = engine.spec_by_symbol[symbol]
    engine.market_sessions[spec.market] = FakeSession(is_open=True)
    df = make_stale_df(age_min=20.0)
    df["close"] = 103.0  # above target
    engine.bars = FakeBars(df)

    engine._update_delayed_shadow_trades()

    assert not engine.delayed_shadow.has_open(symbol)
    closed = json.loads((tmp_path / "delayed.jsonl").read_text().strip())
    assert closed["status"] == "WIN"


def test_update_delayed_shadow_trades_flattens_at_session_close(tmp_path):
    engine, symbol = make_engine(Signal.LONG)
    engine.delayed_shadow = ShadowTradeTracker(tmp_path / "delayed.jsonl", max_hold_min=240)
    engine.delayed_shadow.open(
        symbol, "LONG", 100.0, 98.0, 102.0, headline="x", confidence=0.0, rationale="x"
    )
    spec = engine.spec_by_symbol[symbol]
    engine.market_sessions[spec.market] = FakeSession(is_open=True, should_flatten=True)
    engine.bars = FakeBars(make_stale_df(age_min=20.0))  # close=100, no target/stop hit

    engine._update_delayed_shadow_trades()

    assert not engine.delayed_shadow.has_open(symbol)  # force-closed, not left riding
    closed = json.loads((tmp_path / "delayed.jsonl").read_text().strip())
    assert closed["status"] == "FLATTENED"


def test_update_delayed_shadow_trades_skips_a_closed_market(tmp_path):
    """Mirrors NewsMonitor's own reasoning: max-hold timeout is wall-clock
    based, so updating against a market-closed (hours-stale) price could
    fabricate a TIMEOUT close using a price nothing actually traded at."""
    engine, symbol = make_engine(Signal.LONG)
    engine.delayed_shadow = ShadowTradeTracker(tmp_path / "delayed.jsonl", max_hold_min=240)
    engine.delayed_shadow.open(
        symbol, "LONG", 100.0, 98.0, 102.0, headline="x", confidence=0.0, rationale="x"
    )
    spec = engine.spec_by_symbol[symbol]
    engine.market_sessions[spec.market] = FakeSession(is_open=False)
    engine.bars = FakeBars(make_stale_df(age_min=20.0))

    engine._update_delayed_shadow_trades()

    assert engine.delayed_shadow.has_open(symbol)  # untouched


class FakeBarsNoData:
    """No bars at all -- either "not entitled" or merely "not arrived yet",
    which the dashboard's why-idle gate must tell apart."""

    def __init__(self, permanently_unavailable: bool):
        self._permanently_unavailable = permanently_unavailable

    def dataframe(self, symbol: str):
        return None

    def is_permanently_unavailable(self, symbol: str) -> bool:
        return self._permanently_unavailable


def test_permanently_unavailable_symbol_gets_an_explanatory_gate():
    """Confirmed live: crypto/EU symbols stuck on a permanent 'No market
    data permissions' error showed nothing on the dashboard's why-idle
    column -- _process_symbol returned before ever calling _set_gate."""
    engine, symbol = make_engine(Signal.LONG)
    engine.bars = FakeBarsNoData(permanently_unavailable=True)

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine.orders.placed == []
    assert engine.latest_signals[symbol]["gate"] == "no market data permission for this venue"


def test_symbol_with_merely_no_bars_yet_leaves_no_gate_set():
    """A symbol still warming up (not permission-denied) shouldn't be
    mislabeled -- no gate is set at all until there's something to report."""
    engine, symbol = make_engine(Signal.LONG)
    engine.bars = FakeBarsNoData(permanently_unavailable=False)

    run(engine._process_symbol(symbol, 100_000.0, 0, False, False))
    assert engine.orders.placed == []
    assert "gate" not in engine.latest_signals.get(symbol, {})


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
    # stocks keep plain market entries with a full native bracket
    assert engine.orders.kwargs[0]["entry_limit_price"] is None
    assert engine.orders.kwargs[0]["attach_exits"] is True


def make_crypto_position_engine(stop: float, target: float | None):
    engine, symbol = make_engine(Signal.FLAT)
    contract = engine.contracts[symbol]
    engine.broker.ib.positions = lambda: [
        SimpleNamespace(contract=SimpleNamespace(conId=contract.conId), position=121.8)
    ]
    engine.orders.has_pending_close = lambda c, q: False
    engine.orders.has_live_take_profit = lambda c, q: False
    engine.orders.flattened = []
    engine.orders.take_profits = []
    engine.orders.flatten_position = lambda c, q: engine.orders.flattened.append((c.symbol, q))
    engine.orders.place_standalone_take_profit = (
        lambda c, q, t, meta=None: engine.orders.take_profits.append((c.symbol, q, t))
    )
    engine.journal = SimpleNamespace(
        stop_price_for=lambda s: stop,
        target_price_for=lambda s: target,
        record_entry_context=lambda *a, **kw: None,
    )
    return engine, symbol


def test_crypto_exit_manager_flattens_when_stop_crossed():
    """ZeroHash has no native stops -- the engine must flatten a crypto
    position itself once price crosses the recorded stop level."""
    # last close in make_df is 100.0; stop above it -> hit for a long
    engine, symbol = make_crypto_position_engine(stop=101.0, target=105.0)
    engine._manage_crypto_exits()
    assert engine.orders.flattened == [(symbol, 121.8)]
    assert engine.orders.take_profits == []  # stopped out, no TP placed


def test_crypto_exit_manager_places_standalone_take_profit():
    """ZeroHash rejects attached children -- the TP is placed standalone
    once the position exists, sized to the real quantity."""
    engine, symbol = make_crypto_position_engine(stop=99.0, target=105.0)
    engine._manage_crypto_exits()
    assert engine.orders.flattened == []
    assert engine.orders.take_profits == [(symbol, 121.8, 105.0)]
    # a recovered context (target 0) never places a TP
    engine2, _ = make_crypto_position_engine(stop=99.0, target=0.0)
    engine2._manage_crypto_exits()
    assert engine2.orders.take_profits == []
