"""Trading engine: ties together data, strategy, risk and execution into the
main day-trading loop. Never holds positions overnight."""
from __future__ import annotations

import asyncio
import logging

from ib_async import Contract

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings
from tradingbot.data.bars import BarStream
from tradingbot.data.indicators import add_indicators
from tradingbot.execution.order_manager import OrderManager
from tradingbot.market_hours import MarketHours
from tradingbot.news.monitor import NewsMonitor
from tradingbot.risk.manager import RiskManager
from tradingbot.strategy.base import Signal, Strategy
from tradingbot.strategy.ema_rsi_momentum import EmaRsiVwapMomentum

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10


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
        self.market_hours = MarketHours(
            settings.market_open,
            settings.market_close,
            settings.no_new_entries_before_close_min,
            settings.flatten_before_close_min,
        )
        self.contracts: dict[str, Contract] = {}
        self._bar_counts: dict[str, int] = {}
        self._flattened_for_day = False
        self.news_monitor: NewsMonitor | None = None

    async def start(self) -> None:
        await self.broker.connect_with_retry()
        self.bars = BarStream(self.broker.ib, self.settings.bar_size)
        self.orders = OrderManager(self.broker.ib)

        for symbol in self.settings.symbol_list:
            contract = await self.broker.qualify_stock(symbol)
            self.contracts[symbol] = contract
            await self.bars.subscribe(symbol, contract)
            self._bar_counts[symbol] = 0

        await asyncio.sleep(2)  # let the first snapshot of bars arrive
        equity = self.broker.account_net_liquidation()
        self.risk.start_new_session(equity)

        if self.settings.enable_news_monitor:
            self.news_monitor = NewsMonitor(self.settings, self.bars)
            log.info("News sentiment shadow-trading enabled (observation only).")

    async def run_forever(self) -> None:
        await self.start()
        log.info("Trading engine running. Symbols=%s", self.settings.symbol_list)
        try:
            while True:
                await self._tick()
                if self.news_monitor is not None:
                    await self.news_monitor.tick()
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

    def _position_qty(self, contract: Contract) -> float:
        for p in self.broker.ib.positions():
            if p.contract.conId == contract.conId:
                return p.position
        return 0.0

    async def _tick(self) -> None:
        if not self.market_hours.is_market_open():
            return

        equity = self.broker.account_net_liquidation()

        if self.market_hours.should_flatten():
            if not self._flattened_for_day:
                self.orders.flatten_all()
                self._flattened_for_day = True
            return
        self._flattened_for_day = False

        kill_switch = self.risk.check_daily_loss_limit(equity)
        if kill_switch:
            self.orders.flatten_all()
            return

        stop_new_entries = self.market_hours.should_stop_new_entries()

        open_position_count = sum(
            1 for s in self.settings.symbol_list if self._position_qty(self.contracts[s]) != 0
        )

        for symbol in self.settings.symbol_list:
            await self._process_symbol(symbol, equity, open_position_count, stop_new_entries)

    async def _process_symbol(
        self, symbol: str, equity: float, open_position_count: int, stop_new_entries: bool
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

        quantity = self.risk.position_size(equity, entry_price, stop_price)
        if quantity <= 0:
            log.info("%s: signal %s but computed position size is 0, skipping.", symbol, signal)
            return

        log.info(
            "%s: %s signal -> %s %d shares @ ~%.2f, stop=%.2f, target=%.2f",
            symbol,
            signal,
            action,
            quantity,
            entry_price,
            stop_price,
            target_price,
        )
        self.orders.place_bracket(contract, action, quantity, stop_price, target_price)
