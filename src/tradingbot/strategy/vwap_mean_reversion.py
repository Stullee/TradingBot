"""VWAP mean-reversion strategy: fades short-term price extremes back toward
the session VWAP, instead of waiting for a trend to establish itself.

Entry logic (evaluated on each new closed bar):
  LONG  when price has drifted meaningfully below VWAP (distance scaled by
        ATR, so it adapts to each symbol's own volatility), RSI confirms an
        oversold extreme, and the current bar is the first sign of a
        reversal (closed higher than the prior bar).
  SHORT is the mirror image, only emitted if shorting is enabled.
  FLAT  otherwise.

VWAP deviations of this kind occur many times per session (not just at
trend turns), so this fires far more often than a trend-following EMA
crossover -- see tradingbot.strategy.ema_rsi_momentum for that alternative.
Trade-off: smaller average win per trade, and pure countertrend
mean-reversion can fight a strong trend on a genuinely trending day (buying
a dip in a downtrend, or shorting an extension in an uptrend) -- exactly
the setups where this strategy loses most, confirmed both in backtests
(persistently trending names like TSLA/MSFT were the worst performers) and
live (shorting WMT during a sustained intraday uptrend).

Trend filter (on by default, `trend_ema_period`): entries are only allowed
in the direction that doesn't fight a longer-horizon trend -- LONG (buy the
dip) is blocked while price is below the trend EMA (a downtrend), SHORT
(sell the rip) is blocked while price is above it (an uptrend). This keeps
the "buy dips / sell rips" mean-reversion timing but only *with* the
prevailing trend, not against it -- turns the strategy from pure
countertrend into trend-following-with-pullback-entries. Set
`trend_ema_period=0` to disable and get the old unconditional behavior.
"""
from __future__ import annotations

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class VwapMeanReversion(Strategy):
    def __init__(
        self,
        rsi_period: int,
        rsi_oversold: float,
        rsi_overbought: float,
        atr_period: int,
        vwap_dist_atr_mult: float,
        trend_ema_period: int = 50,
        allow_shorting: bool = False,
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.vwap_dist_atr_mult = vwap_dist_atr_mult
        self.trend_ema_period = trend_ema_period
        self.allow_shorting = allow_shorting

    @property
    def min_bars(self) -> int:
        periods = [self.rsi_period, self.atr_period]
        if self.trend_ema_period > 0:
            periods.append(self.trend_ema_period)
        return max(periods) + 2

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < self.min_bars:
            return Signal.FLAT

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        dist = curr["close"] - curr["vwap"]
        threshold = curr["atr"] * self.vwap_dist_atr_mult

        # Trend filter disabled (trend_ema_period=0) -> never blocks either
        # direction, reproducing the old unconditional countertrend behavior.
        blocked_for_long = self.trend_ema_period > 0 and curr["close"] < curr["trend_ema"]
        blocked_for_short = self.trend_ema_period > 0 and curr["close"] > curr["trend_ema"]

        if (
            not blocked_for_long
            and dist <= -threshold
            and curr["rsi"] <= self.rsi_oversold
            and curr["close"] > prev["close"]
        ):
            return Signal.LONG

        if (
            self.allow_shorting
            and not blocked_for_short
            and dist >= threshold
            and curr["rsi"] >= self.rsi_overbought
            and curr["close"] < prev["close"]
        ):
            return Signal.SHORT

        return Signal.FLAT

    def is_exit_signal(self, df: pd.DataFrame, position_is_long: bool) -> bool:
        """Exits as soon as price reverts back to (or past) VWAP -- the mean
        reversion this strategy trades has played out -- independent of the
        hard stop/target bracket orders, which stay in place as a backstop
        for whichever comes first."""
        if len(df) < self.min_bars:
            return False
        curr = df.iloc[-1]
        if position_is_long:
            return curr["close"] >= curr["vwap"]
        return curr["close"] <= curr["vwap"]
