"""Orchestrates news polling, Claude sentiment assessment, and shadow-trade
tracking. Observation-only: never touches the broker or places real orders."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from tradingbot.config import Settings
from tradingbot.data.bars import BarStream
from tradingbot.data.indicators import add_indicators
from tradingbot.news.finnhub_client import FinnhubNewsClient
from tradingbot.news.sentiment import NewsAssessment, NewsSentimentAnalyzer
from tradingbot.news.shadow_trade import ShadowTradeTracker

log = logging.getLogger(__name__)

# Finnhub's free tier allows ~60 requests/minute. _poll_news() makes one
# request per watched symbol every poll cycle -- with a large watchlist,
# firing them back-to-back in a tight loop blows well past that limit
# (confirmed live: 429s on a 70+ symbol watchlist). Spacing calls out by
# this much keeps a 70-symbol poll under ~80s, comfortably inside the
# per-minute limit, well within the (much longer) poll interval budget.
_FINNHUB_REQUEST_GAP_SEC = 1.1

# Finnhub's 1-day lookback returns everything for that rolling window every
# poll, not just what's actually new -- on a fresh start (or any symbol
# with no seen-ids history yet), that backlog can be huge for an active
# stock (confirmed live: 250 articles for NVDA in one poll). Dumping all of
# them into a single Claude call is both needlessly expensive (one massive
# prompt) and bad for signal quality -- 250 mostly-unrelated headlines
# synthesized into one verdict isn't "one focused picture," it's noise.
# Capping the batch and prioritizing the most recent articles means the
# rest simply stay unseen and get picked up (still capped) on subsequent
# polls, spreading a one-time backlog out instead of dumping it all at once.
_MAX_ARTICLES_PER_BATCH = 10


class NewsMonitor:
    def __init__(
        self,
        settings: Settings,
        bars: BarStream,
        is_market_open: Callable[[str], bool] | None = None,
        should_flatten_shadow_trade: Callable[[str, str], bool] | None = None,
    ):
        self.settings = settings
        self.bars = bars
        # Whether `symbol`'s own market is currently open -- checked before
        # opening a shadow trade (see _handle_articles) so a news hit outside
        # trading hours doesn't "enter" at a last price that's really just a
        # stale, hours-old bar (confirmed live: an EU symbol's shadow trade
        # opened using its last close from before that market's close, hours
        # after the fact). Defaults to "always open" for callers that don't
        # care (tests, one-off tools) -- only the live engine passes a real
        # per-symbol session check.
        self._is_market_open = is_market_open or (lambda symbol: True)
        # Whether an open shadow trade for `symbol` (given its opened_at)
        # should be force-closed now -- mirrors real positions getting
        # flattened before their market's close, so shadow trades carry the
        # same no-overnight-risk discipline instead of just sitting open
        # until they happen to hit stop/target/timeout (confirmed live:
        # trades carrying over, still open, from the previous day). Defaults
        # to "never" for callers that don't care; only the live engine
        # passes a real per-symbol/session check.
        self._should_flatten_shadow_trade = should_flatten_shadow_trade or (
            lambda symbol, opened_at: False
        )
        self.finnhub = FinnhubNewsClient(settings.finnhub_api_key)
        self.analyzer = NewsSentimentAnalyzer(settings.anthropic_api_key, settings.news_model)
        self.shadow = ShadowTradeTracker(
            log_path=Path(settings.log_dir) / "shadow_trades.jsonl",
            max_hold_min=settings.news_max_hold_min,
        )
        # Every article's outcome gets persisted here -- both the actual
        # Claude assessment (direction/confidence/rationale) and articles
        # skipped without calling Claude at all (a shadow trade was already
        # open for that symbol). Two things this fixes vs. an in-memory-only
        # seen-set: (1) a restart doesn't forget what's already been
        # assessed -- Finnhub is polled with a 1-day lookback every cycle
        # regardless of restarts, so forgetting meant every restart
        # re-billed Claude for the whole rolling backlog in one burst, which
        # is what was actually draining API credits fast, not the poll
        # interval; (2) you get a readable, queryable record of every
        # analysis instead of only ephemeral log lines.
        self._analysis_log_path = Path(settings.log_dir) / "news_analysis.jsonl"
        self._seen_article_ids: set[int] = self._load_seen_ids()
        self._last_poll_monotonic: float = 0.0

    def _load_seen_ids(self) -> set[int]:
        if not self._analysis_log_path.exists():
            return set()
        ids: set[int] = set()
        with self._analysis_log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.update(json.loads(line)["article_ids"])
                except (json.JSONDecodeError, KeyError):
                    continue
        return ids

    def _persist_seen(
        self,
        symbol: str,
        article_ids: list[int],
        headlines: list[str],
        assessment: NewsAssessment | None,
        skipped_reason: str = "shadow trade already open for this symbol",
    ) -> None:
        """Marks these article ids seen (never re-assessed again, even
        across a restart) and appends a record of what happened -- either a
        real assessment, or why one was skipped without calling Claude."""
        self._seen_article_ids.update(article_ids)
        record = {
            "symbol": symbol,
            "article_ids": article_ids,
            "headlines": headlines,
            "assessed_at": datetime.now(timezone.utc).isoformat(),
            "skipped_reason": None if assessment else skipped_reason,
            "direction": assessment.direction if assessment else None,
            "confidence": assessment.confidence if assessment else None,
            "rationale": assessment.rationale if assessment else None,
        }
        self._analysis_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._analysis_log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

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
        """Checked in this order for each symbol with an open shadow trade:

        1. Should it be flattened now (today's close window reached, or it
           carried over from an earlier calendar day than this check
           existed)? If so, force-close it at the current price -- the same
           no-overnight-risk discipline real positions get, rather than
           riding indefinitely until it happens to hit stop/target/timeout.
        2. Otherwise, is the market closed? Skip entirely, not just the
           stop/target check -- shadow.update()'s max-hold timeout is
           wall-clock based, so calling it with the market's last (frozen,
           hours-stale) close price would fabricate a TIMEOUT close at some
           arbitrary point overnight using a price nothing actually traded
           at. Skipping defers that check to the market's next real, fresh
           price instead of manufacturing an exit against a stale one.
        3. Otherwise, a normal stop/target/timeout update."""
        for symbol in self.settings.symbol_list:
            trade = self.shadow.open_trades.get(symbol)
            if trade is None:
                continue
            df = self.bars.dataframe(symbol)
            if df is None or df.empty:
                continue
            price = float(df["close"].iloc[-1])

            if self._should_flatten_shadow_trade(symbol, trade.opened_at):
                self.shadow.flatten(symbol, price)
                continue

            if not self._is_market_open(symbol):
                continue

            self.shadow.update(symbol, price)

    async def _poll_news(self) -> None:
        symbols_with_new_articles = 0
        articles_assessed = 0
        articles_deferred = 0

        for i, symbol in enumerate(self.settings.symbol_list):
            if i > 0:
                await asyncio.sleep(_FINNHUB_REQUEST_GAP_SEC)
            try:
                articles = await self.finnhub.company_news(symbol)
            except Exception as exc:  # noqa: BLE001 - one bad poll shouldn't kill the engine
                log.warning("Failed to fetch news for %s: %s", symbol, exc)
                continue

            new_articles = [
                a
                for a in articles
                if a.get("id") is not None
                and a["id"] not in self._seen_article_ids
                and a.get("headline")
            ]
            if not new_articles:
                continue

            symbols_with_new_articles += 1
            new_articles.sort(key=lambda a: a.get("datetime", 0), reverse=True)
            batch = new_articles[:_MAX_ARTICLES_PER_BATCH]
            articles_assessed += len(batch)
            articles_deferred += len(new_articles) - len(batch)
            await self._handle_articles(symbol, batch)

        log.info(
            "News poll complete: %d/%d symbols had new articles, %d assessed, "
            "%d deferred to a later poll (batch cap=%d).",
            symbols_with_new_articles,
            len(self.settings.symbol_list),
            articles_assessed,
            articles_deferred,
            _MAX_ARTICLES_PER_BATCH,
        )

    async def _handle_articles(self, symbol: str, articles: list[dict]) -> None:
        """Assesses every new article about `symbol` from this poll together
        as one combined picture (rather than one isolated call per
        headline), so e.g. a mix of good and bad news nets out sensibly
        instead of each piece being judged with no awareness of the others.
        This also directly cuts API call volume on days with multiple
        articles about the same stock."""
        article_ids = [a["id"] for a in articles]
        headlines = [a.get("headline", "") for a in articles]

        if self.shadow.has_open(symbol):
            self._persist_seen(symbol, article_ids, headlines, assessment=None)
            return  # one shadow trade per symbol at a time, keeps evaluation simple

        if not self._is_market_open(symbol):
            # A shadow trade opens at the last available bar's close --
            # meaningless (and misleading for the win-rate/avg-R stats this
            # is meant to inform) if that "current price" is actually hours
            # stale because the symbol's market is closed. Skipped here,
            # before the Claude call, not just before shadow.open(), so a
            # news hit outside trading hours doesn't cost an assessment call
            # for a trade that was never going to open anyway.
            log.info("%s: market closed, skipping news assessment.", symbol)
            self._persist_seen(
                symbol, article_ids, headlines, assessment=None,
                skipped_reason="market closed for this symbol",
            )
            return

        assessment = await self.analyzer.assess(symbol, articles)
        self._persist_seen(symbol, article_ids, headlines, assessment)

        log.info(
            "[NEWS] %s: %s (confidence=%.2f) from %d article(s) - %s | %s",
            symbol,
            assessment.direction,
            assessment.confidence,
            len(articles),
            assessment.rationale,
            "; ".join(headlines),
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
            headline="; ".join(headlines),
            confidence=assessment.confidence,
            rationale=assessment.rationale,
        )

    async def aclose(self) -> None:
        await self.finnhub.aclose()
        await self.analyzer.aclose()
