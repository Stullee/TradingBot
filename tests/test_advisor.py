import asyncio
import json
from types import SimpleNamespace

from tradingbot.advisor import TradingAdvisor


class FakeMessages:
    def __init__(self, tool_input: dict):
        self.tool_input = tool_input
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", input=self.tool_input)
        return SimpleNamespace(content=[block])


def make_advisor(tmp_path, tool_input: dict) -> TradingAdvisor:
    advisor = TradingAdvisor(api_key="test", model="test-model", interval_min=60, log_dir=tmp_path)
    advisor._client = SimpleNamespace(
        messages=FakeMessages(tool_input), close=_async_noop
    )
    return advisor


async def _async_noop():
    return None


def run(coro):
    return asyncio.run(coro)


REPORT = {
    "health": "WARNING",
    "assessment": "Expectancy is negative over 40 trades; MSFT drives most losses.",
    "observations": ["MSFT: -4.2R over 12 trades", "commissions are 0.3R/trade of drag"],
    "recommendations": [
        {"title": "Disable MSFT", "detail": "Persistent trend-day losses.", "priority": "high"}
    ],
}


def test_report_is_parsed_persisted_and_exposed(tmp_path):
    advisor = make_advisor(tmp_path, REPORT)
    assert advisor.due is True
    report = run(advisor.maybe_run({"equity": 100_000}))
    assert report["health"] == "WARNING"
    assert advisor.latest_report["assessment"].startswith("Expectancy is negative")
    assert advisor.due is False  # interval gate armed

    lines = (tmp_path / "advisor_reports.jsonl").read_text().splitlines()
    persisted = json.loads(lines[-1])
    assert persisted["recommendations"][0]["title"] == "Disable MSFT"
    assert "generated_at" in persisted


def test_snapshot_is_sent_as_prompt_context(tmp_path):
    advisor = make_advisor(tmp_path, REPORT)
    run(advisor.maybe_run({"equity": 42_000, "live_trades": {"closed": 7}}))
    call = advisor._client.messages.calls[0]
    prompt = call["messages"][0]["content"]
    assert "42000" in prompt.replace(",", "")
    assert call["tool_choice"]["name"] == "report_trading_assessment"


def test_latest_report_reloads_from_disk(tmp_path):
    advisor = make_advisor(tmp_path, REPORT)
    run(advisor.maybe_run({}))
    # a fresh instance (restart) picks the last persisted report back up
    reborn = TradingAdvisor(api_key="test", model="test-model", interval_min=60, log_dir=tmp_path)
    assert reborn.latest_report is not None
    assert reborn.latest_report["health"] == "WARNING"


def test_missing_health_defaults_to_warning(tmp_path):
    """Confirmed live: a truncated tool call came back without the required
    'health' field ('health=None' in the log, blank badge on the dashboard).
    An incomplete report must degrade to WARNING, never to nothing."""
    incomplete = {k: v for k, v in REPORT.items() if k != "health"}
    advisor = make_advisor(tmp_path, incomplete)
    report = run(advisor.maybe_run({}))
    assert report["health"] == "WARNING"
    assert advisor.latest_report["health"] == "WARNING"


def test_degenerate_field_shapes_are_normalized(tmp_path):
    """Confirmed live: the model returned `observations` as one plain
    string; the dashboard's .map() over it threw and blanked the page.
    Every list field must be coerced to the shape consumers rely on."""
    weird = {
        "health": "OK",
        "assessment": "sample too small",
        "observations": "just one string",
        "recommendations": "collect more data",
    }
    advisor = make_advisor(tmp_path, weird)
    report = run(advisor.maybe_run({}))
    assert report["observations"] == ["just one string"]
    assert report["recommendations"] == [
        {"title": "collect more data", "detail": "", "priority": "medium"}
    ]


def test_malformed_persisted_report_is_normalized_on_load(tmp_path):
    """A bad report written by an earlier version must not keep breaking
    the dashboard after an upgrade -- normalization applies on load too."""
    (tmp_path / "advisor_reports.jsonl").write_text(
        json.dumps({"assessment": "a", "observations": "s", "recommendations": None}) + "\n"
    )
    advisor = TradingAdvisor(api_key="test", model="m", interval_min=60, log_dir=tmp_path)
    assert advisor.latest_report["health"] == "WARNING"
    assert advisor.latest_report["observations"] == ["s"]
    assert advisor.latest_report["recommendations"] == []


def test_failed_call_returns_none_and_rearms(tmp_path):
    advisor = TradingAdvisor(api_key="test", model="test-model", interval_min=60, log_dir=tmp_path)

    class ExplodingMessages:
        async def create(self, **kwargs):
            raise RuntimeError("api down")

    advisor._client = SimpleNamespace(messages=ExplodingMessages(), close=_async_noop)
    assert run(advisor.maybe_run({})) is None
    assert advisor.latest_report is None
