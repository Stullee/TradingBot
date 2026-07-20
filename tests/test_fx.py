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
    its first tick to arrive. `close` (default NaN) simulates the prior
    closing price IB still reports when the market itself is closed."""

    def __init__(self, mid: float | None, populate_after: int = 0, close: float = float("nan")):
        self._mid = mid
        self._populate_after = populate_after
        self._calls = 0
        self.contract = None
        self.close = close

    def midpoint(self) -> float | None:
        self._calls += 1
        return self._mid if self._calls > self._populate_after else None

    def marketPrice(self) -> float:
        return float("nan")


class FakeIB:
    def __init__(
        self,
        rates: dict[str, float],
        populate_after: dict[str, int] | None = None,
        closes: dict[str, float] | None = None,
    ):
        self.rates = rates  # e.g. {"EURUSD": 1.10}
        self.populate_after = populate_after or {}
        self.closes = closes or {}
        self.qualify_calls: list[str] = []
        self.mkt_data_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.market_data_type_calls: list[int] = []

    async def qualifyContractsAsync(self, contract):
        pair = contract.symbol + contract.currency
        self.qualify_calls.append(pair)
        if pair not in self.rates:
            return [None]
        return [FakeContract(contract.symbol, contract.currency)]

    def reqMktData(self, contract, *args, **kwargs):
        pair = contract.symbol + contract.currency
        self.mkt_data_calls.append(pair)
        ticker = FakeTicker(
            self.rates.get(pair),
            self.populate_after.get(pair, 0),
            close=self.closes.get(pair, float("nan")),
        )
        ticker.contract = contract
        return ticker

    def cancelMktData(self, contract):
        self.cancel_calls.append(contract.symbol + contract.currency)

    def reqMarketDataType(self, data_type: int) -> None:
        self.market_data_type_calls.append(data_type)


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


def test_falls_back_to_prior_close_when_no_live_quote():
    """Confirmed live: on a Sunday (forex market closed) a freshly
    subscribed EURUSD ticker never produced a midpoint, and the session's
    first entry signal was lost to 'Could not determine FX rate'. With no
    bid/ask, the prior close must be used -- plenty accurate for sizing."""
    ib = FakeIB({"EURUSD": None}, closes={"EURUSD": 1.08})
    fx = fast_fx(ib)
    assert run(fx.rate("EUR", "USD")) == 1.08
    # inverse direction works off the same closed-market ticker too
    assert run(fx.rate("USD", "EUR")) == pytest.approx(1 / 1.08)


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


def test_subscribing_toggles_delayed_data_only_around_the_reqmktdata_call():
    # reqMarketDataType is client-wide, not per-request -- it must be
    # switched to delayed(3) only for the reqMktData call itself (so
    # accounts without live FX entitlement still get a quote), then
    # switched straight back to live(1), so it doesn't leave every *other*
    # subsequent request on the connection (in particular the engine's
    # keepUpToDate equity bar streams) stuck on delayed data too.
    ib = FakeIB({"EURUSD": 1.10})
    fx = fast_fx(ib)
    run(fx.rate("EUR", "USD"))
    assert ib.market_data_type_calls == [3, 1]

    # Reusing an already-subscribed pair makes no further reqMktData call,
    # so it shouldn't touch the market data type setting again either.
    run(fx.rate("EUR", "USD"))
    assert ib.market_data_type_calls == [3, 1]


def test_unsubscribe_all_cancels_every_open_subscription():
    ib = FakeIB({"EURUSD": 1.10, "EURKRW": 1500.0})
    fx = fast_fx(ib)
    run(fx.rate("EUR", "USD"))
    run(fx.rate("EUR", "KRW"))
    fx.unsubscribe_all()
    assert set(ib.cancel_calls) == {"EURUSD", "EURKRW"}


def test_stale_rate_fallback_survives_a_restart(tmp_path):
    """Confirmed live: a post-restart EURUSD subscription received no ticks
    all afternoon and every US entry was deferred for hours -- the
    morning's persisted rate must size them instead."""
    cache = tmp_path / "fx_rates.json"
    fx_live = FxConverter(
        FakeIB({"EURUSD": 1.10}), first_tick_attempts=1, first_tick_delay_sec=0,
        cache_path=cache,
    )
    assert run(fx_live.rate("EUR", "USD")) == 1.10  # live rate, persisted

    dead_ib = FakeIB({"EURUSD": None})  # qualifies, but the ticker never populates
    fx_restarted = FxConverter(
        dead_ib, first_tick_attempts=1, first_tick_delay_sec=0, cache_path=cache
    )
    assert run(fx_restarted.rate("EUR", "USD")) == 1.10  # stale fallback
    assert run(fx_restarted.rate("USD", "EUR")) == pytest.approx(1 / 1.10)


def test_nonexistent_pair_is_probed_only_once():
    """The USDEUR probe (IB lists only one direction) was re-fired on every
    signal, costing an error round trip each time."""
    ib = FakeIB({"EURUSD": 1.10})
    fx = fast_fx(ib)
    run(fx.rate("USD", "EUR"))
    run(fx.rate("USD", "EUR"))
    assert ib.qualify_calls.count("USDEUR") == 1
