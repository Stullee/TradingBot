import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from tradingbot.news.monitor import NewsMonitor, PendingAssessment
from tradingbot.news.sentiment import NewsAssessment


def make_settings(tmp_path, **overrides):
    defaults = dict(
        finnhub_api_key="fh-key",
        anthropic_api_key="an-key",
        news_model="claude-haiku-4-5-20251001",
        news_max_hold_min=240,
        news_confidence_threshold=0.6,
        log_dir=str(tmp_path),
        symbol_list=["AAPL"],
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        atr_period=14,
        stop_atr_mult=1.5,
        target_atr_mult=2.5,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeFinnhub:
    def __init__(self, articles_by_symbol: dict[str, list[dict]] | None = None):
        self.articles_by_symbol = articles_by_symbol or {}
        self.calls: list[str] = []

    async def company_news(self, symbol):
        self.calls.append(symbol)
        return self.articles_by_symbol.get(symbol, [])


class FakeAnalyzer:
    def __init__(self, assessment: NewsAssessment | None = None):
        self.assessment = assessment or NewsAssessment("NONE", 0.0, "no-op")
        self.calls: list[tuple[str, list[dict]]] = []

    async def assess(self, symbol, articles):
        self.calls.append((symbol, list(articles)))
        return self.assessment


class FakeShadow:
    def __init__(self, open_symbols: set[str] | None = None):
        self.open_symbols = set(open_symbols or set())
        self.open_calls: list[tuple] = []

    def has_open(self, symbol):
        return symbol in self.open_symbols

    def open(self, symbol, direction, entry_price, stop_price, target_price, **kwargs):
        self.open_symbols.add(symbol)
        self.open_calls.append((symbol, direction, entry_price, stop_price, target_price))


def test_no_seen_ids_file_yet_starts_empty(tmp_path):
    monitor = NewsMonitor(make_settings(tmp_path), bars=None)
    assert monitor._seen_article_ids == set()


def test_seen_ids_persist_across_restarts():
    """The bug this guards against: a restart used to forget every article
    it had already assessed, re-billing Claude for the same day's backlog
    on every single restart. A second NewsMonitor instance pointed at the
    same log_dir must inherit what the first one had already seen."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = make_settings(tmp_dir)
        monitor1 = NewsMonitor(settings, bars=None)
        monitor1._persist_seen("AAPL", [123, 456], ["h1", "h2"], NewsAssessment("NONE", 0.1, "r"))

        monitor2 = NewsMonitor(settings, bars=None)  # simulates a restart
        assert monitor2._seen_article_ids == {123, 456}


def test_persist_seen_writes_full_assessment_record(tmp_path):
    monitor = NewsMonitor(make_settings(tmp_path), bars=None)
    monitor._persist_seen(
        "AAPL", [1], ["Apple beats earnings"], NewsAssessment("LONG", 0.8, "strong beat")
    )

    lines = monitor._analysis_log_path.read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["symbol"] == "AAPL"
    assert record["article_ids"] == [1]
    assert record["direction"] == "LONG"
    assert record["confidence"] == 0.8


def test_poll_news_paces_requests_and_skips_gap_before_the_first(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingbot.news.monitor._FINNHUB_REQUEST_GAP_SEC", 0.02)
    settings = make_settings(tmp_path, symbol_list=["AAPL", "MSFT", "GOOGL"])
    monitor = NewsMonitor(settings, bars=None)
    fake = FakeFinnhub()
    monitor.finnhub = fake

    start = time.monotonic()
    asyncio.run(monitor._poll_news())
    elapsed = time.monotonic() - start

    assert fake.calls == ["AAPL", "MSFT", "GOOGL"]
    # 3 symbols -> 2 gaps, not 3 -> no wasted wait before the very first request
    assert elapsed >= 0.04


def test_multiple_new_articles_for_one_symbol_are_assessed_in_a_single_call(tmp_path):
    """This is the "big picture" behavior: N new articles about the same
    stock in one poll should cost one Claude call, not N."""
    articles = [
        {"id": 1, "headline": "Good news"},
        {"id": 2, "headline": "Bad news"},
        {"id": 3, "headline": "More news"},
    ]
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({"AAPL": articles})
    monitor.shadow = FakeShadow()
    fake_analyzer = FakeAnalyzer(NewsAssessment("NONE", 0.2, "mixed signals"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert len(fake_analyzer.calls) == 1
    symbol, seen_articles = fake_analyzer.calls[0]
    assert symbol == "AAPL"
    assert [a["id"] for a in seen_articles] == [1, 2, 3]
    assert monitor._seen_article_ids == {1, 2, 3}


def test_already_seen_articles_are_not_reassessed(tmp_path):
    articles = [{"id": 1, "headline": "Old news"}, {"id": 2, "headline": "New news"}]
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({"AAPL": articles})
    monitor.shadow = FakeShadow()
    monitor._seen_article_ids = {1}  # id 1 was already assessed in an earlier poll
    fake_analyzer = FakeAnalyzer(NewsAssessment("NONE", 0.1, "r"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert len(fake_analyzer.calls) == 1
    _, seen_articles = fake_analyzer.calls[0]
    assert [a["id"] for a in seen_articles] == [2]  # only the unseen one


def test_large_backlog_is_capped_and_deferred_not_dumped_in_one_call(tmp_path):
    """The bug this guards against: a fresh start (or any symbol with no
    seen-ids history) can have Finnhub's 1-day lookback return a huge batch
    (confirmed live: 250 articles for one symbol in one poll). All of that
    must not go into a single Claude call -- only the cap's worth of the
    most recent articles should be assessed; the rest stay unseen for a
    later poll."""
    articles = [
        {"id": i, "headline": f"headline {i}", "datetime": i} for i in range(1, 26)
    ]  # 25 articles, newest (highest id/datetime) last in this list on purpose
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({"AAPL": articles})
    monitor.shadow = FakeShadow()
    fake_analyzer = FakeAnalyzer(NewsAssessment("NONE", 0.2, "r"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert len(fake_analyzer.calls) == 1
    _, assessed = fake_analyzer.calls[0]
    assert len(assessed) == 10  # capped, not all 25
    # the most recent 10 (by datetime), not an arbitrary first-10 slice
    assert sorted(a["id"] for a in assessed) == list(range(16, 26))
    # only the assessed ones are marked seen -- the other 15 stay unseen
    # and will be picked up (still capped) on a later poll
    assert monitor._seen_article_ids == set(range(16, 26))


def test_analyzer_is_still_called_when_shadow_trade_already_open(tmp_path):
    """The bug this guards against: an article that arrives while a shadow
    trade is already open used to be skipped before ever calling Claude --
    marked seen and never reconsidered, even after that trade later closed
    and the symbol was free again. It's now assessed once regardless (an
    article is billed to Claude exactly once, ever) and kept as a pending
    candidate instead of being thrown away."""
    articles = [{"id": 1, "headline": "headline"}]
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({"AAPL": articles})
    monitor.shadow = FakeShadow(open_symbols={"AAPL"})
    fake_analyzer = FakeAnalyzer(NewsAssessment("LONG", 0.8, "strong beat"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert len(fake_analyzer.calls) == 1  # assessed despite the open trade
    assert monitor._seen_article_ids == {1}
    assert monitor.shadow.open_calls == []  # but not opened -- one trade per symbol at a time
    assert "AAPL" in monitor._pending  # kept, not discarded


def test_assessment_becomes_pending_when_market_is_closed(tmp_path):
    """A news hit for a symbol whose market is currently closed must not
    open a shadow trade at whatever price its last bar happened to close at
    -- that price could be hours stale (the bug this guards against: an EU
    symbol's shadow trade opened using its pre-close price, hours after
    that market had actually closed). It's still assessed once, though, and
    kept pending for the market's next open."""
    articles = [{"id": 1, "headline": "headline"}]
    settings = make_settings(tmp_path, symbol_list=["ASML"])
    monitor = NewsMonitor(settings, bars=None, is_market_open=lambda symbol: False)
    monitor.finnhub = FakeFinnhub({"ASML": articles})
    monitor.shadow = FakeShadow()
    fake_analyzer = FakeAnalyzer(NewsAssessment("LONG", 0.8, "strong beat"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert len(fake_analyzer.calls) == 1
    assert monitor._seen_article_ids == {1}
    assert monitor.shadow.open_calls == []
    assert "ASML" in monitor._pending


class FakeOhlcvBars:
    """Unlike FakeBars, provides enough real OHLCV rows for add_indicators
    (used by _try_open_pending to size a shadow trade)."""

    def __init__(self, closes: dict[str, float], n: int = 20):
        self.frames = {
            symbol: pd.DataFrame(
                {"open": [c] * n, "high": [c] * n, "low": [c] * n, "close": [c] * n, "volume": [1] * n},
                index=pd.date_range("2026-07-16 09:00", periods=n, freq="5min", tz="UTC"),
            )
            for symbol, c in closes.items()
        }

    def dataframe(self, symbol):
        return self.frames.get(symbol)


def test_pending_assessment_opens_once_the_market_is_open_again(tmp_path):
    """The actual point of caching the assessment: once whatever blocked it
    clears (here, the market opening), a later tick opens the trade from
    the cached assessment without calling Claude a second time."""
    settings = make_settings(tmp_path, symbol_list=["ASML"])
    is_open = {"value": False}
    monitor = NewsMonitor(
        settings, bars=FakeOhlcvBars({"ASML": 700.0}), is_market_open=lambda symbol: is_open["value"]
    )
    monitor.finnhub = FakeFinnhub({"ASML": [{"id": 1, "headline": "headline"}]})
    monitor.shadow = FakeShadow()
    fake_analyzer = FakeAnalyzer(NewsAssessment("LONG", 0.8, "strong beat"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())
    assert monitor.shadow.open_calls == []
    assert len(fake_analyzer.calls) == 1

    is_open["value"] = True
    monitor._retry_pending_assessments()

    assert len(monitor.shadow.open_calls) == 1
    assert monitor.shadow.open_calls[0][0:2] == ("ASML", "LONG")
    assert "ASML" not in monitor._pending  # consumed, not retried again
    assert len(fake_analyzer.calls) == 1  # still never billed twice


def test_pending_assessment_ages_out_without_ever_becoming_actionable(tmp_path):
    """An assessment that's been blocked long enough to exceed
    news_max_hold_min is stale news by the time it could act -- discarded
    rather than opened against a market that's moved on."""
    settings = make_settings(tmp_path, symbol_list=["AAPL"], news_max_hold_min=60)
    monitor = NewsMonitor(
        settings, bars=FakeBars({"AAPL": 200.0}), is_market_open=lambda symbol: True
    )
    monitor.shadow = FakeShadow()
    old_assessment = PendingAssessment(
        direction="LONG",
        confidence=0.8,
        rationale="old news",
        headline="old headline",
        assessed_at=(datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(),
    )
    monitor._pending["AAPL"] = old_assessment

    monitor._retry_pending_assessments()

    assert monitor.shadow.open_calls == []
    assert "AAPL" not in monitor._pending


def test_market_open_check_defaults_to_always_open(tmp_path):
    """Callers that don't pass is_market_open (tests, one-off tools) get the
    old unconditional behavior -- only the live engine wires up a real
    per-symbol check."""
    articles = [{"id": 1, "headline": "headline"}]
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({"AAPL": articles})
    monitor.shadow = FakeShadow()
    fake_analyzer = FakeAnalyzer(NewsAssessment("NONE", 0.2, "r"))
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert len(fake_analyzer.calls) == 1


class FakeBars:
    def __init__(self, closes: dict[str, float]):
        self.closes = closes

    def dataframe(self, symbol):
        if symbol not in self.closes:
            return None
        return pd.DataFrame({"close": [self.closes[symbol]]})


class FakeShadowUpdateTracker:
    def __init__(self, open_symbols=None):
        self.update_calls: list[tuple[str, float]] = []
        self.flatten_calls: list[tuple[str, float]] = []
        self.open_trades = {
            symbol: SimpleNamespace(opened_at="2026-07-16T10:00:00+00:00")
            for symbol in (open_symbols or [])
        }

    def update(self, symbol, price):
        self.update_calls.append((symbol, price))

    def flatten(self, symbol, price):
        self.flatten_calls.append((symbol, price))


def test_open_shadow_trades_are_not_updated_while_market_is_closed(tmp_path):
    """The bug this guards against: shadow.update()'s max-hold timeout is
    wall-clock based, so calling it overnight with the market's last
    (frozen) close price would fabricate a TIMEOUT close using a price
    nothing actually traded at -- confirmed as the same class of bug as the
    entry-side stale-price shadow trade this fix's sibling addressed."""
    settings = make_settings(tmp_path, symbol_list=["ASML"])
    monitor = NewsMonitor(
        settings, bars=FakeBars({"ASML": 700.0}), is_market_open=lambda symbol: False
    )
    monitor.shadow = FakeShadowUpdateTracker(open_symbols=["ASML"])

    monitor._update_open_shadow_trades()

    assert monitor.shadow.update_calls == []
    assert monitor.shadow.flatten_calls == []


def test_open_shadow_trades_are_updated_while_market_is_open(tmp_path):
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(
        settings, bars=FakeBars({"AAPL": 200.0}), is_market_open=lambda symbol: True
    )
    monitor.shadow = FakeShadowUpdateTracker(open_symbols=["AAPL"])

    monitor._update_open_shadow_trades()

    assert monitor.shadow.update_calls == [("AAPL", 200.0)]


def test_open_shadow_trade_is_flattened_instead_of_updated_when_due(tmp_path):
    """The bug this guards against: shadow trades never got the same
    no-overnight-risk discipline real positions get via should_flatten() --
    they just sat open until they happened to hit stop/target/timeout,
    which is why trades were still open a full day after they opened."""
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(
        settings,
        bars=FakeBars({"AAPL": 200.0}),
        is_market_open=lambda symbol: True,
        should_flatten_shadow_trade=lambda symbol, opened_at: True,
    )
    monitor.shadow = FakeShadowUpdateTracker(open_symbols=["AAPL"])

    monitor._update_open_shadow_trades()

    assert monitor.shadow.flatten_calls == [("AAPL", 200.0)]
    assert monitor.shadow.update_calls == []


def test_poll_completion_is_always_logged_even_with_nothing_new(tmp_path, caplog):
    """The bug this guards against: a poll that finds zero new articles for
    every symbol used to log nothing at all, making "the news monitor is
    fine but quiet" indistinguishable from "the news monitor is silently
    broken" -- both just look like silence in the log."""
    settings = make_settings(tmp_path, symbol_list=["AAPL", "MSFT"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({})  # no articles for anyone

    with caplog.at_level("INFO"):
        asyncio.run(monitor._poll_news())

    assert "News poll complete" in caplog.text
    assert "0/2 symbols had new articles" in caplog.text
