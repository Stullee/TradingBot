import asyncio

import pytest

from tradingbot.fx import FxConverter


class FakeContract:
    def __init__(self, symbol: str, currency: str):
        self.symbol = symbol
        self.currency = currency
        self.conId = 1


class FakeTicker:
    """midpoint() returns None for the first `populate_after` calls, then
    `mid` -- simulates a freshly subscribed ticker that takes a moment for
    its first tick to arrive."""

    def __init__(self, mid: float | None, populate_after: int = 0):
        self._mid = mid
        self._populate_after = populate_after
        self._calls = 0
        self.contract = None

    def midpoint(self) -> float | None:
        self._calls += 1
        return self._mid if self._calls > self._populate_after else None


class FakeIB:
    def __init__(self, rates: dict[str, float], populate_after: dict[str, int] | None = None):
        self.rates = rates  # e.g. {"EURUSD": 1.10}
        self.populate_after = populate_after or {}
        self.qualify_calls: list[str] = []
        self.mkt_data_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def qualifyContractsAsync(self, contract):
        pair = contract.symbol + contract.currency
        self.qualify_calls.append(pair)
        if pair not in self.rates:
            return [None]
        return [FakeContract(contract.symbol, contract.currency)]

    def reqMktData(self, contract, *args, **kwargs):
        pair = contract.symbol + contract.currency
        self.mkt_data_calls.append(pair)
        ticker = FakeTicker(self.rates.get(pair), self.populate_after.get(pair, 0))
        ticker.contract = contract
        return ticker

    def cancelMktData(self, contract):
        self.cancel_calls.append(contract.symbol + contract.currency)


def run(coro):
    return asyncio.run(coro)


def fast_fx(ib: FakeIB) -> FxConverter:
    return FxConverter(ib, first_tick_attempts=5, first_tick_delay_sec=0)


def test_same_currency_is_identity_and_makes_no_ib_call():
    ib = FakeIB({})
    fx = fast_fx(ib)
    result = run(fx.convert(100.0, "USD", "USD"))
    assert result == 100.0
    assert ib.qualify_calls == []


def test_direct_pair_conversion():
    ib = FakeIB({"EURUSD": 1.10})
    fx = fast_fx(ib)
    rate = run(fx.rate("EUR", "USD"))
    assert rate == 1.10
    result = run(fx.convert(100.0, "EUR", "USD"))
    assert result == pytest.approx(110.0)


def test_inverse_pair_fallback():
    # Only EURUSD is quoted; asking for USD->EUR should fall back to 1/EURUSD.
    ib = FakeIB({"EURUSD": 1.25})
    fx = fast_fx(ib)
    rate = run(fx.rate("USD", "EUR"))
    assert rate == 0.8  # 1 / 1.25
    assert "USDEUR" in ib.qualify_calls
    assert "EURUSD" in ib.qualify_calls


def test_subscription_is_reused_not_requested_again():
    ib = FakeIB({"EURUSD": 1.10})
    fx = fast_fx(ib)
    run(fx.rate("EUR", "USD"))
    run(fx.rate("EUR", "USD"))
    assert ib.mkt_data_calls == ["EURUSD"]  # second call reused the live subscription


def test_missing_rate_raises():
    ib = FakeIB({})
    fx = fast_fx(ib)
    try:
        run(fx.rate("EUR", "JPY"))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "EUR->JPY" in str(exc)


def test_waits_for_first_tick_on_a_freshly_subscribed_pair():
    ib = FakeIB({"EURUSD": 1.10}, populate_after={"EURUSD": 2})
    fx = fast_fx(ib)
    rate = run(fx.rate("EUR", "USD"))
    assert rate == 1.10
    assert ib.mkt_data_calls == ["EURUSD"]  # still only one subscription


def test_unsubscribe_all_cancels_every_open_subscription():
    ib = FakeIB({"EURUSD": 1.10, "EURKRW": 1500.0})
    fx = fast_fx(ib)
    run(fx.rate("EUR", "USD"))
    run(fx.rate("EUR", "KRW"))
    fx.unsubscribe_all()
    assert set(ib.cancel_calls) == {"EURUSD", "EURKRW"}
