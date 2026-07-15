"""Live FX conversion via IB forex quotes, so a single risk budget in the
account's base currency can size positions denominated in other currencies.
Identity fast-path when currencies match -- no IB request needed."""
from __future__ import annotations

import logging
import time

from ib_async import IB, Forex

log = logging.getLogger(__name__)


class FxConverter:
    def __init__(self, ib: IB, cache_ttl_sec: float = 60.0):
        self.ib = ib
        self.cache_ttl_sec = cache_ttl_sec
        self._rate_cache: dict[tuple[str, str], tuple[float, float]] = {}

    async def rate(self, from_ccy: str, to_ccy: str) -> float:
        """Multiplier to convert an amount in from_ccy into to_ccy."""
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return 1.0

        cached = self._rate_cache.get((from_ccy, to_ccy))
        now = time.monotonic()
        if cached and now - cached[1] < self.cache_ttl_sec:
            return cached[0]

        rate = await self._fetch_rate(from_ccy, to_ccy)
        self._rate_cache[(from_ccy, to_ccy)] = (rate, now)
        return rate

    async def convert(self, amount: float, from_ccy: str, to_ccy: str) -> float:
        return amount * await self.rate(from_ccy, to_ccy)

    async def _fetch_rate(self, from_ccy: str, to_ccy: str) -> float:
        # IB usually only lists one direction per pair (e.g. EURUSD, not
        # USDEUR) -- try direct, then fall back to the inverse.
        for pair, invert in ((f"{from_ccy}{to_ccy}", False), (f"{to_ccy}{from_ccy}", True)):
            try:
                [ticker] = await self.ib.reqTickersAsync(Forex(pair))
                price = ticker.midpoint()
                if price and price == price:  # excludes NaN/None/0
                    return (1 / price) if invert else price
            except Exception as exc:  # noqa: BLE001 - try the next pair/direction
                log.debug("FX lookup for %s failed: %s", pair, exc)
        raise RuntimeError(f"Could not determine FX rate for {from_ccy}->{to_ccy}")
