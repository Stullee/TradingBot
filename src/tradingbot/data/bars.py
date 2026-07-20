"""Intraday bar subscriptions per symbol, exposed as pandas DataFrames.

Two modes: "live" uses IB's keepUpToDate=True push updates (used for stocks).
"polled" re-fetches the full bar list on demand instead -- IB rejects
keepUpToDate=True for crypto contracts ("Source price not supported with
live updates"), so crypto symbols use this mode with periodic refresh calls
from the engine instead."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime

import pandas as pd
from ib_async import IB, Contract

log = logging.getLogger(__name__)


class _HistoricalRequestPacer:
    """Keeps ALL of this stream's historical-data requests inside IB's
    pacing budget (~60 per rolling 10 minutes per connection), with margin.

    Confirmed live (Monday US open): two quick restarts (~40 startup
    subscribes each) plus a 22-symbol resubscribe burst exhausted the
    budget; the paced-out requests returned empty bar lists that replaced
    Friday's data, leaving every US symbol with no bars and no recovery.
    A single shared budget across subscribe/refresh/resubscribe makes that
    arithmetic impossible rather than merely unlikely."""

    def __init__(self, max_requests: int = 48, window_sec: float = 600.0):
        self._max_requests = max_requests
        self._window_sec = window_sec
        self._request_times: deque[float] = deque()
        self._now = time.monotonic  # injectable for tests

    async def wait_turn(self) -> None:
        while True:
            now = self._now()
            while self._request_times and now - self._request_times[0] > self._window_sec:
                self._request_times.popleft()
            if len(self._request_times) < self._max_requests:
                self._request_times.append(now)
                return
            wait = self._request_times[0] + self._window_sec - now
            log.warning(
                "Historical-data pacing budget exhausted (%d requests in the last "
                "%.0fs) -- delaying the next request %.0fs to stay inside IB's limit.",
                len(self._request_times),
                self._window_sec,
                wait,
            )
            await asyncio.sleep(max(wait, 1.0))

_BAR_SIZE_UNIT_SECONDS = {"sec": 1, "min": 60, "hour": 3600, "day": 86400, "week": 604800}


def parse_bar_size_seconds(bar_size: str) -> int:
    """Parses an IB bar size string (e.g. "5 mins", "1 hour") into seconds.
    Falls back to 300 (5 min, this bot's standard/tested bar size) for
    anything unrecognized rather than raising -- used for staleness
    thresholds, where a slightly-wrong fallback is harmless."""
    parts = bar_size.strip().split()
    if len(parts) == 2:
        try:
            n = int(parts[0])
            unit = parts[1].rstrip("s").lower()
        except ValueError:
            return 300
        if unit in _BAR_SIZE_UNIT_SECONDS:
            return n * _BAR_SIZE_UNIT_SECONDS[unit]
    return 300


def bars_to_dataframe(bars) -> pd.DataFrame:
    """Converts an ib_async BarDataList (or any list of objects with
    date/open/high/low/close/volume) into a sorted, tz-aware OHLCV DataFrame."""
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


class BarStream:
    """Keeps one historical bar list (today's session) per symbol."""

    def __init__(self, ib: IB, bar_size: str):
        self.ib = ib
        self.bar_size = bar_size
        self._bar_lists: dict[str, object] = {}
        self._polled: dict[str, tuple[Contract, bool, str]] = {}
        self._live: dict[str, tuple[Contract, bool, str]] = {}
        self._pacer = _HistoricalRequestPacer()

    async def subscribe(
        self,
        symbol: str,
        contract: Contract,
        use_rth: bool = True,
        what_to_show: str = "TRADES",
        live_updates: bool = True,
    ) -> None:
        if live_updates:
            await self._pacer.wait_turn()
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
            self._live[symbol] = (contract, use_rth, what_to_show)
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
        await self._pacer.wait_turn()
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

    async def refresh_all_polled(self) -> None:
        for symbol in list(self._polled):
            try:
                await self.refresh_polled(symbol)
            except Exception:  # noqa: BLE001 - one bad refresh shouldn't skip the rest
                log.exception("Failed to refresh polled bars for %s", symbol)

    def has_live_subscription(self, symbol: str) -> bool:
        return symbol in self._live

    def latest_bar_time(self, symbol: str) -> datetime | None:
        bars = self._bar_lists.get(symbol)
        return bars[-1].date if bars else None

    async def resubscribe_live(self, symbol: str) -> None:
        """Cancels and re-establishes a live (keepUpToDate=True) bar
        subscription for `symbol`. Used when the stream appears to have
        silently stalled -- IB market data farm hiccups don't always
        surface as an explicit error (confirmed live: bars frozen for 35+
        minutes during regular market hours with no error logged, alongside
        an unrelated "market data farm connection is inactive" warning)."""
        info = self._live.get(symbol)
        if info is None:
            return
        contract, use_rth, what_to_show = info
        old_bars = self._bar_lists.get(symbol)
        if old_bars is not None:
            self.ib.cancelHistoricalData(old_bars)
        await self._pacer.wait_turn()
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
        if bars:
            log.info("Re-subscribed stale live bar stream for %s", symbol)
        else:
            # Keep the (empty but keepUpToDate) subscription -- it may start
            # ticking when the farm recovers -- and the stale checker now
            # treats empty live subscriptions as resubscribe candidates, so
            # this state self-heals after the cooldown instead of sitting
            # bar-less forever (confirmed live at a Monday US open).
            log.warning(
                "Re-subscribe for %s returned no bars -- will retry after the cooldown.",
                symbol,
            )

    def log_latest(self, symbol: str) -> None:
        """Logs the current latest bar for a symbol -- works the same for both
        live-streamed and polled symbols, since both just populate
        self._bar_lists. Called periodically from the engine for every
        symbol, so live-streamed ones (stocks) get the same ongoing
        "is data actually moving" visibility that polled ones (crypto) do."""
        bars = self._bar_lists.get(symbol)
        if not bars:
            log.warning("%s: no bars yet (still no market data?)", symbol)
            return
        last = bars[-1]
        log.info("%s: %d bars, latest close=%.2f @ %s", symbol, len(bars), last.close, last.date)

    def dataframe(self, symbol: str) -> pd.DataFrame | None:
        bars = self._bar_lists.get(symbol)
        if not bars:
            return None
        return bars_to_dataframe(bars)

    def unsubscribe_all(self) -> None:
        for bars in self._bar_lists.values():
            self.ib.cancelHistoricalData(bars)
        self._bar_lists.clear()
        self._polled.clear()
        self._live.clear()
