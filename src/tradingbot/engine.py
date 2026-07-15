"""Trading engine: ties together data, strategy, risk and execution into the
main trading loop across one or more markets (see tradingbot.markets).
Each symbol's market session independently governs when it trades and when
it gets force-flattened -- never holds positions past its own market's
session close (or, for always-open markets like crypto, past a daily
synthetic checkpoint)."""
from __future__ import annotations

import asyncio
import logging
import time

from ib_async import Contract

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings
from tradingbot.data.bars import BarStream
from tradingbot.data.indicators import add_indicators
from tradingbot.execution.order_manager import OrderManager
from tradingbot.fx import FxConverter
from tradingbot.market_hours import MarketSession
from tradingbot.markets import BUILTIN_MARKETS, build_session
from tradingbot.news.monitor import NewsMonitor
from tradingbot.risk.manager import RiskManager
from tradingbot.strategy.base import Signal, Strategy
from tradingbot.strategy.ema_rsi_momentum import EmaRsiVwapMomentum
from tradingbot.symbols import SymbolSpec

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10
POLLED_BARS_REFRESH_SEC = 60


class TradingEngine:
    def __init__(self, settings: Settings, strategy: Strategy | None = None):
        self.settings = settings
        self.broker = BrokerConnection(settings)
        self.strategy = strategy or EmaRsiVwapMomentum(
            ema_fast=settings.ema_fast,
            ema_slow=settings.ema_slow,
            rsi_period=settings.rsi_period,
            rsi_long_min=settings.rsi_long_min,
            rsi_long_max=settings.rsi_long_max,
            rsi_short_min=settings.rsi_short_min,
            rsi_short_max=settings.rsi_short_max,
            atr_period=settings.atr_period,
            allow_shorting=settings.allow_shorting,
        )
        self.risk = RiskManager(
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_concurrent_positions=settings.max_concurrent_positions,
            max_position_pct=settings.max_position_pct,
        )
        self.contracts: dict[str, Contract] = {}
        self._bar_counts: dict[str, int] = {}
        self.news_monitor: NewsMonitor | None = None
        self.base_currency = "USD"

        self.symbol_specs: list[SymbolSpec] = settings.symbol_specs
        self.spec_by_symbol: dict[str, SymbolSpec] = {s.symbol: s for s in self.symbol_specs}
        self.symbols_by_market: dict[str, list[str]] = {}
        for spec in self.symbol_specs:
            self.symbols_by_market.setdefault(spec.market, []).append(spec.symbol)

        self.market_sessions: dict[str, MarketSession] = {
            market_name: build_session(
                BUILTIN_MARKETS[market_name],
                settings.no_new_entries_before_close_min,
                settings.flatten_before_close_min,
            )
            for market_name in self.symbols_by_market
        }
        self._flattened_today: dict[str, bool] = {m: False for m in self.symbols_by_market}
        self._last_polled_refresh = 0.0

    async def start(self) -> None:
        await self.broker.connect_with_retry()
        self.bars = BarStream(self.broker.ib, self.settings.bar_size)
        self.orders = OrderManager(self.broker.ib)
        self.fx = FxConverter(self.broker.ib)

        for spec in list(self.symbol_specs):
            try:
                contract = await self.broker.qualify_contract(
                    spec.security_type, spec.symbol, spec.exchange, spec.currency
                )
                preset = BUILTIN_MARKETS[spec.market]
                is_crypto = spec.security_type == "CRYPTO"
                await self.bars.subscribe(
                    spec.symbol,
                    contract,
                    use_rth=not preset.outside_rth,
                    what_to_show="AGGTRADES" if is_crypto else "TRADES",
                    # IB rejects keepUpToDate=True ("live updates") for crypto
                    # contracts, so crypto bars are refreshed by polling instead
                    # (see engine._tick's periodic refresh_all_polled() call).
                    live_updates=not is_crypto,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not take down the rest
                log.error(
                    "Skipping %s (%s/%s/%s): %s. Fix and restart the bot to pick it back up.",
                    spec.symbol,
                    spec.market,
                    spec.exchange,
                    spec.currency,
                    exc,
                )
                self._drop_symbol(spec)
                continue

            self.contracts[spec.symbol] = contract
            self._bar_counts[spec.symbol] = 0

        if not self.contracts:
            raise RuntimeError(
                "No symbols could be qualified -- check the MARKETS/SYMBOLS config."
            )

        await asyncio.sleep(2)  # let the first snapshot of bars arrive
        equity = self.broker.account_net_liquidation()
        self.base_currency = self.broker.account_base_currency()
        self.risk.start_new_session(equity)

        if self.settings.enable_news_monitor:
            self.news_monitor = NewsMonitor(self.settings, self.bars)
            log.info("News sentiment shadow-trading enabled (observation only).")

    async def run_forever(self) -> None:
        await self.start()
        log.info(
            "Trading engine running. Markets=%s",
            {m: syms for m, syms in self.symbols_by_market.items()},
        )
        try:
            while True:
                try:
                    await self._tick()
                    if self.news_monitor is not None:
                        await self.news_monitor.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad tick must not kill the process
                    log.exception("Unhandled error in engine tick, continuing.")
                await asyncio.sleep(POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            log.info("Engine stopping...")
            raise
        finally:
            self.orders.flatten_all()
            self.bars.unsubscribe_all()
            self.broker.disconnect()
            if self.news_monitor is not None:
                await self.news_monitor.aclose()

    def _drop_symbol(self, spec: SymbolSpec) -> None:
        """Removes a symbol that failed to qualify from the active trading set,
        cleaning up its market/session entirely if it was the last symbol in it."""
        self.symbol_specs.remove(spec)
        self.spec_by_symbol.pop(spec.symbol, None)
        symbols = self.symbols_by_market.get(spec.market)
        if symbols and spec.symbol in symbols:
            symbols.remove(spec.symbol)
        if not symbols:
            self.symbols_by_market.pop(spec.market, None)
            self.market_sessions.pop(spec.market, None)
            self._flattened_today.pop(spec.market, None)

    def _position_qty(self, contract: Contract) -> float:
        for p in self.broker.ib.positions():
            if p.contract.conId == contract.conId:
                return p.position
        return 0.0

    async def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_polled_refresh >= POLLED_BARS_REFRESH_SEC:
            self._last_polled_refresh = now
            await self.bars.refresh_all_polled()

        equity = self.broker.account_net_liquidation()

        if self.risk.check_daily_loss_limit(equity):
            self.orders.flatten_all()
            return

        open_position_count = sum(
            1 for contract in self.contracts.values() if self._position_qty(contract) != 0
        )

        for market_name, session in self.market_sessions.items():
            await self._tick_market(market_name, session, equity, open_position_count)

    async def _tick_market(
        self, market_name: str, session: MarketSession, equity: float, open_position_count: int
    ) -> None:
        symbols = self.symbols_by_market[market_name]

        if session.should_flatten():
            if not self._flattened_today[market_name]:
                self.orders.flatten_contracts([self.contracts[s] for s in symbols])
                self._flattened_today[market_name] = True
            return
        self._flattened_today[market_name] = False

        if not session.is_open():
            return

        stop_new_entries = session.should_stop_new_entries()
        outside_rth = BUILTIN_MARKETS[market_name].outside_rth

        for symbol in symbols:
            await self._process_symbol(
                symbol, equity, open_position_count, stop_new_entries, outside_rth
            )

    async def _process_symbol(
        self,
        symbol: str,
        equity: float,
        open_position_count: int,
        stop_new_entries: bool,
        outside_rth: bool,
    ) -> None:
        contract = self.contracts[symbol]
        df = self.bars.dataframe(symbol)
        if df is None or df.empty:
            return

        bar_count = len(df)
        if bar_count <= self._bar_counts[symbol]:
            return  # still waiting for the current bar to close
        self._bar_counts[symbol] = bar_count

        closed = df.iloc[:-1]  # exclude the still-forming last bar
        if len(closed) < self.strategy.min_bars:
            return

        enriched = add_indicators(
            closed,
            self.settings.ema_fast,
            self.settings.ema_slow,
            self.settings.rsi_period,
            self.settings.atr_period,
        )

        position_qty = self._position_qty(contract)

        if position_qty != 0:
            if self.strategy.is_exit_signal(enriched, position_is_long=position_qty > 0):
                log.info("%s: exit signal (EMA cross reversal), flattening.", symbol)
                self.orders.flatten_position(contract, position_qty)
            return

        if stop_new_entries:
            return
        if not self.risk.can_open_new_position(open_position_count):
            return

        signal = self.strategy.generate_signal(enriched)
        if signal == Signal.FLAT:
            return

        last = enriched.iloc[-1]
        entry_price = float(last["close"])
        atr_value = float(last["atr"])
        stop_dist = atr_value * self.settings.stop_atr_mult
        target_dist = atr_value * self.settings.target_atr_mult

        if signal == Signal.LONG:
            stop_price = entry_price - stop_dist
            target_price = entry_price + target_dist
            action = "BUY"
        else:
            stop_price = entry_price + stop_dist
            target_price = entry_price - target_dist
            action = "SELL"

        spec = self.spec_by_symbol[symbol]
        equity_local = (
            await self.fx.convert(equity, self.base_currency, spec.currency)
            if spec.currency != self.base_currency
            else equity
        )
        quantity = self.risk.position_size(equity_local, entry_price, stop_price)
        if quantity <= 0:
            log.info("%s: signal %s but computed position size is 0, skipping.", symbol, signal)
            return

        log.info(
            "%s: %s signal -> %s %d shares @ ~%.2f %s, stop=%.2f, target=%.2f",
            symbol,
            signal,
            action,
            quantity,
            entry_price,
            spec.currency,
            stop_price,
            target_price,
        )
        self.orders.place_bracket(
            contract, action, quantity, stop_price, target_price, outside_rth=outside_rth
        )
