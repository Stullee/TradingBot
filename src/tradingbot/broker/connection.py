"""Thin wrapper around ib_async's IB() client: connect/reconnect + contract helpers."""
from __future__ import annotations

import asyncio
import logging

from ib_async import IB, Contract, Crypto, PortfolioItem, Stock

from tradingbot.config import Settings

log = logging.getLogger(__name__)


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
        # flattens. Reflects realized P&L since this connection was opened.
        self.realized_pnl_by_symbol: dict[str, float] = {}
        self.ib.updatePortfolioEvent += self._on_portfolio_update
        self._closing = False

    def _on_portfolio_update(self, item: PortfolioItem) -> None:
        self.realized_pnl_by_symbol[item.contract.symbol] = item.realizedPNL

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
        # Fall back to delayed data wherever this account lacks a live/real-time
        # entitlement, instead of silently getting nothing back (this is what
        # was causing FX ticker lookups -- used for cross-currency position
        # sizing -- to fail outright on accounts without live FX/crypto data).
        # Does not downgrade instruments the account *does* have live access to.
        self.ib.reqMarketDataType(3)
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
        log.warning("Lost connection to IB. Will attempt to reconnect...")
        asyncio.ensure_future(self.connect_with_retry())

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
