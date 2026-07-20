"""Trendline breakout momentum strategy: fits a least-squares regression
line over a rolling window of closes and enters on a genuine break above
(or, if shorting, below) that established trendline -- chasing an already-
confirmed rise rather than fading it back to a mean, unlike
vwap_mean_reversion.

Entry logic (evaluated on each new closed bar):
  LONG  when the rolling trendline's slope is positive and its fit is tight
        (R-squared above min_r_squared -- a real, consistent rise, not a
        noisy zigzag that happens to net upward over the window), and this
        bar's close crosses from at-or-below the trendline's current
        projected value to above it. The crossing requirement (not just
        "currently above") means this fires once, at the actual breakout
        moment, not on every bar spent already above the line.
  SHORT is the mirror image, only emitted if shorting is enabled.
  FLAT  otherwise, including once price falls back below its own trendline
        (used as the exit signal -- momentum failing is the exit condition,
        the same way a mean-reversion trade exits on reaching its target).

Deliberately fires more often than vwap_mean_reversion's trend filter or
ema_rsi_momentum's crossover -- a tight regression fit is a real, recurring
condition, not a rare setup. That's also exactly why it needs validating
before being trusted with capital: entering more often only pays off if the
underlying edge is real, and this hasn't been proven live yet -- shadow-test
it before promoting it.

Session-scoped: the fit only ever uses bars from the *current* trading
session (the `session_date` column added by data.indicators). An intraday
"trendline" spanning the overnight/weekend gap is geometrically
meaningless -- confirmed live (VOW3, Monday 2026-07-20): a weekend
gap-down followed by a clean morning uptrend read as "no valid trend" for
hours because Friday's bars dominated the window. The cost of the fix is
honest patience: no signals until the session has trend_window+1 closed
bars (~the first 100 minutes at defaults) -- during which the dashboard
shows the session bar count filling up instead of a gap-polluted fit.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class TrendlineBreakout(Strategy):
    def __init__(
        self,
        trend_window: int = 20,
        min_r_squared: float = 0.7,
        atr_period: int = 14,
        allow_shorting: bool = False,
    ):
        self.trend_window = trend_window
        self.min_r_squared = min_r_squared
        self.atr_period = atr_period
        self.allow_shorting = allow_shorting

    @property
    def min_bars(self) -> int:
        return max(self.trend_window, self.atr_period) + 2

    def _fit(self, closes: pd.Series) -> tuple[float, float, float]:
        """OLS fit of the trailing trend_window closes against bar index.
        Returns (slope, this-window's-last-point projected value, r_squared)."""
        window = closes.iloc[-self.trend_window :]
        x = np.arange(len(window))
        slope, intercept = np.polyfit(x, window.to_numpy(), 1)
        fitted = slope * x + intercept
        residuals = window.to_numpy() - fitted
        ss_res = float(np.sum(residuals**2))
        ss_tot = float(np.sum((window.to_numpy() - window.mean()) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        projected = float(fitted[-1])
        return float(slope), projected, r_squared

    def _session_closes(self, df: pd.DataFrame) -> pd.Series:
        """Closes belonging to the newest bar's own trading session. Frames
        without session info (plain test fixtures) fall back to all closes."""
        if "session_date" in df.columns:
            current = df["session_date"].iloc[-1]
            return df.loc[df["session_date"] == current, "close"]
        return df["close"]

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < self.min_bars:
            return Signal.FLAT

        closes = self._session_closes(df)
        if len(closes) < self.trend_window + 1:
            return Signal.FLAT  # session too young for a same-session fit
        curr_slope, curr_line, curr_r2 = self._fit(closes)
        _, prev_line, _ = self._fit(closes.iloc[:-1])

        curr_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])

        # A close that genuinely lies on the fitted line (the boundary case
        # "at or below" is meant to include) can still land a hair on either
        # side of it after floating-point rounding in the regression itself
        # -- a tolerance scaled to the line's own magnitude keeps that from
        # being misread as "already crossed" or "hasn't crossed yet".
        tol = abs(prev_line) * 1e-9 + 1e-9
        crossed_above = prev_close <= prev_line + tol and curr_close > curr_line
        crossed_below = prev_close >= prev_line - tol and curr_close < curr_line

        if crossed_above and curr_slope > 0 and curr_r2 >= self.min_r_squared:
            return Signal.LONG
        if (
            self.allow_shorting
            and crossed_below
            and curr_slope < 0
            and curr_r2 >= self.min_r_squared
        ):
            return Signal.SHORT
        return Signal.FLAT

    def is_exit_signal(self, df: pd.DataFrame, position_is_long: bool) -> bool:
        """Momentum failing -- price falling back through its own trendline
        -- is the exit condition, independent of the hard stop-loss/
        take-profit bracket orders. With too few same-session bars for a
        fit, defers to the bracket rather than judging against a gap."""
        if len(df) < self.min_bars:
            return False
        closes = self._session_closes(df)
        if len(closes) < self.trend_window:
            return False
        _, line, _ = self._fit(closes)
        curr_close = float(closes.iloc[-1])
        if position_is_long:
            return curr_close < line
        return curr_close > line

    def diagnostics(self, df: pd.DataFrame) -> dict:
        """Not used by generate_signal/is_exit_signal -- a sanity-check
        window into what the fit currently looks like for this symbol
        (picked up by the live dashboard's per-symbol table, see engine.py),
        since slope/fit-quality/distance-from-line aren't otherwise visible
        anywhere while this strategy is live. Optional, duck-typed: no other
        Strategy needs to implement this, engine.py only reads it if present."""
        if len(df) < self.min_bars:
            return {}
        closes = self._session_closes(df)
        if len(closes) < self.trend_window + 1:
            # Session too young for a same-session fit -- surface how far
            # along the warmup is instead of a gap-polluted fit.
            return {"trend_session_bars": int(len(closes))}
        slope, line, r2 = self._fit(closes)
        close = float(closes.iloc[-1])
        return {
            "trend_slope": slope,
            "trend_r_squared": r2,
            "trend_line_value": line,
            "trend_distance_pct": (close - line) / line * 100 if line else None,
            "trend_session_bars": int(len(closes)),
        }
