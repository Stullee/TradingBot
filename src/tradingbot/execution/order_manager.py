"""Order placement: bracket entries (market parent + stop-loss + take-profit) and
end-of-day flatten-all. Every entry always carries a protective stop -- no naked
positions are ever opened, and ensure_protective_stop() re-attaches one if a
position is ever found without it (e.g. expired DAY children after a holiday).

Prices are rounded to the contract's own minimum tick, not a blanket 2
decimals -- exchanges with banded tick sizes (e.g. Hong Kong: a HK$380 stock
ticks in 0.2) reject orders at off-tick prices, and a rejected bracket child
is exactly the naked-position situation the bracket exists to prevent."""
from __future__ import annotations

import logging

from ib_async import IB, Contract, LimitOrder, MarketOrder, StopOrder, Trade

log = logging.getLogger(__name__)


def round_to_tick(price: float, min_tick: float) -> float:
    """Nearest multiple of `min_tick`, cleaned of binary-float dust so the
    wire value is exact (IB rejects e.g. 380.20000000000003)."""
    if min_tick <= 0:
        min_tick = 0.01
    steps = round(price / min_tick)
    text = f"{min_tick:.10f}".rstrip("0")
    decimals = len(text.split(".")[1]) if "." in text else 0
    return round(steps * min_tick, decimals)


class OrderManager:
    def __init__(self, ib: IB):
        self.ib = ib
        # Entry (bracket parent) trades still in flight, keyed by conId --
        # lets the engine treat a placed-but-not-yet-filled entry as an open
        # position slot. Without this, every symbol signaling in the same
        # tick sees the same stale ib.positions() count and the
        # max-concurrent-positions cap can be blown straight through on a
        # broad market move (exactly when correlated over-entry hurts most).
        self._entry_trades: dict[int, Trade] = {}

    def _prune_entries(self) -> None:
        for con_id in [c for c, t in self._entry_trades.items() if t.isDone()]:
            del self._entry_trades[con_id]

    def has_pending_entry(self, con_id: int) -> bool:
        """True while an entry parent order for this contract is neither
        filled nor cancelled -- used to block duplicate entries (e.g. a
        halted symbol whose market order is still pending a bar later)."""
        self._prune_entries()
        return con_id in self._entry_trades

    def pending_entry_conids(self) -> set[int]:
        self._prune_entries()
        return set(self._entry_trades)

    def place_bracket(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        stop_price: float,
        target_price: float,
        outside_rth: bool = False,
        min_tick: float = 0.01,
    ) -> Trade:
        """action: 'BUY' to go long, 'SELL' to go short. Returns the parent Trade.
        outside_rth must be True for the order to be eligible to trigger/fill
        outside a market's regular trading hours (e.g. US pre/post-market)."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        exit_action = "SELL" if action == "BUY" else "BUY"

        # tif is set explicitly (rather than left blank) because IB silently
        # cancels API orders whose parameters get auto-corrected against the
        # account's order presets (e.g. blank TIF -> DAY) instead of just
        # adjusting them, unless "Bypass Order Precautions for API Orders" is
        # enabled in TWS/Gateway's API precaution settings.
        parent = MarketOrder(action, quantity)
        parent.transmit = False
        parent.outsideRth = outside_rth
        parent.tif = "DAY"

        take_profit = LimitOrder(exit_action, quantity, round_to_tick(target_price, min_tick))
        take_profit.transmit = False
        take_profit.outsideRth = outside_rth
        take_profit.tif = "DAY"

        stop_loss = StopOrder(exit_action, quantity, round_to_tick(stop_price, min_tick))
        stop_loss.transmit = True
        stop_loss.outsideRth = outside_rth
        stop_loss.tif = "DAY"

        parent_trade = self.ib.placeOrder(contract, parent)
        parent.orderId = parent_trade.order.orderId

        take_profit.parentId = parent.orderId
        stop_loss.parentId = parent.orderId

        self.ib.placeOrder(contract, take_profit)
        self.ib.placeOrder(contract, stop_loss)

        self._entry_trades[contract.conId] = parent_trade

        log.info(
            "Bracket order placed: %s %s x%s, stop=%.4f, target=%.4f",
            action,
            contract.symbol,
            quantity,
            stop_loss.auxPrice,
            take_profit.lmtPrice,
        )
        return parent_trade

    def has_live_protective_stop(self, contract: Contract, position_qty: float) -> bool:
        """True if a live stop order exists that closes (part of) this
        position: a SELL stop for a long, a BUY stop for a short."""
        closing_action = "SELL" if position_qty > 0 else "BUY"
        for t in self.ib.openTrades():
            if (
                t.contract.conId == contract.conId
                and not t.isDone()
                and t.order.orderType in ("STP", "STP LMT")
                and t.order.action == closing_action
            ):
                return True
        return False

    def has_pending_close(self, contract: Contract, position_qty: float) -> bool:
        """True if a market order that would close this position is already
        in flight (a flatten mid-execution) -- placing a protective stop on
        top of that would double-close into a reverse position."""
        closing_action = "SELL" if position_qty > 0 else "BUY"
        for t in self.ib.openTrades():
            if (
                t.contract.conId == contract.conId
                and not t.isDone()
                and t.order.orderType == "MKT"
                and t.order.action == closing_action
            ):
                return True
        return False

    def place_protective_stop(
        self,
        contract: Contract,
        position_qty: float,
        stop_price: float,
        outside_rth: bool = False,
        min_tick: float = 0.01,
    ) -> Trade:
        """Attaches a standalone stop-loss to an existing position that has
        none (found by the engine's reconciliation pass -- e.g. bracket
        children that expired as DAY orders while the position survived an
        early close, or a restart mid-bracket)."""
        action = "SELL" if position_qty > 0 else "BUY"
        order = StopOrder(action, abs(position_qty), round_to_tick(stop_price, min_tick))
        order.tif = "DAY"
        order.outsideRth = outside_rth
        trade = self.ib.placeOrder(contract, order)
        log.error(
            "%s: position of %s had NO live stop-loss -- re-attached protective stop @ %.4f.",
            contract.symbol,
            position_qty,
            order.auxPrice,
        )
        return trade

    def flatten_position(self, contract: Contract, position_qty: float) -> Trade | None:
        """Cancel any resting exit orders for this contract and close it at market."""
        if position_qty == 0:
            return None

        for order in list(self.ib.openTrades()):
            if order.contract.conId == contract.conId and not order.isDone():
                self.ib.cancelOrder(order.order)

        action = "SELL" if position_qty > 0 else "BUY"
        order = MarketOrder(action, abs(position_qty))
        order.tif = "DAY"
        trade = self.ib.placeOrder(contract, order)
        log.warning("Flattening %s: %s %s @ market", contract.symbol, action, abs(position_qty))
        return trade

    def flatten_all(self) -> None:
        self._flatten_matching(lambda p: True, log_label="Flatten-all")

    def flatten_contracts(self, contracts: list[Contract]) -> None:
        """Flatten only positions in the given contracts (used for per-market
        end-of-session flattening, leaving other markets' positions alone)."""
        con_ids = {c.conId for c in contracts}
        self._flatten_matching(lambda p: p.contract.conId in con_ids, log_label="Flatten")

    def _flatten_matching(self, predicate, log_label: str) -> None:
        positions = [p for p in self.ib.positions() if p.position != 0 and predicate(p)]
        if not positions:
            log.info("%s: no open positions.", log_label)
            return
        log.warning("%s triggered: closing %d open position(s).", log_label, len(positions))
        for p in positions:
            self.flatten_position(p.contract, p.position)
