import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from tradingbot.news.shadow_trade import ShadowTrade
from tradingbot.status import AccountStatus, PositionStatus
from tradingbot.webapp import create_app


class StubBroker:
    """create_app never calls broker methods directly -- only
    status.gather_account_status(broker) does, and that's monkeypatched out
    in these tests -- so this just needs to exist as a placeholder."""


def make_settings(tmp_path, symbol_list=None):
    return SimpleNamespace(log_dir=str(tmp_path), symbol_list=symbol_list or [])


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
            assert data["symbols"] == []
            assert data["open_shadow_trades"] == []
        finally:
            await client.close()

    run(scenario())


def test_status_endpoint_includes_per_symbol_signals_and_open_shadow_trades(tmp_path, monkeypatch):
    async def fake_gather_account_status(broker, include_fills=True):
        return AccountStatus(
            equity=100_000.0,
            base_currency="EUR",
            positions=[PositionStatus("MBG", 4336, 46.14, 121.15)],
            realized_pnl_by_symbol={},
        )

    monkeypatch.setattr("tradingbot.webapp.gather_account_status", fake_gather_account_status)

    class StubShadow:
        open_trades = {
            "AAPL": ShadowTrade(
                symbol="AAPL",
                direction="LONG",
                entry_price=150.0,
                stop_price=148.0,
                target_price=154.0,
                opened_at="2026-07-16T14:22:00+00:00",
                headline="Apple beats earnings",
                confidence=0.82,
                rationale="strong beat",
                last_price=151.0,
            )
        }

    class StubNewsMonitor:
        shadow = StubShadow()

    async def scenario():
        settings = make_settings(tmp_path, symbol_list=["MBG", "AAPL"])
        latest_signals = {
            "MBG": {"signal": "LONG", "rsi": 21.3, "close": 46.12, "vwap": 46.26, "updated_at": "t"}
        }
        app = create_app(StubBroker(), settings, latest_signals, StubNewsMonitor())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/status")
            data = await resp.json()

            mbg = next(s for s in data["symbols"] if s["symbol"] == "MBG")
            assert mbg["bar_signal"] == "LONG"
            assert mbg["rsi"] == 21.3
            assert mbg["position_qty"] == 4336
            assert mbg["unrealized_pnl"] == 121.15

            aapl = next(s for s in data["symbols"] if s["symbol"] == "AAPL")
            assert aapl["bar_signal"] is None  # no signal computed yet for this symbol
            assert aapl["position_qty"] == 0

            assert len(data["open_shadow_trades"]) == 1
            assert data["open_shadow_trades"][0]["symbol"] == "AAPL"
            assert data["open_shadow_trades"][0]["direction"] == "LONG"
            assert data["open_shadow_trades"][0]["last_price"] == 151.0
            assert data["open_shadow_trades"][0]["unrealized_r"] == 0.5  # (151-150)/(150-148)
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


def test_status_endpoint_times_out_instead_of_hanging_forever(tmp_path, monkeypatch):
    # A query that never completes must surface as a visible error on the
    # page, not an indefinite "Loading..." spinner -- confirmed live: the
    # page itself loaded fine but /api/status never once completed.
    monkeypatch.setattr("tradingbot.webapp._STATUS_TIMEOUT_SEC", 0.05)

    async def hanging_gather_account_status(broker, include_fills=True):
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled by the timeout")

    monkeypatch.setattr("tradingbot.webapp.gather_account_status", hanging_gather_account_status)

    async def scenario():
        app = create_app(StubBroker(), make_settings(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/status")
            assert resp.status == 504
            data = await resp.json()
            assert "timed out" in data["error"]
        finally:
            await client.close()

    run(scenario())
