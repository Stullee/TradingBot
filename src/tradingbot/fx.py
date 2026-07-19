"""Live FX conversion via a persistent IB forex market data subscription, so
a single risk budget in the account's base currency can size positions
denominated in other currencies. Identity fast-path when currencies match
-- no IB request needed.

Uses a streaming subscription (reqMktData, not a one-shot snapshot) kept
open per currency pair for the life of the connection, read from directly.
A one-shot snapshot request (reqTickersAsync) proved unreliable in
practice on this account -- repeatedly failing to populate within several
retries even for a liquid, genuinely tradeable pair (see engine.py's
history for EUR->USD). A persistent subscription only pays the
"waiting for the first tick" cost once per pair; every rate() call after
that reads an already-warm, continuously updating value with no IB round
trip on the critical path of processing a trade signal."""
from __future__ import annotations

import asyncio
import logging

from ib_async import IB, Forex, Ticker

log = logging.getLogger(__name__)


class FxConverter:
    def __init__(
        self,
        ib: IB,
        first_tick_attempts: int = 10,
        first_tick_delay_sec: float = 1.0,
    ):
        self.ib = ib
        self.first_tick_attempts = first_tick_attempts
        self.first_tick_delay_sec = first_tick_delay_sec
        self._tickers: dict[str, Ticker] = {}

    async def rate(self, from_ccy: str, to_ccy: str) -> float:
        """Multiplier to convert an amount in from_ccy into to_ccy."""
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return 1.0

        # IB usually only lists one direction per pair (e.g. EURUSD, not
        # USDEUR) -- try direct, then fall back to the inverse.
        for pair, invert in ((f"{from_ccy}{to_ccy}", False), (f"{to_ccy}{from_ccy}", True)):
            price = await self._price(pair)
            if price and price == price:  # excludes NaN/None/0
                return (1 / price) if invert else price
        raise RuntimeError(f"Could not determine FX rate for {from_ccy}->{to_ccy}")

    async def convert(self, amount: float, from_ccy: str, to_ccy: str) -> float:
        return amount * await self.rate(from_ccy, to_ccy)

    @staticmethod
    def _best_price(ticker: Ticker) -> float | None:
        """Midpoint when a live bid/ask exists; otherwise the most recent
        traded/closing price. The fallback matters when the FX market itself
        is closed (weekends): confirmed live, a Sunday crypto signal on a
        EUR-base account found a freshly subscribed EURUSD ticker with no
        bid/ask to build a midpoint from, and the trade was lost -- the
        prior close is plenty accurate for position sizing."""
        for value in (ticker.midpoint(), ticker.marketPrice(), ticker.close):
            if value and value == value:  # excludes None/0/NaN
                return value
        return None

    async def _price(self, pair: str) -> float | None:
        ticker = self._tickers.get(pair)
        if ticker is None:
            try:
                [qualified] = await self.ib.qualifyContractsAsync(Forex(pair))
            except Exception as exc:  # noqa: BLE001 - pair might just not exist, try the next
                log.debug("Could not qualify FX pair %s: %s", pair, exc)
                return None
            if qualified is None or not getattr(qualified, "conId", None):
                return None
            # reqMarketDataType is a client-wide setting, not per-request --
            # switching to delayed(3) only for the moment this subscription
            # is created (then straight back to live(1)) fixes accounts
            # without live FX entitlement without leaving every *other*
            # subsequent request on this connection (in particular the
            # engine's keepUpToDate equity bar streams) permanently degraded
            # to delayed too. Confirmed live: leaving delayed mode set
            # connection-wide made US-equity live bars update only every
            # 15+ min, mimicking a stalled feed.
            self.ib.reqMarketDataType(3)
            try:
                ticker = self.ib.reqMktData(qualified, "", False, False)
            finally:
                self.ib.reqMarketDataType(1)
            self._tickers[pair] = ticker
            log.info("Subscribed to live/delayed FX quotes for %s", pair)

        price = self._best_price(ticker)
        if price is not None:
            return price

        # Freshly subscribed (or IB hasn't sent a tick yet) -- give it a
        # moment; every later call for this pair reuses the now-warm ticker.
        for _ in range(self.first_tick_attempts):
            await asyncio.sleep(self.first_tick_delay_sec)
            price = self._best_price(ticker)
            if price is not None:
                return price
        return None

    def unsubscribe_all(self) -> None:
        for ticker in self._tickers.values():
            self.ib.cancelMktData(ticker.contract)
        self._tickers.clear()
