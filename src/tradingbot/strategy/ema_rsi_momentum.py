"""Intraday EMA-crossover momentum strategy, filtered by RSI and session VWAP.

Entry logic (evaluated on each new closed bar):
  LONG  when EMA(fast) crosses above EMA(slow) on this bar, RSI is in the
        [rsi_long_min, rsi_long_max] band (bullish but not overbought/exhausted),
        and price trades above the session VWAP (confirms buyers are in control).
  SHORT is the mirror image, only emitted if shorting is enabled.
  FLAT  otherwise (including when the opposite EMA cross happens, used as an
        exit signal by the engine).

This is deliberately a simple, well-known intraday momentum setup meant as a
solid, testable starting point -- swap in a different Strategy implementation
for anything more sophisticated.
"""
from __future__ import annotations

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class EmaRsiVwapMomentum(Strategy):
    def __init__(
        self,
        ema_fast: int,
        ema_slow: int,
        rsi_period: int,
        rsi_long_min: float,
        rsi_long_max: float,
        rsi_short_min: float,
        rsi_short_max: float,
        atr_period: int,
        allow_shorting: bool = False,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.atr_period = atr_period
        self.allow_shorting = allow_shorting

    @property
    def min_bars(self) -> int:
        return max(self.ema_slow, self.rsi_period, self.atr_period) + 2

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < self.min_bars:
            return Signal.FLAT

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        crossed_up = prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
        crossed_down = prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]

        if (
            crossed_up
            and self.rsi_long_min <= curr["rsi"] <= self.rsi_long_max
            and curr["close"] > curr["vwap"]
        ):
            return Signal.LONG

        if (
            self.allow_shorting
            and crossed_down
            and self.rsi_short_min <= curr["rsi"] <= self.rsi_short_max
            and curr["close"] < curr["vwap"]
        ):
            return Signal.SHORT

        return Signal.FLAT

    def is_exit_signal(self, df: pd.DataFrame, position_is_long: bool) -> bool:
        """EMA cross back the other way is treated as a trend-exhaustion exit,
        independent of the hard stop-loss/take-profit bracket orders."""
        if len(df) < self.min_bars:
            return False
        curr, prev = df.iloc[-1], df.iloc[-2]
        if position_is_long:
            return prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]
        return prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
