from types import SimpleNamespace

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
    om.place_bracket(make_contract(), "BUY", 100, stop_price=379.93, target_price=381.07, min_tick=0.2)
    stop = next(t.order for t in ib.trades if t.order.orderType == "STP")
    tp = next(t.order for t in ib.trades if t.order.orderType == "LMT")
    assert stop.auxPrice == 380.0
    assert tp.lmtPrice == 381.0


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
    om.place_protective_stop(contract, position_qty=10, stop_price=97.77, min_tick=0.05)
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
