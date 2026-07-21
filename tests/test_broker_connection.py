import asyncio
import json

from ib_async import Stock

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings


def make_broker() -> BrokerConnection:
    return BrokerConnection(Settings(ib_port=7497, symbols="AAPL"))


def run(coro):
    return asyncio.run(coro)


def test_disconnected_does_not_spawn_a_second_overlapping_reconnect_loop():
    """The bug this guards against: ib_async's disconnectedEvent firing a
    second time before the first reconnect attempt has finished (confirmed
    live: a reconnect that succeeds only to immediately get kicked with
    "clientId 17 already in use?" seconds later) used to spawn a second
    connect_with_retry() loop racing the first one to reconnect with the
    same clientId -- exactly the kind of collision that message describes."""
    broker = make_broker()
    call_count = 0
    release = asyncio.Event()

    async def fake_connect_with_retry(max_delay=60):
        nonlocal call_count
        call_count += 1
        await release.wait()

    broker.connect_with_retry = fake_connect_with_retry

    async def scenario():
        broker._on_disconnected()
        await asyncio.sleep(0)  # let the first reconnect task start running
        broker._on_disconnected()  # fires again before it has resolved
        await asyncio.sleep(0)
        assert call_count == 1  # second call was a no-op, not a new task

        release.set()
        await broker._reconnect_task  # let the first attempt finish

        broker._on_disconnected()  # first loop is done -- safe to spawn a new one
        await asyncio.sleep(0)
        assert call_count == 2

    run(scenario())


def test_realized_pnl_survives_position_flattening_to_zero():
    """Reproduces the live bug: ib_async's own portfolio() cache drops a
    contract (and its realizedPNL) the instant updatePortfolio reports
    position=0 -- see Wrapper.updatePortfolio's `portfolioItems.pop(...)`.
    BrokerConnection's own tracker must retain the last-known realizedPNL
    for that symbol instead of losing it the same way."""
    broker = make_broker()
    contract = Stock(symbol="WMT", exchange="NASDAQ", currency="USD", conId=13824)

    # Position open: some unrealized P&L, nothing realized yet.
    broker.ib.wrapper.updatePortfolio(
        contract, 10.0, 116.0, 1160.0, 115.0, 10.0, 0.0, "DU123"
    )
    assert any(p.contract.symbol == "WMT" for p in broker.ib.portfolio())

    # Position closed out entirely -- IB's real final update carries
    # position=0 and the realized P&L for the whole round trip.
    broker.ib.wrapper.updatePortfolio(
        contract, 0.0, 115.39, 0.0, 0.0, 0.0, -577.95, "DU123"
    )

    # ib_async's own cache has already dropped it (this is the bug this
    # tracker exists to work around) ...
    assert not any(p.contract.symbol == "WMT" for p in broker.ib.portfolio())
    # ... but the broker's own tracker still has it.
    assert broker.realized_pnl_by_symbol["WMT"] == -577.95


def test_realized_pnl_tracks_multiple_symbols_independently():
    broker = make_broker()
    wmt = Stock(symbol="WMT", exchange="NASDAQ", currency="USD", conId=1)
    sap = Stock(symbol="SAP", exchange="IBIS", currency="EUR", conId=2)

    broker.ib.wrapper.updatePortfolio(wmt, 0.0, 115.0, 0.0, 0.0, 0.0, -577.95, "DU123")
    broker.ib.wrapper.updatePortfolio(sap, 0.0, 140.0, 0.0, 0.0, 0.0, 659.41, "DU123")

    assert broker.realized_pnl_by_symbol == {"WMT": -577.95, "SAP": 659.41}


def test_realized_pnl_persists_and_survives_a_restart(tmp_path):
    path = tmp_path / "realized_pnl.json"
    broker = make_broker()
    broker.enable_realized_pnl_persistence(path)

    wmt = Stock(symbol="WMT", exchange="NASDAQ", currency="USD", conId=1)
    broker.ib.wrapper.updatePortfolio(wmt, 0.0, 115.0, 0.0, 0.0, 0.0, -577.95, "DU123")
    assert path.exists()

    restarted = make_broker()
    restarted.enable_realized_pnl_persistence(path)
    assert restarted.realized_pnl_by_symbol == {"WMT": -577.95}


def test_realized_pnl_not_persisted_unless_explicitly_enabled(tmp_path):
    # A one-off connection (CLI report, backtester) must not write to a
    # shared state file just by receiving the account's live
    # updatePortfolio events -- only enable_realized_pnl_persistence() opts
    # a connection into writing, so it can't clobber the live engine's file.
    path = tmp_path / "realized_pnl.json"
    broker = make_broker()

    wmt = Stock(symbol="WMT", exchange="NASDAQ", currency="USD", conId=1)
    broker.ib.wrapper.updatePortfolio(wmt, 0.0, 115.0, 0.0, 0.0, 0.0, -577.95, "DU123")

    assert broker.realized_pnl_by_symbol == {"WMT": -577.95}  # still tracked in-memory
    assert not path.exists()  # but nothing written without opting in


def test_enable_persistence_merges_loaded_state_with_live_updates(tmp_path):
    path = tmp_path / "realized_pnl.json"
    path.write_text('{"SAP": 659.41}')

    broker = make_broker()
    broker.enable_realized_pnl_persistence(path)
    assert broker.realized_pnl_by_symbol == {"SAP": 659.41}

    wmt = Stock(symbol="WMT", exchange="NASDAQ", currency="USD", conId=1)
    broker.ib.wrapper.updatePortfolio(wmt, 0.0, 115.0, 0.0, 0.0, 0.0, -577.95, "DU123")

    assert broker.realized_pnl_by_symbol == {"SAP": 659.41, "WMT": -577.95}
    assert json.loads(path.read_text()) == {"SAP": 659.41, "WMT": -577.95}


def test_enable_persistence_ignores_corrupt_file(tmp_path):
    path = tmp_path / "realized_pnl.json"
    path.write_text("not json")

    broker = make_broker()
    broker.enable_realized_pnl_persistence(path)  # should not raise
    assert broker.realized_pnl_by_symbol == {}


def test_critical_error_hook_fires_only_for_our_own_orders():
    """Confirmed live: error 200 from the FxConverter's USDEUR
    qualification probe (a nonexistent pair, probed by design) was reported
    as 'IB order error 200' -- the critical-code set overlaps codes IB also
    uses for non-order requests, so only a reqId matching one of our
    submitted orders' ids may escalate."""
    from types import SimpleNamespace

    from tradingbot.broker.connection import BrokerConnection
    from tradingbot.config import Settings

    broker = BrokerConnection(Settings(ib_port=7497, symbols="AAPL"))
    calls: list[tuple[int, int]] = []
    broker.on_critical_order_error = lambda req_id, code, msg: calls.append((req_id, code))
    broker.ib.trades = lambda: [SimpleNamespace(order=SimpleNamespace(orderId=221))]

    broker._on_ib_error(999, 200, "no security definition", None)  # FX probe -> ignored
    broker._on_ib_error(221, 200, "no security definition", None)  # our order -> fires
    broker._on_ib_error(221, 2104, "farm connection ok", None)  # benign code -> ignored

    assert calls == [(221, 200)]


def test_contract_meta_uses_market_rule_bands_for_the_routed_exchange():
    """The DBK incident's root cause: ContractDetails.minTick said 0.0005
    but TGATE's enforced tick at EUR30 is 0.005 -- the market rule (price
    bands), selected positionally by exchange, is the authority."""
    import asyncio
    from types import SimpleNamespace

    from ib_async import PriceIncrement

    from tradingbot.broker.connection import BrokerConnection
    from tradingbot.config import Settings

    broker = BrokerConnection(Settings(ib_port=7497, symbols="AAPL"))
    details = SimpleNamespace(
        minTick=0.0005, sizeIncrement=0.0, minSize=0.0,
        marketRuleIds="26,1707", validExchanges="SMART,TGATE",
    )
    requested_rules = []

    async def fake_details(contract):
        return [details]

    async def fake_rule(rule_id):
        requested_rules.append(rule_id)
        return [PriceIncrement(0.0, 0.001), PriceIncrement(10.0, 0.005)]

    broker.ib.reqContractDetailsAsync = fake_details
    broker.ib.reqMarketRuleAsync = fake_rule
    contract = SimpleNamespace(symbol="DBK", exchange="TGATE", secType="STK")

    meta = asyncio.run(broker.contract_meta(contract))
    assert requested_rules == [1707]  # TGATE's rule, not SMART's
    assert meta.price_increments == ((0.0, 0.001), (10.0, 0.005))
    assert meta.tick_for(30.59) == 0.005


def test_qualify_contract_trusts_ib_even_when_it_picks_a_different_venue():
    """Confirmed live (2026-07-21): qualifying SAP/MBG/VOW3/BMW/etc. on
    exchange="IBIS" sometimes comes back from IB with exchange="TGATE"
    instead (an alternate German MTF sharing the same conId). This was
    first read as IB wrongly overriding an explicit choice and pinned back
    to IBIS -- but the account's actual data subscription turned out to
    cover Tradegate specifically, with no separate Xetra line item, so the
    pin may have been forcing the wrong venue. Since which venue is truly
    entitled can't be confirmed here, qualify_contract must not guess in
    either direction -- it returns whatever IB itself resolved to."""
    broker = make_broker()

    async def fake_qualify(contract):
        return [Stock(symbol=contract.symbol, exchange="TGATE",
                       currency=contract.currency, conId=14204)]

    broker.ib.qualifyContractsAsync = fake_qualify
    qualified = run(broker.qualify_contract("STK", "SAP", "IBIS", "EUR"))
    assert qualified.exchange == "TGATE"


def test_qualify_contract_leaves_smart_routing_alone():
    """SMART is a deliberate "let IB pick" request (used for US symbols) --
    IB choosing a specific primaryExchange under it is expected, not a bug
    to correct."""
    broker = make_broker()

    async def fake_qualify(contract):
        return [Stock(symbol=contract.symbol, exchange="SMART",
                       primaryExchange="NASDAQ", currency=contract.currency, conId=1)]

    broker.ib.qualifyContractsAsync = fake_qualify
    qualified = run(broker.qualify_contract("STK", "AAPL", "SMART", "USD"))
    assert qualified.exchange == "SMART"
    assert qualified.primaryExchange == "NASDAQ"


def test_contract_meta_stock_fallback_clamps_min_tick_to_a_cent():
    """With no market rule available, the reported sub-cent minTick must
    not produce sub-tick prices again -- stocks floor at 0.01."""
    import asyncio
    from types import SimpleNamespace

    from tradingbot.broker.connection import BrokerConnection
    from tradingbot.config import Settings

    broker = BrokerConnection(Settings(ib_port=7497, symbols="AAPL"))
    details = SimpleNamespace(
        minTick=0.0005, sizeIncrement=0.0, minSize=0.0,
        marketRuleIds="", validExchanges="",
    )

    async def fake_details(contract):
        return [details]

    broker.ib.reqContractDetailsAsync = fake_details
    contract = SimpleNamespace(symbol="DBK", exchange="TGATE", secType="STK")

    meta = asyncio.run(broker.contract_meta(contract))
    assert meta.min_tick == 0.01
    assert meta.price_increments == ()
