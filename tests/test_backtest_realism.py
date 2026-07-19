"""Tests for the simulator's live-mirroring fill model: next-bar-open
entries, gap-aware stop/target fills, slippage and commission costs. The
mechanics of stop/target/exit sequencing are covered in
test_backtest_simulator.py; this file pins down the realism layer that
keeps backtest expectancy honest."""
import pandas as pd

from tradingbot.backtest.simulator import simulate
from tradingbot.strategy.base import Signal, Strategy


class StubStrategy(Strategy):
    def __init__(self, entries, exits=frozenset()):
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


def test_entry_fills_at_next_bar_open_not_signal_close():
    """The classic one-bar optimism: live, a signal is only visible after
    its bar closes and the market order fills in the next bar -- a gap
    against you is *your* fill, not the signal bar's close."""
    df = flat_ohlcv(12)
    signal_time = df.index[5]
    entry_bar = df.index[6]
    # Gap up on the entry bar: open 102 (signal close was 100).
    df.loc[entry_bar, ["open", "high", "low", "close"]] = [102.0, 106.0, 101.5, 105.0]

    strat = StubStrategy(entries={signal_time: Signal.LONG})
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert len(trades) == 1
    t = trades[0]
    assert t.entry_time == entry_bar
    assert t.entry_price == 102.0  # next bar's open, not 100.0
    # Bracket stays anchored to the signal bar's close, as live prices it
    # before the fill: ATR was 2.0 at the signal bar -> stop 98, target 104.
    assert t.stop_price == 98.0
    assert t.target_price == 104.0
    # Entry bar's high (106) already touched the target -> favorable-gap
    # aware limit fill at max(target, open) = 104.
    assert t.exit_reason == "target"
    assert t.exit_price == 104.0


def test_gap_through_stop_fills_at_the_open():
    """A bar that OPENS below the stop fills there -- pretending the stop
    price held through a gap flatters the results exactly when it hurts."""
    df = flat_ohlcv(12)
    signal_time = df.index[5]
    df.loc[df.index[7], ["open", "high", "low", "close"]] = [95.0, 96.0, 94.0, 95.5]

    strat = StubStrategy(entries={signal_time: Signal.LONG})
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)

    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == 95.0  # the open, not the 98.0 stop price
    assert t.r_multiple < -1.0  # a gap loses more than 1R -- the honest number


def test_slippage_applied_adversely_to_market_fills():
    df = flat_ohlcv(12)
    signal_time = df.index[5]
    df.loc[df.index[7], ["high", "low", "close"]] = [105.0, 99.5, 102.0]  # target 104 touched

    strat = StubStrategy(entries={signal_time: Signal.LONG})
    trades = simulate(
        df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, slippage_bps=10.0, **SIM_KWARGS
    )
    t = trades[0]
    assert t.entry_price == 100.0 * 1.001  # paid up on entry
    assert t.exit_price == 104.0  # limit target: no adverse slippage


def test_commissions_reduce_r_multiple():
    df = flat_ohlcv(12)
    signal_time = df.index[5]
    df.loc[df.index[7], ["high", "low", "close"]] = [105.0, 99.5, 102.0]

    strat = StubStrategy(entries={signal_time: Signal.LONG})
    gross = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)
    net = simulate(
        df,
        strat,
        "TEST",
        stop_atr_mult=1.0,
        target_atr_mult=2.0,
        commission_per_share=0.05,
        **SIM_KWARGS,
    )
    assert gross[0].r_multiple == 2.0
    # (4.00 profit - 0.10 round-trip commission) / 2.00 risk
    assert abs(net[0].r_multiple - 1.95) < 1e-9


def test_signal_on_last_bar_never_fills():
    df = flat_ohlcv(8)
    strat = StubStrategy(entries={df.index[-1]: Signal.LONG})
    trades = simulate(df, strat, "TEST", stop_atr_mult=1.0, target_atr_mult=2.0, **SIM_KWARGS)
    assert trades == []
