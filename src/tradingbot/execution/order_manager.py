"""Order placement: bracket entries (market parent + stop-loss + take-profit) and
end-of-day flatten-all. Every entry always carries a protective stop -- no naked
positions are ever opened."""
from __future__ import annotations

import logging

from ib_async import IB, Contract, LimitOrder, MarketOrder, StopOrder, Trade

log = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, ib: IB):
        self.ib = ib

    def place_bracket(
        self,
        contract: Contract,
        action: str,
        quantity: int,
        stop_price: float,
        target_price: float,
        outside_rth: bool = False,
    ) -> Trade:
        """action: 'BUY' to go long, 'SELL' to go short. Returns the parent Trade.
        outside_rth must be True for the order to be eligible to trigger/fill
        outside a market's regular trading hours (e.g. US pre/post-market)."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        exit_action = "SELL" if action == "BUY" else "BUY"

        parent = MarketOrder(action, quantity)
        parent.transmit = False
        parent.outsideRth = outside_rth

        take_profit = LimitOrder(exit_action, quantity, round(target_price, 2))
        take_profit.transmit = False
        take_profit.outsideRth = outside_rth

        stop_loss = StopOrder(exit_action, quantity, round(stop_price, 2))
        stop_loss.transmit = True
        stop_loss.outsideRth = outside_rth

        parent_trade = self.ib.placeOrder(contract, parent)
        parent.orderId = parent_trade.order.orderId

        take_profit.parentId = parent.orderId
        stop_loss.parentId = parent.orderId

        self.ib.placeOrder(contract, take_profit)
        self.ib.placeOrder(contract, stop_loss)

        log.info(
            "Bracket order placed: %s %s x%d, stop=%.2f, target=%.2f",
            action,
            contract.symbol,
            quantity,
            stop_price,
            target_price,
        )
        return parent_trade

    def flatten_position(self, contract: Contract, position_qty: float) -> Trade | None:
        """Cancel any resting exit orders for this contract and close it at market."""
        if position_qty == 0:
            return None

        for order in list(self.ib.openTrades()):
            if order.contract.conId == contract.conId and not order.isDone():
                self.ib.cancelOrder(order.order)

        action = "SELL" if position_qty > 0 else "BUY"
        order = MarketOrder(action, abs(position_qty))
        trade = self.ib.placeOrder(contract, order)
        log.warning("Flattening %s: %s %d @ market", contract.symbol, action, abs(position_qty))
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
