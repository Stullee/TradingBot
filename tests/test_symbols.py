import pytest

from tradingbot.symbols import parse_markets_dsl, symbols_to_markets_dsl


def test_bare_symbol_uses_market_defaults():
    specs = parse_markets_dsl("US:AAPL,MSFT")
    assert [s.symbol for s in specs] == ["AAPL", "MSFT"]
    assert all(s.market == "US" and s.exchange == "SMART" and s.currency == "USD" for s in specs)
    assert all(s.security_type == "STK" for s in specs)


def test_symbol_override_exchange_and_currency():
    specs = parse_markets_dsl("EU:ASML.AS@AEB@EUR")
    assert len(specs) == 1
    spec = specs[0]
    assert spec.symbol == "ASML.AS"
    assert spec.exchange == "AEB"
    assert spec.currency == "EUR"
    assert spec.market == "EU"


def test_multiple_markets_and_crypto():
    specs = parse_markets_dsl("US:AAPL;ASIA:0700.HK;CRYPTO:BTC,ETH")
    by_symbol = {s.symbol: s for s in specs}
    assert by_symbol["AAPL"].market == "US"
    assert by_symbol["0700.HK"].market == "ASIA"
    assert by_symbol["0700.HK"].currency == "HKD"
    assert by_symbol["BTC"].security_type == "CRYPTO"
    assert by_symbol["ETH"].security_type == "CRYPTO"


def test_lowercase_market_name_is_normalized():
    specs = parse_markets_dsl("us:aapl")
    assert specs[0].market == "US"
    assert specs[0].symbol == "AAPL"


def test_unknown_market_raises():
    with pytest.raises(ValueError, match="Unknown market"):
        parse_markets_dsl("MARS:AAPL")


def test_duplicate_symbol_raises():
    with pytest.raises(ValueError, match="Duplicate symbol"):
        parse_markets_dsl("US:AAPL;EU:AAPL")


def test_segment_without_colon_raises():
    with pytest.raises(ValueError, match="Invalid MARKETS segment"):
        parse_markets_dsl("US-AAPL")


def test_bad_symbol_token_raises():
    with pytest.raises(ValueError, match="Invalid symbol token"):
        parse_markets_dsl("US:AAPL@ONLYONE")


def test_empty_value_raises():
    with pytest.raises(ValueError, match="at least one symbol"):
        parse_markets_dsl("")


def test_legacy_symbols_to_markets_dsl_round_trips():
    dsl = symbols_to_markets_dsl("aapl, msft ,nvda")
    specs = parse_markets_dsl(dsl)
    assert [s.symbol for s in specs] == ["AAPL", "MSFT", "NVDA"]
    assert all(s.market == "US" for s in specs)
