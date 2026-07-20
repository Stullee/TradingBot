from types import SimpleNamespace

from tradingbot.broker.connection import ContractMeta
from tradingbot.execution.order_manager import OrderManager, round_to_tick


class FakeTrade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        self.done = False

    def isDone(self):
        return self.done


class FakeIB:
    def __init__(self):
        self.trades: list[FakeTrade] = []
        self._next_id = 1

    def placeOrder(self, contract, order):
        if not getattr(order, "orderId", 0):
            order.orderId = self._next_id
            self._next_id += 1
        trade = FakeTrade(contract, order)
        self.trades.append(trade)
        return trade

    def openTrades(self):
        return [t for t in self.trades if not t.isDone()]

    def cancelOrder(self, order):
        for t in self.trades:
            if t.order is order:
                t.done = True

    def positions(self):
        return []


def make_contract(con_id=101, symbol="AAPL"):
    return SimpleNamespace(conId=con_id, symbol=symbol)


def test_round_to_tick_handles_banded_ticks():
    # HKEX band: a HK$380 stock ticks in 0.2 -- blanket 2-decimal rounding
    # produced invalid prices IB rejects.
    assert round_to_tick(380.13, 0.2) == 380.2
    assert round_to_tick(380.09, 0.2) == 380.0
    assert round_to_tick(50.123, 0.01) == 50.12
    assert round_to_tick(0.123456, 0.0001) == 0.1235
    # no float dust: exact multiples on the wire
    assert str(round_to_tick(380.13, 0.2)) == "380.2"


def test_bracket_prices_rounded_to_contract_tick():
    ib = FakeIB()
    om = OrderManager(ib)
    om.place_bracket(
        make_contract(), "BUY", 100, stop_price=379.93, target_price=381.07,
        meta=ContractMeta(min_tick=0.2),
    )
    stop = next(t.order for t in ib.trades if t.order.orderType == "STP")
    tp = next(t.order for t in ib.trades if t.order.orderType == "LMT")
    assert stop.auxPrice == 380.0
    assert tp.lmtPrice == 381.0


def test_tick_for_uses_the_price_band_not_the_min_tick():
    """ContractDetails.minTick is only the smallest theoretical tick; the
    enforced one comes from the market rule's price bands."""
    meta = ContractMeta(
        min_tick=0.0005,
        price_increments=((0.0, 0.001), (10.0, 0.005), (100.0, 0.02)),
    )
    assert meta.tick_for(5.0) == 0.001
    assert meta.tick_for(30.59) == 0.005
    assert meta.tick_for(240.0) == 0.02
    # no bands -> falls back to min_tick
    assert ContractMeta(min_tick=0.0005).tick_for(30.59) == 0.0005


def test_dbk_regression_children_rounded_to_band_tick():
    """The live incident: DBK@TGATE bracket children went out at 4 decimals
    (rounded only to the reported 0.0005 minTick) and both legs were
    rejected with error 110 -- the MiFID band tick at EUR30 is 0.005."""
    ib = FakeIB()
    om = OrderManager(ib)
    meta = ContractMeta(min_tick=0.0005, price_increments=((0.0, 0.005),))
    om.place_bracket(
        make_contract(symbol="DBK"), "SELL", 6510,
        stop_price=30.6391, target_price=30.4975, meta=meta,
    )
    stop = next(t.order for t in ib.trades if t.order.orderType == "STP")
    tp = next(t.order for t in ib.trades if t.order.orderType == "LMT")
    assert stop.auxPrice == 30.64
    assert tp.lmtPrice == 30.5


def test_entry_limit_price_makes_the_parent_a_limit_order():
    """The live SOL incident: IB rejects unit-denominated crypto market
    BUYs (error 10289 'You must set Cash Quantity') -- crypto entries go
    out as marketable limits in units instead, rounded to tick."""
    ib = FakeIB()
    om = OrderManager(ib)
    om.place_bracket(
        make_contract(symbol="SOL"), "BUY", 2988.87571185,
        stop_price=75.91, target_price=76.76,
        meta=ContractMeta(min_tick=0.01), entry_limit_price=76.4587,
    )
    parent = ib.trades[0].order
    assert parent.orderType == "LMT"
    assert parent.lmtPrice == 76.46
    # children unchanged: exact same unit quantity, TP limit + protective stop
    assert [t.order.orderType for t in ib.trades] == ["LMT", "LMT", "STP"]
    assert all(t.order.totalQuantity == 2988.87571185 for t in ib.trades)


def test_native_stop_false_places_only_entry_and_take_profit():
    """The live ETH incident: ZeroHash rejected the STP child with error
    387, cancelling the whole chain. Crypto brackets now carry only entry +
    take-profit; the stop is engine-managed."""
    ib = FakeIB()
    om = OrderManager(ib)
    om.place_bracket(
        make_contract(symbol="ETH"), "BUY", 121.82922826,
        stop_price=1864.10, target_price=1875.65,
        meta=ContractMeta(min_tick=0.01), entry_limit_price=1874.05, native_stop=False,
    )
    types = [t.order.orderType for t in ib.trades]
    assert types == ["LMT", "LMT"]  # marketable-limit entry + TP, no STP leg
    take_profit = ib.trades[1].order
    assert take_profit.transmit is True  # TP now carries the chain's transmit
    assert take_profit.parentId == ib.trades[0].order.orderId


def test_no_entry_limit_price_keeps_a_market_parent():
    ib = FakeIB()
    om = OrderManager(ib)
    om.place_bracket(make_contract(), "BUY", 10, stop_price=98.0, target_price=104.0)
    assert ib.trades[0].order.orderType == "MKT"


def test_stale_pending_entry_is_cancelled():
    """A parent whose children were rejected can sit non-done forever
    (confirmed live), blocking its symbol and occupying a position slot --
    cancel_stale_entries reclaims it."""
    ib = FakeIB()
    om = OrderManager(ib)
    contract = make_contract(con_id=8)
    om.place_bracket(contract, "BUY", 10, stop_price=98.0, target_price=104.0)
    assert om.has_pending_entry(8) is True

    om.cancel_stale_entries(max_age_sec=1e9)  # too young -> untouched
    assert om.has_pending_entry(8) is True
    om.cancel_stale_entries(max_age_sec=0.0)  # aged out -> cancelled
    assert om.has_pending_entry(8) is False


def test_pending_entry_tracked_until_done():
    ib = FakeIB()
    om = OrderManager(ib)
    contract = make_contract(con_id=7)
    assert om.has_pending_entry(7) is False

    parent_trade = om.place_bracket(contract, "BUY", 10, stop_price=98.0, target_price=104.0)
    assert om.has_pending_entry(7) is True
    assert om.pending_entry_conids() == {7}

    parent_trade.done = True  # entry filled (or cancelled)
    assert om.has_pending_entry(7) is False
    assert om.pending_entry_conids() == set()


def test_has_live_protective_stop_matches_closing_side_only():
    ib = FakeIB()
    om = OrderManager(ib)
    contract = make_contract(con_id=9)
    om.place_bracket(contract, "BUY", 10, stop_price=98.0, target_price=104.0)
    # long position: the SELL stop child protects it
    assert om.has_live_protective_stop(contract, position_qty=10) is True
    # a short position would need a BUY stop -- the SELL stop doesn't count
    assert om.has_live_protective_stop(contract, position_qty=-10) is False


def test_place_protective_stop_reattaches_missing_stop():
    ib = FakeIB()
    om = OrderManager(ib)
    contract = make_contract(con_id=5)
    assert om.has_live_protective_stop(contract, 10) is False
    om.place_protective_stop(
        contract, position_qty=10, stop_price=97.77, meta=ContractMeta(min_tick=0.05)
    )
    assert om.has_live_protective_stop(contract, 10) is True
    stop = ib.trades[-1].order
    assert stop.action == "SELL"
    assert stop.auxPrice == 97.75  # rounded to tick
    assert stop.totalQuantity == 10


def test_flatten_position_cancels_children_first():
    ib = FakeIB()
    om = OrderManager(ib)
    contract = make_contract(con_id=3)
    om.place_bracket(contract, "BUY", 10, stop_price=98.0, target_price=104.0)
    om.flatten_position(contract, 10)
    # all three bracket orders cancelled; the only live order is the closing MKT
    live = [t.order for t in ib.openTrades()]
    assert len(live) == 1
    assert live[0].orderType == "MKT"
    assert live[0].action == "SELL"
