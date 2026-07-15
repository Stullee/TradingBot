"""Orchestrates news polling, Claude sentiment assessment, and shadow-trade
tracking. Observation-only: never touches the broker or places real orders."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from tradingbot.config import Settings
from tradingbot.data.bars import BarStream
from tradingbot.data.indicators import add_indicators
from tradingbot.news.finnhub_client import FinnhubNewsClient
from tradingbot.news.sentiment import NewsSentimentAnalyzer
from tradingbot.news.shadow_trade import ShadowTradeTracker

log = logging.getLogger(__name__)


class NewsMonitor:
    def __init__(self, settings: Settings, bars: BarStream):
        self.settings = settings
        self.bars = bars
        self.finnhub = FinnhubNewsClient(settings.finnhub_api_key)
        self.analyzer = NewsSentimentAnalyzer(settings.anthropic_api_key, settings.news_model)
        self.shadow = ShadowTradeTracker(
            log_path=Path(settings.log_dir) / "shadow_trades.jsonl",
            max_hold_min=settings.news_max_hold_min,
        )
        self._seen_article_ids: set[int] = set()
        self._last_poll_monotonic: float = 0.0

    async def tick(self) -> None:
        """Call on every engine tick. Price updates for open shadow trades are
        cheap and always run; the expensive news poll (external API calls)
        only actually fires every news_poll_interval_sec."""
        self._update_open_shadow_trades()

        now = time.monotonic()
        if now - self._last_poll_monotonic < self.settings.news_poll_interval_sec:
            return
        self._last_poll_monotonic = now
        await self._poll_news()

    def _update_open_shadow_trades(self) -> None:
        for symbol in self.settings.symbol_list:
            df = self.bars.dataframe(symbol)
            if df is None or df.empty:
                continue
            price = float(df["close"].iloc[-1])
            self.shadow.update(symbol, price)

    async def _poll_news(self) -> None:
        for symbol in self.settings.symbol_list:
            try:
                articles = await self.finnhub.company_news(symbol)
            except Exception as exc:  # noqa: BLE001 - one bad poll shouldn't kill the engine
                log.warning("Failed to fetch news for %s: %s", symbol, exc)
                continue

            for article in articles:
                article_id = article.get("id")
                if article_id is None or article_id in self._seen_article_ids:
                    continue
                self._seen_article_ids.add(article_id)
                await self._handle_article(symbol, article)

    async def _handle_article(self, symbol: str, article: dict) -> None:
        if self.shadow.has_open(symbol):
            return  # one shadow trade per symbol at a time, keeps evaluation simple

        headline = article.get("headline", "")
        summary = article.get("summary", "")
        if not headline:
            return

        assessment = await self.analyzer.assess(symbol, headline, summary)
        log.info(
            "[NEWS] %s: %s (confidence=%.2f) - %s | %s",
            symbol,
            assessment.direction,
            assessment.confidence,
            assessment.rationale,
            headline,
        )
        if assessment.direction == "NONE":
            return
        if assessment.confidence < self.settings.news_confidence_threshold:
            return

        df = self.bars.dataframe(symbol)
        if df is None or len(df) < self.settings.atr_period + 2:
            log.info("%s: not enough bar history yet to size a shadow trade, skipping.", symbol)
            return

        enriched = add_indicators(
            df,
            self.settings.ema_fast,
            self.settings.ema_slow,
            self.settings.rsi_period,
            self.settings.atr_period,
        )
        last = enriched.iloc[-1]
        entry_price = float(last["close"])
        atr_value = float(last["atr"])
        stop_dist = atr_value * self.settings.stop_atr_mult
        target_dist = atr_value * self.settings.target_atr_mult

        if assessment.direction == "LONG":
            stop_price = entry_price - stop_dist
            target_price = entry_price + target_dist
        else:
            stop_price = entry_price + stop_dist
            target_price = entry_price - target_dist

        self.shadow.open(
            symbol,
            assessment.direction,
            entry_price,
            stop_price,
            target_price,
            headline=headline,
            confidence=assessment.confidence,
            rationale=assessment.rationale,
        )

    async def aclose(self) -> None:
        await self.finnhub.aclose()
        await self.analyzer.aclose()
