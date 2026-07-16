"""Built-in market presets: exchange/currency/session defaults that symbols
get assigned to via the MARKETS config DSL (see config.py). Kept as a small,
fixed set of well-known presets rather than a fully generic per-market
settings surface, to keep the configuration footprint sane -- if you need a
different exchange/timezone, edit this file directly.

Coverage note: even combining all four, this cannot be true 24/7/365
downtime-free trading -- every stock exchange closes on weekends, and there
remains a gap between the US close and Asia's next open. Crypto is the only
genuinely continuous market here."""
from __future__ import annotations

from dataclasses import dataclass

from tradingbot.market_hours import MarketSession


@dataclass(frozen=True)
class MarketPreset:
    name: str
    exchange: str
    currency: str
    security_type: str  # "STK" | "CRYPTO"
    timezone: str
    open_time: str | None
    close_time: str | None
    always_open: bool = False
    # Whether bars/orders should request data and allow fills outside the
    # open/close window -- used for US pre/post-market extended hours.
    outside_rth: bool = False


BUILTIN_MARKETS: dict[str, MarketPreset] = {
    "US": MarketPreset(
        name="US",
        exchange="SMART",
        currency="USD",
        security_type="STK",
        timezone="US/Eastern",
        # Regular trading hours only (9:30am-4pm ET) -- pre/post-market was
        # tried (outside_rth=True, 4am-8pm ET) but confirmed live to be where
        # the ~20min stale-bar lag consistently showed up: thin after-hours
        # liquidity means a "new" 5-min bar can genuinely take that long to
        # close, which useRTH=True historical data + regular-hours-only
        # is_open() now sidesteps entirely rather than fighting with a
        # bigger staleness threshold.
        open_time="09:30",
        close_time="16:00",
        outside_rth=False,
    ),
    "EU": MarketPreset(
        name="EU",
        exchange="IBIS",  # Xetra (Germany); override per-symbol for other EU exchanges
        currency="EUR",
        security_type="STK",
        timezone="Europe/Berlin",
        open_time="09:00",
        close_time="17:30",
    ),
    "ASIA": MarketPreset(
        name="ASIA",
        exchange="SEHK",  # Hong Kong; override per-symbol for e.g. Tokyo (TSEJ/JPY)
        currency="HKD",
        security_type="STK",
        timezone="Asia/Hong_Kong",
        open_time="09:30",
        close_time="16:00",
    ),
    "CRYPTO": MarketPreset(
        name="CRYPTO",
        # PAXOS is IB's crypto venue for US-regulated (IBKR LLC) accounts. EU
        # accounts (IBKR Ireland/EEA "Europe - Crypto-Assets" trading
        # permission) trade crypto via Zero Hash instead, confirmed against a
        # real account under the exchange code ZEROHASHE (not ZEROHASH) --
        # override per symbol if that's you, e.g.
        # "CRYPTO:BTC@ZEROHASHE@USD,ETH@ZEROHASHE@USD" (try @EUR instead of
        # @USD if that fails to qualify).
        exchange="PAXOS",
        currency="USD",
        security_type="CRYPTO",
        timezone="UTC",
        open_time=None,
        # Not a real close -- crypto trades 24/7. This is a synthetic daily
        # checkpoint so crypto positions still get force-flattened once a day
        # instead of riding indefinitely, keeping the same bounded-risk
        # discipline the rest of the bot relies on.
        close_time="23:55",
        always_open=True,
        outside_rth=True,
    ),
}


def build_session(
    preset: MarketPreset,
    no_new_entries_before_close_min: int,
    flatten_before_close_min: int,
) -> MarketSession:
    return MarketSession(
        timezone=preset.timezone,
        open_time=preset.open_time,
        close_time=preset.close_time,
        no_new_entries_before_close_min=no_new_entries_before_close_min,
        flatten_before_close_min=flatten_before_close_min,
        always_open=preset.always_open,
        trade_weekends=preset.always_open,
    )
