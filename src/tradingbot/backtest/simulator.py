"""Bar-by-bar strategy backtester. Pure pandas/Strategy logic, no IB
dependency -> fully unit testable with synthetic OHLCV data.

Mirrors the live engine's trade lifecycle as closely as a bar-level (not
tick-level) simulation allows: one position per symbol at a time, entry at
the signal bar's close, ATR-based stop/target bracket, exit on whichever of
stop/target/strategy-exit-signal comes first. The one simplification bar
data forces: if a single bar's high/low range touches both the stop and the
target, we can't know which was hit first intrabar, so the stop is assumed
(the conservative assumption)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from tradingbot.data.indicators import add_indicators
from tradingbot.strategy.base import Signal, Strategy

# Bounded trailing window passed to strategy.generate_signal/is_exit_signal
# on each bar, instead of the full history-so-far -- keeps the simulation
# O(n) instead of O(n^2) for long backtests, while comfortably covering any
# lookback either built-in strategy actually uses.
_LOOKBACK_WINDOW = 200


@dataclass
class Trade:
    symbol: str
    side: str  # "LONG" | "SHORT"
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    stop_price: float
    target_price: float
    exit_reason: str  # "stop" | "target" | "signal"

    @property
    def r_multiple(self) -> float:
        """P&L expressed in units of initial risk (entry-to-stop distance).
        Sign already accounts for LONG vs SHORT."""
        risk = abs(self.entry_price - self.stop_price)
        if risk <= 0:
            return 0.0
        raw = self.exit_price - self.entry_price
        if self.side == "SHORT":
            raw = -raw
        return raw / risk


@dataclass
class _OpenPosition:
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float


def simulate(
    df: pd.DataFrame,
    strategy: Strategy,
    symbol: str,
    ema_fast: int,
    ema_slow: int,
    rsi_period: int,
    atr_period: int,
    stop_atr_mult: float,
    target_atr_mult: float,
    vwap_tz: str = "US/Eastern",
    trend_ema_period: int = 50,
) -> list[Trade]:
    """Runs `strategy` bar-by-bar over historical OHLCV `df` (must have a
    sorted, tz-aware DatetimeIndex and open/high/low/close/volume columns).
    Returns only fully closed round-trip trades -- a position still open at
    the end of the data is dropped rather than force-closed, since it never
    completed and would bias the stats."""
    enriched = add_indicators(
        df, ema_fast, ema_slow, rsi_period, atr_period, vwap_tz, trend_ema_period
    )
    return simulate_enriched(enriched, strategy, symbol, stop_atr_mult, target_atr_mult)


def simulate_enriched(
    enriched: pd.DataFrame,
    strategy: Strategy,
    symbol: str,
    stop_atr_mult: float,
    target_atr_mult: float,
) -> list[Trade]:
    """Same trade-loop as `simulate()`, but takes an already-indicator-enriched
    frame (see `add_indicators`) instead of computing it internally. Lets a
    caller compute indicators once over a symbol's full history and then
    re-run the loop over different slices/strategy variants of that same
    frame -- e.g. a train/validation split, or comparing strategy variants
    that only differ in entry logic -- without recomputing EMA/RSI/ATR/VWAP
    (which need the full history for an accurate warm-up) once per slice."""
    min_bars = strategy.min_bars

    trades: list[Trade] = []
    position: _OpenPosition | None = None

    for i in range(min_bars, len(enriched)):
        window = enriched.iloc[max(0, i - _LOOKBACK_WINDOW + 1) : i + 1]
        curr = enriched.iloc[i]

        if position is not None:
            is_long = position.side == "LONG"
            hit_stop = (
                curr["low"] <= position.stop_price
                if is_long
                else curr["high"] >= position.stop_price
            )
            hit_target = (
                curr["high"] >= position.target_price
                if is_long
                else curr["low"] <= position.target_price
            )
            exit_signal = strategy.is_exit_signal(window, position_is_long=is_long)

            if hit_stop or hit_target or exit_signal:
                if hit_stop:
                    exit_price, reason = position.stop_price, "stop"
                elif hit_target:
                    exit_price, reason = position.target_price, "target"
                else:
                    exit_price, reason = float(curr["close"]), "signal"
                trades.append(
                    Trade(
                        symbol=symbol,
                        side=position.side,
                        entry_time=position.entry_time,
                        entry_price=position.entry_price,
                        exit_time=curr.name,
                        exit_price=exit_price,
                        stop_price=position.stop_price,
                        target_price=position.target_price,
                        exit_reason=reason,
                    )
                )
                position = None
            continue

        signal = strategy.generate_signal(window)
        if signal == Signal.FLAT:
            continue

        entry_price = float(curr["close"])
        atr_value = float(curr["atr"])
        if atr_value <= 0:
            continue
        stop_dist = atr_value * stop_atr_mult
        target_dist = atr_value * target_atr_mult

        if signal == Signal.LONG:
            stop_price = entry_price - stop_dist
            target_price = entry_price + target_dist
        else:
            stop_price = entry_price + stop_dist
            target_price = entry_price - target_dist

        position = _OpenPosition(
            side=signal.value,
            entry_time=curr.name,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
        )

    return trades
