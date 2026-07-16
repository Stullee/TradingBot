"""Simulated ("shadow") trades opened from news sentiment signals. Tracked
against real market prices using the same ATR stop/target rules as the real
strategy, but never sent to the broker -- this exists purely to evaluate
whether the news signal would have been profitable, before ever wiring it to
real order execution.

Price tracking piggybacks on the bot's existing 5-min bar stream (bar-close
granularity, not tick-level), the same simplification the rest of the bot
already makes for its real bracket orders' conceptual stop/target levels."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ShadowTrade:
    symbol: str
    direction: str  # "LONG" | "SHORT"
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: str
    headline: str
    confidence: float
    rationale: str
    status: str = "OPEN"  # OPEN | WIN | LOSS | TIMEOUT
    closed_at: str | None = None
    exit_price: float | None = None
    r_multiple: float | None = None
    last_price: float | None = None  # most recent price seen while OPEN, for live P&L display

    def unrealized_r_multiple(self) -> float | None:
        """R-multiple as of last_price -- "are we winning or losing right
        now" for a still-open trade, same convention as the closed-trade
        r_multiple (+1R = target-sized win, -1R = full stop-out)."""
        if self.last_price is None:
            return None
        risk_per_share = abs(self.entry_price - self.stop_price)
        if risk_per_share <= 0:
            return None
        pnl_per_share = (
            self.last_price - self.entry_price
            if self.direction == "LONG"
            else self.entry_price - self.last_price
        )
        return round(pnl_per_share / risk_per_share, 3)


class ShadowTradeTracker:
    """At most one open shadow trade per symbol at a time."""

    def __init__(self, log_path: Path, max_hold_min: int):
        self.log_path = log_path
        self.max_hold_min = max_hold_min
        self.open_trades: dict[str, ShadowTrade] = {}

    def has_open(self, symbol: str) -> bool:
        return symbol in self.open_trades

    def open(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_price: float,
        target_price: float,
        headline: str,
        confidence: float,
        rationale: str,
    ) -> None:
        trade = ShadowTrade(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            opened_at=datetime.now(timezone.utc).isoformat(),
            headline=headline,
            confidence=confidence,
            rationale=rationale,
        )
        self.open_trades[symbol] = trade
        log.info(
            "[SHADOW] Opened %s %s @ %.2f (stop=%.2f, target=%.2f, confidence=%.2f) on news: %s",
            direction,
            symbol,
            entry_price,
            stop_price,
            target_price,
            confidence,
            headline,
        )

    def update(self, symbol: str, current_price: float, now: datetime | None = None) -> None:
        trade = self.open_trades.get(symbol)
        if trade is None:
            return
        now = now or datetime.now(timezone.utc)
        trade.last_price = current_price

        if trade.direction == "LONG":
            hit_target = current_price >= trade.target_price
            hit_stop = current_price <= trade.stop_price
        else:
            hit_target = current_price <= trade.target_price
            hit_stop = current_price >= trade.stop_price

        opened_at = datetime.fromisoformat(trade.opened_at)
        timed_out = (now - opened_at).total_seconds() >= self.max_hold_min * 60

        if hit_target:
            self._close(trade, "WIN", current_price, now)
        elif hit_stop:
            self._close(trade, "LOSS", current_price, now)
        elif timed_out:
            self._close(trade, "TIMEOUT", current_price, now)

    def _close(self, trade: ShadowTrade, status: str, exit_price: float, now: datetime) -> None:
        risk_per_share = abs(trade.entry_price - trade.stop_price)
        if trade.direction == "LONG":
            pnl_per_share = exit_price - trade.entry_price
        else:
            pnl_per_share = trade.entry_price - exit_price
        r_multiple = pnl_per_share / risk_per_share if risk_per_share > 0 else 0.0

        trade.status = status
        trade.exit_price = exit_price
        trade.closed_at = now.isoformat()
        trade.r_multiple = round(r_multiple, 3)

        log.info(
            "[SHADOW] Closed %s %s: %s @ %.2f (R=%.2f)",
            trade.direction,
            trade.symbol,
            status,
            exit_price,
            trade.r_multiple,
        )
        self._append_to_log(trade)
        del self.open_trades[trade.symbol]

    def _append_to_log(self, trade: ShadowTrade) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
