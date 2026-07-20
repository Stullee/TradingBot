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


def session_vwap(df: pd.DataFrame, tz: str = "US/Eastern") -> pd.Series:
    """Volume-weighted average price, resetting at the start of each trading
    session (calendar date in `tz`). `tz` must be the traded market's own
    timezone, not hardcoded to US/Eastern -- a market whose session crosses
    US/Eastern midnight (e.g. Hong Kong's 9:30-16:00 HKT session spans
    ~20:30-03:00 US/Eastern) would otherwise get its VWAP reset mid-session."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    day = df.index.tz_convert(tz).date if df.index.tz is not None else df.index.date
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
    vwap_tz: str = "US/Eastern",
    trend_ema_period: int = 50,
) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["atr"] = atr(out, atr_period)
    out["vwap"] = session_vwap(out, vwap_tz)
    # Which trading session (calendar date in the market's own timezone)
    # each bar belongs to -- the same grouping VWAP resets on. Lets
    # strategies confine themselves to the current session: an intraday
    # trendline fitted across the overnight/weekend gap is geometrically
    # meaningless (confirmed live: VOW3's Monday-morning uptrend was
    # invisible for hours because the fit window still contained Friday's
    # bars plus the weekend gap-down).
    out["session_date"] = (
        out.index.tz_convert(vwap_tz).date if out.index.tz is not None else out.index.date
    )
    # A longer-horizon trend reference, independent of ema_fast/ema_slow
    # (which are tuned for short-term crossover signals). Used by
    # VwapMeanReversion as a trend filter -- see that class's docstring.
    # trend_ema_period<=0 means the filter is disabled and the column is
    # never read, but ewm(span=0) itself raises, so skip computing it.
    if trend_ema_period > 0:
        out["trend_ema"] = ema(out["close"], trend_ema_period)
    return out
