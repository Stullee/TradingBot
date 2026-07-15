"""Intraday bar subscriptions per symbol, exposed as pandas DataFrames.

Two modes: "live" uses IB's keepUpToDate=True push updates (used for stocks).
"polled" re-fetches the full bar list on demand instead -- IB rejects
keepUpToDate=True for crypto contracts ("Source price not supported with
live updates"), so crypto symbols use this mode with periodic refresh calls
from the engine instead."""
from __future__ import annotations

import logging

import pandas as pd
from ib_async import IB, Contract

log = logging.getLogger(__name__)


class BarStream:
    """Keeps one historical bar list (today's session) per symbol."""

    def __init__(self, ib: IB, bar_size: str):
        self.ib = ib
        self.bar_size = bar_size
        self._bar_lists: dict[str, object] = {}
        self._polled: dict[str, tuple[Contract, bool, str]] = {}

    async def subscribe(
        self,
        symbol: str,
        contract: Contract,
        use_rth: bool = True,
        what_to_show: str = "TRADES",
        live_updates: bool = True,
    ) -> None:
        if live_updates:
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting=self.bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=2,
                keepUpToDate=True,
            )
            self._bar_lists[symbol] = bars
        else:
            self._polled[symbol] = (contract, use_rth, what_to_show)
            await self.refresh_polled(symbol)

        log.info(
            "Subscribed to %s bars for %s (useRTH=%s, whatToShow=%s, live_updates=%s)",
            self.bar_size,
            symbol,
            use_rth,
            what_to_show,
            live_updates,
        )

    async def refresh_polled(self, symbol: str) -> None:
        contract, use_rth, what_to_show = self._polled[symbol]
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting=self.bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=2,
            keepUpToDate=False,
        )
        self._bar_lists[symbol] = bars
        if bars:
            last = bars[-1]
            log.info(
                "%s: %d bars, latest close=%.2f @ %s",
                symbol,
                len(bars),
                last.close,
                last.date,
            )
        else:
            log.warning("%s: refresh returned no bars (still no market data?)", symbol)

    async def refresh_all_polled(self) -> None:
        for symbol in list(self._polled):
            try:
                await self.refresh_polled(symbol)
            except Exception:  # noqa: BLE001 - one bad refresh shouldn't skip the rest
                log.exception("Failed to refresh polled bars for %s", symbol)

    def dataframe(self, symbol: str) -> pd.DataFrame | None:
        bars = self._bar_lists.get(symbol)
        if not bars:
            return None
        df = pd.DataFrame(
            {
                "date": [b.date for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            }
        )
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], utc=True)
        df = df.set_index("date").sort_index()
        return df

    def unsubscribe_all(self) -> None:
        for bars in self._bar_lists.values():
            self.ib.cancelHistoricalData(bars)
        self._bar_lists.clear()
        self._polled.clear()
