import pandas as pd

from tradingbot.strategy.base import Signal
from tradingbot.strategy.ema_rsi_momentum import EmaRsiVwapMomentum


def make_strategy(allow_shorting: bool = False) -> EmaRsiVwapMomentum:
    return EmaRsiVwapMomentum(
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        rsi_long_min=50,
        rsi_long_max=70,
        rsi_short_min=30,
        rsi_short_max=50,
        atr_period=14,
        allow_shorting=allow_shorting,
    )


def base_df(n: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 09:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "close": [100.0] * n,
            "ema_fast": [99.0] * n,
            "ema_slow": [100.0] * n,
            "rsi": [55.0] * n,
            "vwap": [99.5] * n,
        },
        index=idx,
    )


def _set_bullish_cross(df: pd.DataFrame) -> None:
    df.iloc[-2, df.columns.get_loc("ema_fast")] = 99.0
    df.iloc[-2, df.columns.get_loc("ema_slow")] = 100.0
    df.iloc[-1, df.columns.get_loc("ema_fast")] = 101.0
    df.iloc[-1, df.columns.get_loc("ema_slow")] = 100.0


def _set_bearish_cross(df: pd.DataFrame) -> None:
    df.iloc[-2, df.columns.get_loc("ema_fast")] = 101.0
    df.iloc[-2, df.columns.get_loc("ema_slow")] = 100.0
    df.iloc[-1, df.columns.get_loc("ema_fast")] = 99.0
    df.iloc[-1, df.columns.get_loc("ema_slow")] = 100.0


def test_long_signal_on_fresh_bullish_cross_with_confirmations():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    _set_bullish_cross(df)
    df.iloc[-1, df.columns.get_loc("rsi")] = 60.0
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.generate_signal(df) == Signal.LONG


def test_no_signal_without_enough_bars():
    strat = make_strategy()
    df = base_df(strat.min_bars - 1)
    assert strat.generate_signal(df) == Signal.FLAT


def test_no_long_signal_if_rsi_out_of_band():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    _set_bullish_cross(df)
    df.iloc[-1, df.columns.get_loc("rsi")] = 90.0  # overbought, out of band
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.generate_signal(df) == Signal.FLAT


def test_no_long_signal_if_price_below_vwap():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    _set_bullish_cross(df)
    df.iloc[-1, df.columns.get_loc("rsi")] = 60.0
    df.iloc[-1, df.columns.get_loc("close")] = 98.0
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.generate_signal(df) == Signal.FLAT


def test_short_disabled_by_default():
    strat = make_strategy(allow_shorting=False)
    df = base_df(strat.min_bars + 1)
    _set_bearish_cross(df)
    df.iloc[-1, df.columns.get_loc("rsi")] = 40.0
    df.iloc[-1, df.columns.get_loc("close")] = 98.0
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.generate_signal(df) == Signal.FLAT


def test_short_signal_when_enabled():
    strat = make_strategy(allow_shorting=True)
    df = base_df(strat.min_bars + 1)
    _set_bearish_cross(df)
    df.iloc[-1, df.columns.get_loc("rsi")] = 40.0
    df.iloc[-1, df.columns.get_loc("close")] = 98.0
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.generate_signal(df) == Signal.SHORT


def test_is_exit_signal_for_long_on_bearish_cross():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    _set_bearish_cross(df)
    assert strat.is_exit_signal(df, position_is_long=True)
    assert not strat.is_exit_signal(df, position_is_long=False)


def test_is_exit_signal_for_short_on_bullish_cross():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    _set_bullish_cross(df)
    assert strat.is_exit_signal(df, position_is_long=False)
    assert not strat.is_exit_signal(df, position_is_long=True)
