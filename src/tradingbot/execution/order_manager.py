"""Order placement: bracket entries (market parent + stop-loss + take-profit) and
end-of-day flatten-all. Every entry always carries a protective stop -- no naked
positions are ever opened, and ensure_protective_stop() re-attaches one if a
position is ever found without it (e.g. expired DAY children after a holiday).

Prices are rounded to the contract's *price-banded* tick (ContractMeta's
market-rule bands, falling back to its minTick) -- exchanges with banded
ticks (MiFID: a EUR30 stock ticks in 0.005; Hong Kong: a HK$380 stock ticks
in 0.2) reject orders at off-tick prices with error 110, and a rejected
bracket child is exactly the naked-position situation the bracket exists to
prevent (confirmed live: DBK children at 4 decimals, both legs cancelled)."""
from __future__ import annotations

import logging
import math
import time

from ib_async import IB, Contract, LimitOrder, MarketOrder, StopOrder, Trade

from tradingbot.broker.connection import ContractMeta

log = logging.getLogger(__name__)

# An entry market order on a liquid symbol fills in seconds. One still not
# done after this long is stuck -- e.g. a parent whose bracket children were
# rejected at invalid prices was never transmitted and sat pending
# (confirmed live), blocking its symbol and occupying a position slot.
STALE_ENTRY_MAX_AGE_SEC = 600.0


def round_to_tick(price: float, min_tick: float, mode: str = "nearest") -> float:
    """Multiple of `min_tick`, cleaned of binary-float dust so the wire
    value is exact (IB rejects e.g. 380.20000000000003). mode "up"/"down"
    rounds toward that direction -- marketable entry limits must round
    *toward* marketability (up for BUY, down for SELL): confirmed live, a
    coarse tick rounded a DOGE buy limit below the market, leaving an IOC
    order that could never fill."""
    if min_tick <= 0:
        min_tick = 0.01
    raw_steps = price / min_tick
    if mode == "up":
        steps = math.ceil(raw_steps - 1e-9)
    elif mode == "down":
        steps = math.floor(raw_steps + 1e-9)
    else:
        steps = round(raw_steps)
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
        self._entry_placed_at: dict[int, float] = {}

    def _prune_entries(self) -> None:
        for con_id in [c for c, t in self._entry_trades.items() if t.isDone()]:
            del self._entry_trades[con_id]
            self._entry_placed_at.pop(con_id, None)

    def cancel_stale_entries(self, max_age_sec: float = STALE_ENTRY_MAX_AGE_SEC) -> None:
        """Cancels entry parents that are neither filled nor cancelled after
        max_age_sec -- see STALE_ENTRY_MAX_AGE_SEC. Without this, a stuck
        parent blocks its symbol from new entries and counts as an occupied
        position slot indefinitely."""
        self._prune_entries()
        now = time.monotonic()
        for con_id, trade in list(self._entry_trades.items()):
            placed_at = self._entry_placed_at.get(con_id)
            if placed_at is None or now - placed_at < max_age_sec:
                continue
            log.warning(
                "Cancelling stale entry order for %s: not filled/cancelled after %.0f min.",
                trade.contract.symbol,
                (now - placed_at) / 60,
            )
            self.ib.cancelOrder(trade.order)
            del self._entry_trades[con_id]
            self._entry_placed_at.pop(con_id, None)

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
        meta: ContractMeta | None = None,
        entry_limit_price: float | None = None,
        attach_exits: bool = True,
    ) -> Trade:
        """action: 'BUY' to go long, 'SELL' to go short. Returns the parent Trade.
        outside_rth must be True for the order to be eligible to trigger/fill
        outside a market's regular trading hours (e.g. US pre/post-market).

        entry_limit_price: when set, the entry is a marketable LIMIT at that
        price instead of a market order. Used for crypto: IB rejects crypto
        market BUY orders denominated in units (confirmed live -- error
        10289 "You must set Cash Quantity for this order"); market buys
        there must be in fiat cashQty, which would leave the exit orders'
        unit quantity unknowable until the fill.

        attach_exits=False places ONLY the entry, as an IOC marketable
        limit, with no child orders at all -- the full ZeroHash crypto rule
        set, every line confirmed live: stop orders are unsupported (error
        387), buy orders must be TIF "Minutes or IOC" (error 201), and
        "This instrument doesn't support child/hedge orders" (error 201),
        so even a take-profit cannot be attached. IOC means the entry
        either fills immediately against the touch or cancels itself --
        no resting entry, nothing for cancel_stale_entries to reclaim.
        Exits are then engine-managed (engine._manage_crypto_exits): a
        standalone take-profit is placed once the position exists, and the
        stop is enforced in software."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        meta = meta or ContractMeta()

        if not attach_exits:
            if entry_limit_price is None:
                raise ValueError("attach_exits=False requires entry_limit_price")
            entry = LimitOrder(
                action,
                quantity,
                round_to_tick(
                    entry_limit_price,
                    meta.tick_for(entry_limit_price),
                    mode="up" if action == "BUY" else "down",
                ),
            )
            entry.tif = "IOC"
            entry.outsideRth = outside_rth
            entry.transmit = True
            entry_trade = self.ib.placeOrder(contract, entry)
            self._entry_trades[contract.conId] = entry_trade
            self._entry_placed_at[contract.conId] = time.monotonic()
            log.info(
                "Entry (IOC limit) placed: %s %s x%s @ %.6f -- stop %.6f and target %.6f "
                "are engine-managed on this venue.",
                action,
                contract.symbol,
                quantity,
                entry.lmtPrice,
                stop_price,
                target_price,
            )
            return entry_trade

        exit_action = "SELL" if action == "BUY" else "BUY"

        # tif is set explicitly (rather than left blank) because IB silently
        # cancels API orders whose parameters get auto-corrected against the
        # account's order presets (e.g. blank TIF -> DAY) instead of just
        # adjusting them, unless "Bypass Order Precautions for API Orders" is
        # enabled in TWS/Gateway's API precaution settings.
        if entry_limit_price is not None:
            parent: MarketOrder | LimitOrder = LimitOrder(
                action, quantity, round_to_tick(entry_limit_price, meta.tick_for(entry_limit_price))
            )
        else:
            parent = MarketOrder(action, quantity)
        parent.transmit = False
        parent.outsideRth = outside_rth
        parent.tif = "DAY"

        take_profit = LimitOrder(
            exit_action, quantity, round_to_tick(target_price, meta.tick_for(target_price))
        )
        take_profit.transmit = False
        take_profit.outsideRth = outside_rth
        take_profit.tif = "DAY"

        stop_loss = StopOrder(
            exit_action, quantity, round_to_tick(stop_price, meta.tick_for(stop_price))
        )
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
        self._entry_placed_at[contract.conId] = time.monotonic()

        log.info(
            "Bracket order placed: %s %s x%s, stop=%.4f, target=%.4f",
            action,
            contract.symbol,
            quantity,
            stop_loss.auxPrice,
            take_profit.lmtPrice,
        )
        return parent_trade

    def place_standalone_take_profit(
        self,
        contract: Contract,
        position_qty: float,
        target_price: float,
        meta: ContractMeta | None = None,
    ) -> Trade:
        """A plain (non-child) limit order closing the position at the
        target -- for venues that don't support attached orders (ZeroHash).
        Placed by the engine once the position actually exists, sized to
        the real filled quantity."""
        meta = meta or ContractMeta()
        action = "SELL" if position_qty > 0 else "BUY"
        order = LimitOrder(
            action, abs(position_qty), round_to_tick(target_price, meta.tick_for(target_price))
        )
        order.tif = "DAY"
        trade = self.ib.placeOrder(contract, order)
        log.info(
            "Standalone take-profit placed: %s %s x%s @ %.6f",
            action,
            contract.symbol,
            abs(position_qty),
            order.lmtPrice,
        )
        return trade

    def has_live_take_profit(self, contract: Contract, position_qty: float) -> bool:
        """True if a live limit order that closes (part of) this position
        is resting."""
        closing_action = "SELL" if position_qty > 0 else "BUY"
        for t in self.ib.openTrades():
            if (
                t.contract.conId == contract.conId
                and not t.isDone()
                and t.order.orderType == "LMT"
                and t.order.action == closing_action
            ):
                return True
        return False

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
        meta: ContractMeta | None = None,
    ) -> Trade:
        """Attaches a standalone stop-loss to an existing position that has
        none (found by the engine's reconciliation pass -- e.g. bracket
        children that expired as DAY orders while the position survived an
        early close, or a restart mid-bracket)."""
        meta = meta or ContractMeta()
        action = "SELL" if position_qty > 0 else "BUY"
        order = StopOrder(
            action, abs(position_qty), round_to_tick(stop_price, meta.tick_for(stop_price))
        )
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
