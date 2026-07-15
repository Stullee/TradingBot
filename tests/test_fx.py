import asyncio

import pytest

from tradingbot.fx import FxConverter


class FakeTicker:
    def __init__(self, mid: float | None):
        self._mid = mid

    def midpoint(self) -> float | None:
        return self._mid


class FakeIB:
    def __init__(self, rates: dict[str, float]):
        self.rates = rates  # e.g. {"EURUSD": 1.10}
        self.calls: list[str] = []

    async def reqTickersAsync(self, contract):
        pair = contract.symbol + contract.currency
        self.calls.append(pair)
        return [FakeTicker(self.rates.get(pair))]


def run(coro):
    return asyncio.run(coro)


def test_same_currency_is_identity_and_makes_no_ib_call():
    ib = FakeIB({})
    fx = FxConverter(ib)
    result = run(fx.convert(100.0, "USD", "USD"))
    assert result == 100.0
    assert ib.calls == []


def test_direct_pair_conversion():
    ib = FakeIB({"EURUSD": 1.10})
    fx = FxConverter(ib)
    # 100 USD -> EUR at EURUSD=1.10 means 1 EUR = 1.10 USD, so 100 USD = 100/1.10 EUR...
    # but our convert(amount, from, to) uses rate(from,to) as a direct multiplier.
    rate = run(fx.rate("EUR", "USD"))
    assert rate == 1.10
    result = run(fx.convert(100.0, "EUR", "USD"))
    assert result == pytest.approx(110.0)


def test_inverse_pair_fallback():
    # Only EURUSD is quoted; asking for USD->EUR should fall back to 1/EURUSD.
    ib = FakeIB({"EURUSD": 1.25})
    fx = FxConverter(ib)
    rate = run(fx.rate("USD", "EUR"))
    assert rate == 0.8  # 1 / 1.25
    assert "USDEUR" in ib.calls
    assert "EURUSD" in ib.calls


def test_rate_is_cached_within_ttl():
    ib = FakeIB({"EURUSD": 1.10})
    fx = FxConverter(ib, cache_ttl_sec=60.0)
    run(fx.rate("EUR", "USD"))
    run(fx.rate("EUR", "USD"))
    assert ib.calls == ["EURUSD"]  # second call served from cache, no new IB request


def test_missing_rate_raises():
    ib = FakeIB({})
    fx = FxConverter(ib)
    try:
        run(fx.rate("EUR", "JPY"))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "EUR->JPY" in str(exc)
