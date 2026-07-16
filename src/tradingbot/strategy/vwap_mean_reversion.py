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
Trade-off: smaller average win per trade, and it can fight a strong trend
on a genuinely trending day.
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
        allow_shorting: bool = False,
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.vwap_dist_atr_mult = vwap_dist_atr_mult
        self.allow_shorting = allow_shorting

    @property
    def min_bars(self) -> int:
        return max(self.rsi_period, self.atr_period) + 2

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < self.min_bars:
            return Signal.FLAT

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        dist = curr["close"] - curr["vwap"]
        threshold = curr["atr"] * self.vwap_dist_atr_mult

        if (
            dist <= -threshold
            and curr["rsi"] <= self.rsi_oversold
            and curr["close"] > prev["close"]
        ):
            return Signal.LONG

        if (
            self.allow_shorting
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
