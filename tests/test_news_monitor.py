import asyncio
import json
import time
from types import SimpleNamespace

from tradingbot.news.monitor import NewsMonitor


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
    def __init__(self):
        self.calls: list[str] = []

    async def company_news(self, symbol):
        self.calls.append(symbol)
        return []


def test_no_seen_ids_file_yet_starts_empty(tmp_path):
    monitor = NewsMonitor(make_settings(tmp_path), bars=None)
    assert monitor._seen_article_ids == set()


def test_mark_seen_appends_one_json_line_per_id(tmp_path):
    monitor = NewsMonitor(make_settings(tmp_path), bars=None)
    monitor._mark_seen(1)
    monitor._mark_seen(2)

    lines = monitor._seen_ids_path.read_text().strip().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


def test_seen_ids_persist_across_restarts():
    """The bug this guards against: a restart used to forget every article
    it had already assessed, re-billing Claude for the same day's backlog
    on every single restart. A second NewsMonitor instance pointed at the
    same log_dir must inherit what the first one had already seen."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = make_settings(tmp_dir)
        monitor1 = NewsMonitor(settings, bars=None)
        monitor1._mark_seen(123)
        monitor1._mark_seen(456)

        monitor2 = NewsMonitor(settings, bars=None)  # simulates a restart
        assert monitor2._seen_article_ids == {123, 456}


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
