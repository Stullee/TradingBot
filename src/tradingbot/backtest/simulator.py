"""Bar-by-bar strategy backtester. Pure pandas/Strategy logic, no IB
dependency -> fully unit testable with synthetic OHLCV data.

Mirrors the live engine's trade lifecycle as closely as a bar-level (not
tick-level) simulation allows, including its *timing*: live, a signal is
only visible after its bar closes and the market order fills somewhere in
the next bar -- so entries here fill at the **next bar's open**, not the
signal bar's close (the classic one-bar optimism that can flip a marginal
strategy's expectancy sign). Stop/target levels are still anchored to the
signal bar's close +/- ATR, exactly as the live bracket is priced before
the fill.

Cost model (both default to 0 for pure-logic tests; the runner passes the
configured values):
  - slippage_bps: applied adversely to every market-order fill -- entries,
    stop exits, strategy-signal exits. The take-profit is a limit order and
    fills at its price or better, so no slippage there.
  - commission_per_share: charged per unit per side (x2 per round trip),
    reflected in each Trade's cost_per_share and therefore its R.

Gap handling: if a bar *opens* beyond the stop, the stop fills at that open
(you can't fill better than where the market reopened); a favorable gap
past the target fills at the open too (limit fills at price or better). If
a single bar's high/low range touches both levels intrabar, the stop is
assumed (the conservative choice -- we can't know which was hit first)."""
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
    cost_per_share: float = 0.0  # round-trip commissions per unit

    @property
    def r_multiple(self) -> float:
        """Net P&L (after per-share costs) in units of initial risk
        (entry-to-stop distance). Sign already accounts for LONG vs SHORT."""
        risk = abs(self.entry_price - self.stop_price)
        if risk <= 0:
            return 0.0
        raw = self.exit_price - self.entry_price
        if self.side == "SHORT":
            raw = -raw
        return (raw - self.cost_per_share) / risk


@dataclass
class _OpenPosition:
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float


@dataclass
class _PendingEntry:
    """Signal seen at a bar's close; fills at the next bar's open."""

    side: str
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
    commission_per_share: float = 0.0,
    slippage_bps: float = 0.0,
) -> list[Trade]:
    """Runs `strategy` bar-by-bar over historical OHLCV `df` (must have a
    sorted, tz-aware DatetimeIndex and open/high/low/close/volume columns).
    Returns only fully closed round-trip trades -- a position still open at
    the end of the data is dropped rather than force-closed, since it never
    completed and would bias the stats."""
    enriched = add_indicators(
        df, ema_fast, ema_slow, rsi_period, atr_period, vwap_tz, trend_ema_period
    )
    return simulate_enriched(
        enriched,
        strategy,
        symbol,
        stop_atr_mult,
        target_atr_mult,
        commission_per_share=commission_per_share,
        slippage_bps=slippage_bps,
    )


def simulate_enriched(
    enriched: pd.DataFrame,
    strategy: Strategy,
    symbol: str,
    stop_atr_mult: float,
    target_atr_mult: float,
    commission_per_share: float = 0.0,
    slippage_bps: float = 0.0,
) -> list[Trade]:
    """Same trade-loop as `simulate()`, but takes an already-indicator-enriched
    frame (see `add_indicators`) instead of computing it internally. Lets a
    caller compute indicators once over a symbol's full history and then
    re-run the loop over different slices/strategy variants of that same
    frame -- e.g. a train/validation split, or comparing strategy variants
    that only differ in entry logic -- without recomputing EMA/RSI/ATR/VWAP
    (which need the full history for an accurate warm-up) once per slice."""
    min_bars = strategy.min_bars
    slip = slippage_bps / 10_000.0
    cost_per_share = commission_per_share * 2

    trades: list[Trade] = []
    position: _OpenPosition | None = None
    pending: _PendingEntry | None = None

    def close_trade(exit_time, exit_price: float, reason: str) -> None:
        nonlocal position
        trades.append(
            Trade(
                symbol=symbol,
                side=position.side,
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                exit_time=exit_time,
                exit_price=exit_price,
                stop_price=position.stop_price,
                target_price=position.target_price,
                exit_reason=reason,
                cost_per_share=cost_per_share,
            )
        )
        position = None

    def check_exits(curr) -> None:
        """Stop/target off the bar's range (gap-aware), then the strategy's
        own exit signal at the bar close. Stop wins ties (conservative)."""
        is_long = position.side == "LONG"
        open_px = float(curr["open"])
        if is_long:
            hit_stop = curr["low"] <= position.stop_price
            hit_target = curr["high"] >= position.target_price
            stop_fill = min(position.stop_price, open_px) * (1 - slip)
            target_fill = max(position.target_price, open_px)
        else:
            hit_stop = curr["high"] >= position.stop_price
            hit_target = curr["low"] <= position.target_price
            stop_fill = max(position.stop_price, open_px) * (1 + slip)
            target_fill = min(position.target_price, open_px)

        if hit_stop:
            close_trade(curr.name, stop_fill, "stop")
        elif hit_target:
            close_trade(curr.name, target_fill, "target")

    def handle_open_position(curr, window) -> None:
        """Bracket exits off the bar's range, then the strategy's own exit
        read at the bar close (a market order, so slippage applies)."""
        check_exits(curr)
        if position is not None and strategy.is_exit_signal(
            window, position_is_long=position.side == "LONG"
        ):
            close_px = float(curr["close"])
            exit_px = close_px * (1 - slip) if position.side == "LONG" else close_px * (1 + slip)
            close_trade(curr.name, exit_px, "signal")

    for i in range(min_bars, len(enriched)):
        window = enriched.iloc[max(0, i - _LOOKBACK_WINDOW + 1) : i + 1]
        curr = enriched.iloc[i]

        if pending is not None:
            # Fill last bar's signal at this bar's open, adverse slippage.
            open_px = float(curr["open"])
            entry_px = open_px * (1 + slip) if pending.side == "LONG" else open_px * (1 - slip)
            position = _OpenPosition(
                side=pending.side,
                entry_time=curr.name,
                entry_price=entry_px,
                stop_price=pending.stop_price,
                target_price=pending.target_price,
            )
            pending = None
            # The bracket (and the strategy's exit read at this bar's close)
            # is live from the moment of the fill.
            handle_open_position(curr, window)
            continue

        if position is not None:
            handle_open_position(curr, window)
            continue

        signal = strategy.generate_signal(window)
        if signal == Signal.FLAT:
            continue

        # Bracket priced off the signal bar's close +/- ATR, exactly as the
        # live engine computes it before the (next-bar) fill.
        signal_close = float(curr["close"])
        atr_value = float(curr["atr"])
        if atr_value <= 0:
            continue
        stop_dist = atr_value * stop_atr_mult
        target_dist = atr_value * target_atr_mult

        if signal == Signal.LONG:
            pending = _PendingEntry(
                side="LONG",
                stop_price=signal_close - stop_dist,
                target_price=signal_close + target_dist,
            )
        else:
            pending = _PendingEntry(
                side="SHORT",
                stop_price=signal_close + stop_dist,
                target_price=signal_close - target_dist,
            )

    return trades
