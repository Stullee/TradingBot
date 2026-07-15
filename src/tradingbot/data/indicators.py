"""Pure pandas indicator calculations used by strategies. No IB dependency -> easy to unit test."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.mask(avg_loss == 0, 100.0)  # no losses in the lookback -> RSI 100
    result = result.mask((avg_gain == 0) & (avg_loss == 0), 50.0)  # no movement at all
    return result.fillna(50.0)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, resetting at the start of each trading session (date)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    day = df.index.tz_convert("US/Eastern").date if df.index.tz is not None else df.index.date
    grouped_pv = pd.Series(pv.values, index=day).groupby(level=0).cumsum()
    grouped_vol = pd.Series(df["volume"].values, index=day).groupby(level=0).cumsum()
    vwap = (grouped_pv / grouped_vol.replace(0, np.nan)).values
    return pd.Series(vwap, index=df.index)


def add_indicators(
    df: pd.DataFrame,
    ema_fast: int,
    ema_slow: int,
    rsi_period: int,
    atr_period: int,
) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["atr"] = atr(out, atr_period)
    out["vwap"] = session_vwap(out)
    return out
