import pandas as pd

from tradingbot.backtest.simulator import simulate
from tradingbot.strategy.base import Signal, Strategy


class StubStrategy(Strategy):
    """Deterministic strategy for testing the simulator's bar-stepping/
    stop/target/exit mechanics in isolation, independent of real indicator
    logic (which is tested separately per-strategy)."""

    def __init__(self, entries: dict[pd.Timestamp, Signal], exits: set[pd.Timestamp]):
        self.entries = entries
        self.exits = exits

    @property
    def min_bars(self) -> int:
        return 1

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return self.entries.get(df.index[-1], Signal.FLAT)

    def is_exit_signal(self, df: pd.DataFrame, position_is_long: bool) -> bool:
        return df.index[-1] in self.exits


def flat_ohlcv(n: int, close: float = 100.0) -> pd.DataFrame:
    """Constant-range bars -> a stable, known ATR (TrueRange = high-low = 2
    every bar since the price never moves), so stop/target distances at
    entry are predictable: ATR=2.0."""
    idx = pd.date_range("2024-01-02 09:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + 1] * n,
            "low": [close - 1] * n,
            "close": [close] * n,
            "volume": [1000] * n,
        },
        index=idx,
    )


SIM_KWARGS = dict(ema_fast=5, ema_slow=10, rsi_period=5, atr_period=5)


def test_long_trade_hits_target():
    df = flat_ohlcv(10)
    entry_time = df.index[5]
    df.loc[df.index[6], ["high", "low", "close"]] = [105.0, 99.0, 102.0]  # touches target 104

    strat = StubStrategy(entries={entry_time: Signal.LONG}, exits=set())
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert len(trades) == 1
    t = trades[0]
    assert t.side == "LONG"
    assert t.entry_price == 100.0
    assert t.stop_price == 98.0
    assert t.target_price == 104.0
    assert t.exit_reason == "target"
    assert t.exit_price == 104.0
    assert t.r_multiple == 2.0  # target_atr_mult / stop_atr_mult


def test_long_trade_hits_stop():
    df = flat_ohlcv(10)
    entry_time = df.index[5]
    df.loc[df.index[6], ["high", "low", "close"]] = [100.5, 97.0, 98.0]  # touches stop 98

    strat = StubStrategy(entries={entry_time: Signal.LONG}, exits=set())
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == 98.0
    assert t.r_multiple == -1.0  # a stop-out is always exactly -1R by construction


def test_long_trade_exits_on_strategy_signal_at_bar_close():
    df = flat_ohlcv(10)
    entry_time = df.index[5]
    exit_time = df.index[6]
    # Neither stop (98) nor target (104) touched -- pure signal exit.
    df.loc[exit_time, ["high", "low", "close"]] = [101.0, 99.5, 100.5]

    strat = StubStrategy(entries={entry_time: Signal.LONG}, exits={exit_time})
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "signal"
    assert t.exit_price == 100.5
    assert t.exit_time == exit_time
    assert t.r_multiple == (100.5 - 100.0) / 2.0


def test_short_trade_hits_target():
    df = flat_ohlcv(10)
    entry_time = df.index[5]
    # SHORT entry at 100, stop=102, target=96 (stop_mult=1, target_mult=2, ATR=2)
    df.loc[df.index[6], ["high", "low", "close"]] = [100.5, 95.0, 96.0]

    strat = StubStrategy(entries={entry_time: Signal.SHORT}, exits=set())
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert len(trades) == 1
    t = trades[0]
    assert t.side == "SHORT"
    assert t.stop_price == 102.0
    assert t.target_price == 96.0
    assert t.exit_reason == "target"
    assert t.r_multiple == 2.0


def test_no_new_entry_while_position_open():
    df = flat_ohlcv(10)
    entry_time = df.index[5]
    second_signal_time = df.index[6]  # would-be second LONG while already in a position
    df.loc[second_signal_time, ["high", "low", "close"]] = [100.5, 99.5, 100.0]

    strat = StubStrategy(
        entries={entry_time: Signal.LONG, second_signal_time: Signal.LONG}, exits=set()
    )
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    # Position from bar 5 never closes within this short series -> dropped,
    # and the bar-6 signal must have been ignored (position was open).
    assert trades == []


def test_position_still_open_at_end_of_data_is_dropped():
    df = flat_ohlcv(10)
    entry_time = df.index[8]  # opens near the end, never gets a chance to close

    strat = StubStrategy(entries={entry_time: Signal.LONG}, exits=set())
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert trades == []


def test_no_entry_when_strategy_always_flat():
    df = flat_ohlcv(10)
    strat = StubStrategy(entries={}, exits=set())
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)
    assert trades == []
