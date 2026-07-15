"""Live intraday bar subscriptions per symbol, exposed as pandas DataFrames."""
from __future__ import annotations

import logging

import pandas as pd
from ib_async import IB, Contract

log = logging.getLogger(__name__)


class BarStream:
    """Keeps one auto-updating historical bar list (today's session) per symbol."""

    def __init__(self, ib: IB, bar_size: str):
        self.ib = ib
        self.bar_size = bar_size
        self._bar_lists: dict[str, object] = {}

    async def subscribe(
        self,
        symbol: str,
        contract: Contract,
        use_rth: bool = True,
        what_to_show: str = "TRADES",
    ) -> None:
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
        log.info(
            "Subscribed to live %s bars for %s (useRTH=%s, whatToShow=%s)",
            self.bar_size,
            symbol,
            use_rth,
            what_to_show,
        )

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
