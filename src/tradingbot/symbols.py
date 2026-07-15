"""Parses the MARKETS config DSL into per-symbol trading specs.

Format: "MARKET1:sym1,sym2@EXCHANGE@CCY,sym3;MARKET2:sym4,..."
  - MARKET must be one of tradingbot.markets.BUILTIN_MARKETS (case-insensitive)
  - each symbol is either a bare ticker (uses the market preset's default
    exchange/currency) or SYMBOL@EXCHANGE@CCY to override both, e.g. for a
    different exchange within the same region.

Example:
  MARKETS=US:AAPL,MSFT,NVDA;EU:SAP.DE,ASML.AS@AEB@EUR;ASIA:0700.HK;CRYPTO:BTC,ETH
"""
from __future__ import annotations

from dataclasses import dataclass

from tradingbot.markets import BUILTIN_MARKETS, MarketPreset


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    market: str
    exchange: str
    currency: str
    security_type: str


def parse_markets_dsl(value: str) -> list[SymbolSpec]:
    specs: list[SymbolSpec] = []
    seen_symbols: set[str] = set()

    for segment in value.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if ":" not in segment:
            raise ValueError(
                f"Invalid MARKETS segment {segment!r}: expected 'MARKET:sym1,sym2,...'"
            )
        market_name, symbols_part = segment.split(":", 1)
        market_name = market_name.strip().upper()
        preset = BUILTIN_MARKETS.get(market_name)
        if preset is None:
            raise ValueError(
                f"Unknown market {market_name!r} in MARKETS. Known markets: "
                f"{', '.join(sorted(BUILTIN_MARKETS))}"
            )
        for token in symbols_part.split(","):
            token = token.strip()
            if not token:
                continue
            spec = _parse_symbol_token(token, preset)
            if spec.symbol in seen_symbols:
                raise ValueError(f"Duplicate symbol {spec.symbol!r} in MARKETS")
            seen_symbols.add(spec.symbol)
            specs.append(spec)

    if not specs:
        raise ValueError("MARKETS must contain at least one symbol")
    return specs


def _parse_symbol_token(token: str, preset: MarketPreset) -> SymbolSpec:
    parts = token.split("@")
    if len(parts) == 1:
        symbol = parts[0]
        exchange, currency = preset.exchange, preset.currency
    elif len(parts) == 3:
        symbol, exchange, currency = parts
    else:
        raise ValueError(
            f"Invalid symbol token {token!r}: expected 'SYMBOL' or 'SYMBOL@EXCHANGE@CURRENCY'"
        )
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError(f"Invalid symbol token {token!r}: empty symbol")
    return SymbolSpec(
        symbol=symbol,
        market=preset.name,
        exchange=exchange.strip().upper(),
        currency=currency.strip().upper(),
        security_type=preset.security_type,
    )


def symbols_to_markets_dsl(symbols: str) -> str:
    """Backward-compat: turns the legacy comma-separated SYMBOLS field into
    an equivalent MARKETS DSL string, all assigned to the US market preset."""
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return "US:" + ",".join(tickers)
