import asyncio

from tradingbot.alerts import AlertSender


class FakeResponse:
    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return FakeResponse()

    async def aclose(self):
        pass


def run(coro):
    return asyncio.run(coro)


def test_disabled_sender_is_a_noop():
    sender = AlertSender("")
    assert sender.enabled is False
    run(sender.send("key", "title", "message"))  # must not raise or post


def test_identical_alerts_are_rate_limited_per_key():
    sender = AlertSender("http://hook.invalid/alert", min_interval_sec=300)
    sender._client = FakeClient()

    async def scenario():
        await sender.send("kill-switch-daily", "t", "m")
        await sender.send("kill-switch-daily", "t", "m")  # same key, inside window
        await sender.send("naked-position-AAPL", "t2", "m2")  # different key

    run(scenario())
    posted_keys = [payload["key"] for _, payload in sender._client.posts]
    assert posted_keys == ["kill-switch-daily", "naked-position-AAPL"]


def test_payload_contains_title_message_and_timestamp():
    sender = AlertSender("http://hook.invalid/alert")
    sender._client = FakeClient()
    run(sender.send("k", "TradingBot: something", "details here"))
    _, payload = sender._client.posts[0]
    assert payload["title"] == "TradingBot: something"
    assert payload["message"] == "details here"
    assert "timestamp" in payload
