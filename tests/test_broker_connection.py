import json

from ib_async import Stock

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings


def make_broker() -> BrokerConnection:
    return BrokerConnection(Settings(ib_port=7497, symbols="AAPL"))


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
