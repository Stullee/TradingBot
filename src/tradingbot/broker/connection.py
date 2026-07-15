"""Thin wrapper around ib_async's IB() client: connect/reconnect + contract helpers."""
from __future__ import annotations

import asyncio
import logging

from ib_async import IB, Stock, Contract

from tradingbot.config import Settings

log = logging.getLogger(__name__)


class BrokerConnection:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ib = IB()
        self.ib.disconnectedEvent += self._on_disconnected
        self._closing = False

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
        log.info("Connected. Server version=%s", self.ib.client.serverVersion())

    def _on_disconnected(self) -> None:
        if self._closing:
            return
        log.warning("Lost connection to IB. Will attempt to reconnect...")
        asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 5
        while not self._closing and not self.ib.isConnected():
            try:
                await self.connect()
                log.info("Reconnected to IB.")
                return
            except Exception as exc:  # noqa: BLE001 - keep retrying on any connection error
                log.error("Reconnect attempt failed: %s. Retrying in %ss.", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def qualify_stock(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Contract:
        contract = Stock(symbol, exchange, currency)
        [qualified] = await self.ib.qualifyContractsAsync(contract)
        return qualified

    def account_net_liquidation(self) -> float:
        account = self.settings.ib_account_id or ""
        for v in self.ib.accountValues(account):
            if v.tag == "NetLiquidation" and (not account or v.account == account):
                return float(v.value)
        raise RuntimeError("NetLiquidation value not available yet from IB account updates")

    def disconnect(self) -> None:
        self._closing = True
        if self.ib.isConnected():
            self.ib.disconnect()
