import numpy as np
import pandas as pd

from tradingbot.data.indicators import add_indicators, atr, ema, rsi, session_vwap


def make_ohlcv() -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 09:30", periods=30, freq="5min", tz="UTC")
    close = pd.Series(np.linspace(100, 130, 30), index=idx)
    high = close + 1
    low = close - 1
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(1000, index=idx)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_ema_fast_leads_slow_in_uptrend():
    df = make_ohlcv()
    fast = ema(df["close"], 5)
    slow = ema(df["close"], 20)
    assert fast.iloc[-1] > slow.iloc[-1]


def test_rsi_bounded_0_100():
    df = make_ohlcv()
    r = rsi(df["close"], 14)
    assert (r >= 0).all() and (r <= 100).all()


def test_rsi_high_in_strong_uptrend():
    df = make_ohlcv()
    r = rsi(df["close"], 14)
    assert r.iloc[-1] > 60


def test_atr_positive():
    df = make_ohlcv()
    a = atr(df, 14)
    assert (a.dropna() > 0).all()


def test_vwap_first_bar_equals_typical_price():
    df = make_ohlcv()
    v = session_vwap(df)
    typical_first = (df["high"].iloc[0] + df["low"].iloc[0] + df["close"].iloc[0]) / 3
    assert v.iloc[0] == typical_first


def test_vwap_within_session_extremes():
    # VWAP is a cumulative average since session start, so on a trending session it can
    # lag behind the latest bar's own high/low -- but it must stay within the session's
    # overall high/low range.
    df = make_ohlcv()
    v = session_vwap(df)
    assert v.min() >= df["low"].min()
    assert v.max() <= df["high"].max()


def test_add_indicators_adds_expected_columns():
    df = make_ohlcv()
    out = add_indicators(df, ema_fast=5, ema_slow=20, rsi_period=14, atr_period=14)
    for col in ["ema_fast", "ema_slow", "rsi", "atr", "vwap"]:
        assert col in out.columns
    assert len(out) == len(df)
