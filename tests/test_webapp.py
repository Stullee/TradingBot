import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from tradingbot.status import AccountStatus, PositionStatus
from tradingbot.webapp import create_app


class StubBroker:
    """create_app never calls broker methods directly -- only
    status.gather_account_status(broker) does, and that's monkeypatched out
    in these tests -- so this just needs to exist as a placeholder."""


def make_settings(tmp_path):
    return SimpleNamespace(log_dir=str(tmp_path))


def run(coro):
    return asyncio.run(coro)


def test_index_serves_html(tmp_path):
    async def scenario():
        app = create_app(StubBroker(), make_settings(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/")
            assert resp.status == 200
            assert "text/html" in resp.headers["Content-Type"]
            body = await resp.text()
            assert "TradingBot Status" in body
        finally:
            await client.close()

    run(scenario())


def test_status_endpoint_returns_expected_json_shape(tmp_path, monkeypatch):
    async def fake_gather_account_status(broker, include_fills=True):
        return AccountStatus(
            equity=100_000.0,
            base_currency="EUR",
            positions=[PositionStatus("AAPL", 10, 150.0, 25.0)],
            realized_pnl_by_symbol={"MSFT": 42.0},
        )

    monkeypatch.setattr("tradingbot.webapp.gather_account_status", fake_gather_account_status)

    async def scenario():
        app = create_app(StubBroker(), make_settings(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["equity"] == 100_000.0
            assert data["base_currency"] == "EUR"
            assert data["positions"] == [
                {"symbol": "AAPL", "quantity": 10, "avg_cost": 150.0, "unrealized_pnl": 25.0}
            ]
            assert data["realized_pnl_by_symbol"] == {"MSFT": 42.0}
            assert data["shadow"]["closed"] == 0
            assert data["news"]["assessed"] == 0
        finally:
            await client.close()

    run(scenario())


def test_status_endpoint_returns_500_json_on_failure(tmp_path, monkeypatch):
    async def failing_gather_account_status(broker, include_fills=True):
        raise RuntimeError("IB not connected")

    monkeypatch.setattr("tradingbot.webapp.gather_account_status", failing_gather_account_status)

    async def scenario():
        app = create_app(StubBroker(), make_settings(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/status")
            assert resp.status == 500
            data = await resp.json()
            assert "IB not connected" in data["error"]
        finally:
            await client.close()

    run(scenario())
