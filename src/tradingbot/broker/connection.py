"""Thin wrapper around ib_async's IB() client: connect/reconnect + contract helpers."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ib_async import IB, Contract, Crypto, PortfolioItem, Stock

from tradingbot.config import Settings

log = logging.getLogger(__name__)

# IB error codes that mean an order we placed was refused or is broken --
# the ones where "the bot thinks it has a bracket but doesn't" becomes
# possible. Everything else (farm status chatter, warnings) is already
# logged by ib_async itself at an appropriate level.
#   103: duplicate order id            110: price does not conform to min tick
#   200: no security definition        201: order rejected
#   203: security not allowed          321: server-side validation error
#   461/462: order held/rerouted oddities  10148: order to be cancelled is not valid
CRITICAL_ORDER_ERROR_CODES = {103, 110, 200, 201, 203, 321, 461, 462, 10148}


@dataclass(frozen=True)
class ContractMeta:
    """Per-contract trading increments from IB's ContractDetails + market
    rule. 0/empty means "not reported" -- the engine then falls back to
    whole shares (stocks) or a tiny fractional increment (crypto).

    `price_increments` are the exchange's *price-banded* ticks ((low_edge,
    increment), ascending) from IB's market rule. This matters because
    ContractDetails.minTick alone is the smallest theoretical tick, not the
    enforced one: confirmed live, DBK@TGATE reported a sub-0.005 minTick,
    the bracket children went out at 4 decimals, and both were rejected
    with error 110 ("price does not conform to the minimum price
    variation") -- the MiFID band for a EUR30 stock is 0.005."""

    min_tick: float = 0.01
    size_increment: float = 0.0
    min_size: float = 0.0
    price_increments: tuple[tuple[float, float], ...] = ()

    def tick_for(self, price: float) -> float:
        """The enforceable price increment at `price`: the market-rule band
        covering it, else the plain min_tick."""
        tick = 0.0
        for low_edge, increment in self.price_increments:
            if price >= low_edge:
                tick = increment
            else:
                break  # bands are sorted by low_edge
        return tick or self.min_tick or 0.01


class BrokerConnection:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ib = IB()
        self.ib.disconnectedEvent += self._on_disconnected
        # ib_async's own portfolio() cache drops a contract the instant its
        # position hits 0 (that's how updatePortfolio works -- see
        # ib_async.wrapper.Wrapper.updatePortfolio), taking that position's
        # realizedPNL with it. Tracked here independently, keyed by symbol,
        # so a fully closed position's realized P&L stays visible (e.g. in
        # the live dashboard/report) instead of vanishing the moment it
        # flattens.
        self.realized_pnl_by_symbol: dict[str, float] = {}
        self._realized_pnl_persist_path: Path | None = None
        self.ib.updatePortfolioEvent += self._on_portfolio_update
        self.ib.errorEvent += self._on_ib_error
        # Optional hook (set by the engine) invoked for CRITICAL_ORDER_ERROR
        # codes -- e.g. to fire an alert webhook. Must never raise.
        self.on_critical_order_error: Callable[[int, int, str], None] | None = None
        self._closing = False
        self._reconnect_task: asyncio.Task | None = None

    def enable_realized_pnl_persistence(self, path: Path) -> None:
        """Opt-in: loads any realized P&L persisted by a previous run of
        *this* connection (so a restart doesn't reset "today's" P&L back to
        empty), then keeps writing updates to `path` from then on.

        Deliberately not automatic in __init__ -- this class is also used
        for short-lived, one-off connections (CLI report, backtester,
        trend-filter comparison) that share the account's live
        updatePortfolio stream but hold no history of their own. If one of
        those also persisted, its near-empty in-memory tracker would
        overwrite the live engine's file with a snapshot missing everything
        that closed before it connected. Only the long-running live engine
        should call this."""
        self._realized_pnl_persist_path = path
        if path.exists():
            try:
                self.realized_pnl_by_symbol.update(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                log.warning("Could not read %s, starting with no realized P&L history.", path)

    def _on_ib_error(
        self, reqId: int, errorCode: int, errorString: str, contract: Contract | None = None, *args
    ) -> None:
        """Surfaces order-refusal errors loudly and to the optional alert
        hook. ib_async logs everything already, but rejections were easy to
        miss in the stream -- and a rejected bracket child is a position
        without its stop, which the engine's reconciliation pass then fixes
        but a human should hear about."""
        if errorCode not in CRITICAL_ORDER_ERROR_CODES:
            return
        # The same codes fire for non-order requests too: error 200 ("no
        # security definition") is also the normal response to probing a
        # nonexistent FX pair during qualification (confirmed live: the
        # FxConverter's USDEUR probe was reported as an "order error").
        # Only escalate when reqId is actually one of our submitted orders.
        if not any(t.order.orderId == reqId for t in self.ib.trades()):
            return
        symbol = getattr(contract, "symbol", None) or "?"
        log.error("IB order error %s for %s (orderId/reqId=%s): %s", errorCode, symbol, reqId, errorString)
        if self.on_critical_order_error is not None:
            try:
                self.on_critical_order_error(reqId, errorCode, f"{symbol}: {errorString}")
            except Exception:  # noqa: BLE001 - alert failure must not break the event stream
                log.exception("on_critical_order_error hook failed")

    def _on_portfolio_update(self, item: PortfolioItem) -> None:
        self.realized_pnl_by_symbol[item.contract.symbol] = item.realizedPNL
        if self._realized_pnl_persist_path is not None:
            self._realized_pnl_persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._realized_pnl_persist_path.write_text(json.dumps(self.realized_pnl_by_symbol))

    async def connect(self) -> None:
        s = self.settings
        log.info(
            "Connecting to IB at %s:%s (clientId=%s, %s)",
            s.ib_host,
            s.ib_port,
            s.ib_client_id,
            "PAPER" if s.is_paper else "LIVE",
        )
        await self.ib.connectAsync(s.ib_host, s.ib_port, clientId=s.ib_client_id, timeout=15)
        if s.ib_account_id:
            self.ib.reqAccountUpdates(True, s.ib_account_id)
        # Deliberately *not* calling reqMarketDataType(3) (delayed) here
        # globally: it's a client-wide setting that also applies to every bar
        # subscription made afterward, not just the FX ticker lookups it was
        # once added here to unblock (see fx.py's FxConverter._price, which
        # now scopes that same delayed-fallback to just its own subscription
        # call). Confirmed live: applying it connection-wide made every
        # keepUpToDate US-equity bar stream behave like delayed data (updates
        # only every 15+ min) even though the account has live entitlement
        # for them, which engine._check_stale_bars then endlessly flagged and
        # tried to "fix" by resubscribing -- a real IB behavior, not a bug in
        # that stale-check logic, and not fixable by resubscribing at all.
        log.info("Connected. Server version=%s", self.ib.client.serverVersion())

    async def connect_with_retry(self, max_delay: int = 60) -> None:
        """Like connect(), but keeps retrying with backoff instead of raising --
        used for the initial connection too, so the bot can be started before
        IB Gateway/TWS is up and running (e.g. account still being set up,
        IB Gateway mid-restart) and it'll just wait instead of crashing."""
        delay = 5
        while not self._closing:
            try:
                await self.connect()
                return
            except Exception as exc:  # noqa: BLE001 - keep retrying on any connection error
                log.warning(
                    "Could not connect to IB (%s). Retrying in %ss...", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    def _on_disconnected(self) -> None:
        if self._closing:
            return
        # ib_async's disconnectedEvent firing a second time before the first
        # reconnect attempt has finished (e.g. a connect that succeeds only
        # to immediately get kicked -- confirmed live: "Peer closed
        # connection. clientId 17 already in use?" seconds after a
        # successful reconnect) would otherwise spawn a second
        # connect_with_retry() loop racing the first one to reconnect with
        # the same clientId, which is exactly the kind of collision that
        # message describes. Only one reconnect loop may be in flight at a
        # time.
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        log.warning("Lost connection to IB. Will attempt to reconnect...")
        self._reconnect_task = asyncio.ensure_future(self.connect_with_retry())

    async def qualify_contract(
        self, security_type: str, symbol: str, exchange: str, currency: str
    ) -> Contract:
        if security_type == "CRYPTO":
            contract: Contract = Crypto(symbol, exchange, currency)
        else:
            contract = Stock(symbol, exchange, currency)
        [qualified] = await self.ib.qualifyContractsAsync(contract)
        if qualified is None or not getattr(qualified, "conId", None):
            raise ValueError(
                f"IB could not resolve {symbol!r} on {exchange}/{currency}. "
                "Double check IB's own symbol format for this exchange -- it often "
                "differs from other data providers (e.g. no '.DE'/'.L'/'.AS' suffix; "
                "the exchange field already disambiguates the listing)."
            )
        return qualified

    async def contract_meta(self, contract: Contract) -> ContractMeta:
        """Fetches the contract's price increments (market-rule bands +
        minTick) and size increments so orders conform to the venue's rules
        (MiFID/HK tick bands, board lots, fractional crypto sizes). Any
        failure falls back to defaults rather than blocking the symbol."""
        try:
            details = await self.ib.reqContractDetailsAsync(contract)
        except Exception as exc:  # noqa: BLE001 - metadata is best-effort
            log.warning("Could not fetch contract details for %s: %s", contract.symbol, exc)
            return ContractMeta()
        if not details:
            return ContractMeta()
        d = details[0]

        def _positive(value) -> float:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return 0.0
            return v if v > 0 else 0.0

        min_tick = _positive(d.minTick) or 0.01
        increments = await self._price_increments(contract, d)
        if not increments and getattr(contract, "secType", "") == "STK":
            # No band info and minTick is only the smallest *theoretical*
            # tick (see class docstring) -- a one-cent floor is valid for
            # every stock in this bot's universe and prevents the sub-tick
            # rejections seen live, at the cost of sub-$1/EUR1 penny-stock
            # precision this bot doesn't trade anyway.
            min_tick = max(min_tick, 0.01)

        return ContractMeta(
            min_tick=min_tick,
            size_increment=_positive(getattr(d, "sizeIncrement", 0.0)),
            min_size=_positive(getattr(d, "minSize", 0.0)),
            price_increments=increments,
        )

    async def _price_increments(self, contract: Contract, details) -> tuple:
        """Price-banded tick sizes from IB's market rule for the exchange
        this contract actually routes to. marketRuleIds is comma-separated,
        positionally aligned with validExchanges."""
        rule_ids = [r.strip() for r in (getattr(details, "marketRuleIds", "") or "").split(",") if r.strip()]
        if not rule_ids:
            return ()
        exchanges = [
            e.strip() for e in (getattr(details, "validExchanges", "") or "").split(",") if e.strip()
        ]
        rule_id = rule_ids[0]
        if contract.exchange in exchanges and len(rule_ids) == len(exchanges):
            rule_id = rule_ids[exchanges.index(contract.exchange)]
        try:
            rule = await self.ib.reqMarketRuleAsync(int(rule_id))
        except Exception as exc:  # noqa: BLE001 - metadata is best-effort
            log.warning(
                "Could not fetch market rule %s for %s: %s", rule_id, contract.symbol, exc
            )
            return ()
        increments = sorted(
            (float(pi.lowEdge), float(pi.increment))
            for pi in (rule or [])
            if float(pi.increment) > 0
        )
        return tuple(increments)

    def account_net_liquidation(self) -> float:
        account = self.settings.ib_account_id or ""
        for v in self.ib.accountValues(account):
            if v.tag == "NetLiquidation" and (not account or v.account == account):
                return float(v.value)
        raise RuntimeError("NetLiquidation value not available yet from IB account updates")

    def account_base_currency(self) -> str:
        account = self.settings.ib_account_id or ""
        for v in self.ib.accountValues(account):
            if v.tag == "NetLiquidation" and (not account or v.account == account):
                return v.currency or "USD"
        return "USD"

    def disconnect(self) -> None:
        self._closing = True
        if self.ib.isConnected():
            self.ib.disconnect()
