import asyncio
import json
import time
from types import SimpleNamespace

from tradingbot.news.monitor import NewsMonitor
from tradingbot.news.sentiment import NewsAssessment


def make_settings(tmp_path, **overrides):
    defaults = dict(
        finnhub_api_key="fh-key",
        anthropic_api_key="an-key",
        news_model="claude-haiku-4-5-20251001",
        news_max_hold_min=240,
        log_dir=str(tmp_path),
        symbol_list=["AAPL"],
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
        self.open_symbols = open_symbols or set()

    def has_open(self, symbol):
        return symbol in self.open_symbols


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
    assert record["skipped_reason"] is None


def test_persist_seen_records_skip_reason_without_an_assessment(tmp_path):
    monitor = NewsMonitor(make_settings(tmp_path), bars=None)
    monitor._persist_seen("AAPL", [1], ["headline"], assessment=None)

    lines = monitor._analysis_log_path.read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["direction"] is None
    assert "shadow trade already open" in record["skipped_reason"]


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


def test_no_analyzer_call_when_shadow_trade_already_open(tmp_path):
    """Saves the API call entirely (not just skips opening a new shadow
    trade) -- has_open() is checked before assess(), not after."""
    articles = [{"id": 1, "headline": "headline"}]
    settings = make_settings(tmp_path, symbol_list=["AAPL"])
    monitor = NewsMonitor(settings, bars=None)
    monitor.finnhub = FakeFinnhub({"AAPL": articles})
    monitor.shadow = FakeShadow(open_symbols={"AAPL"})
    fake_analyzer = FakeAnalyzer()
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert fake_analyzer.calls == []
    assert monitor._seen_article_ids == {1}  # still marked seen, just never billed


def test_no_shadow_trade_or_analyzer_call_when_market_is_closed(tmp_path):
    """A news hit for a symbol whose market is currently closed must not
    spend a Claude call or open a shadow trade at whatever price its last
    bar happened to close at -- that price could be hours stale (the bug
    this guards against: an EU symbol's shadow trade opened using its
    pre-close price, hours after that market had actually closed)."""
    articles = [{"id": 1, "headline": "headline"}]
    settings = make_settings(tmp_path, symbol_list=["ASML"])
    monitor = NewsMonitor(settings, bars=None, is_market_open=lambda symbol: False)
    monitor.finnhub = FakeFinnhub({"ASML": articles})
    monitor.shadow = FakeShadow()
    fake_analyzer = FakeAnalyzer()
    monitor.analyzer = fake_analyzer

    asyncio.run(monitor._poll_news())

    assert fake_analyzer.calls == []
    assert monitor._seen_article_ids == {1}  # still marked seen, just never billed

    lines = monitor._analysis_log_path.read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["direction"] is None
    assert "market closed" in record["skipped_reason"]


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
