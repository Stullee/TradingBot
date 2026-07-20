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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ib_async import Contract

from tradingbot.advisor import TradingAdvisor
from tradingbot.alerts import AlertSender
from tradingbot.broker.connection import BrokerConnection, ContractMeta
from tradingbot.config import Settings
from tradingbot.data.bars import BarStream, parse_bar_size_seconds
from tradingbot.data.indicators import add_indicators, atr
from tradingbot.eod import append_eod_record, build_eod_summary, format_eod_alert, format_eod_text
from tradingbot.execution.order_manager import OrderManager
from tradingbot.execution.trade_journal import TradeJournal, summarize_live_trades
from tradingbot.fx import FxConverter
from tradingbot.market_hours import MarketSession
from tradingbot.markets import BUILTIN_MARKETS, build_session
from tradingbot.news.monitor import NewsMonitor
from tradingbot.risk.manager import RiskManager
from tradingbot.status import gather_shadow_trading_status
from tradingbot.strategy.base import Signal, Strategy
from tradingbot.strategy.ema_rsi_momentum import EmaRsiVwapMomentum
from tradingbot.strategy.trendline_breakout import TrendlineBreakout
from tradingbot.strategy.vwap_mean_reversion import VwapMeanReversion
from tradingbot.symbols import SymbolSpec
from tradingbot.webapp import run_dashboard

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 10
POLLED_BARS_REFRESH_SEC = 60
STALE_BAR_CHECK_SEC = 60
# How often to verify every open position still has a live protective stop
# (bracket children can die without the position: DAY orders expiring while
# a position survives an early close, a rejected child, a restart
# mid-bracket). A position without its stop is the exact unbounded-loss
# situation the bracket exists to prevent.
PROTECTION_CHECK_SEC = 60
# At least 3 bar intervals with no new bar (and at least 10 min regardless,
# for small bar sizes) before treating a live stream as stalled -- normal
# bar-close timing jitter shouldn't false-positive, but IB market data farm
# hiccups that silently stop delivering updates (confirmed live: no error
# logged, bars frozen 35+ minutes during regular market hours) should.
STALE_BAR_MIN_THRESHOLD_SEC = 600
# Minimum gap between resubscribe attempts for the *same* symbol. A
# resubscribe is a real reqHistoricalDataAsync call, subject to IB's
# historical-data pacing limit (~60 requests per rolling 10-minute window
# per connection -- see backtest/runner.py's docstring). Without a cooldown,
# a farm-wide outage that hits the whole US watchlist at once (confirmed
# live: 20 symbols going stale in the same tick, repeating every
# STALE_BAR_CHECK_SEC because resubscribing doesn't fix an upstream farm
# issue) re-fires that same 20-request burst every single check cycle --
# ~20/min, over 3x the pacing budget, continuously -- which appears to have
# been enough to choke the shared IB connection badly enough to make the
# live dashboard (same event loop, same connection) unreachable for
# minutes at a time. The cooldown bounds worst-case volume to roughly
# (watchlist size / cooldown) regardless of how long the outage lasts.
RESUBSCRIBE_COOLDOWN_SEC = 300
# Spaces out resubscribe calls *within* one check cycle too, so a mass-stale
# event doesn't fire its whole burst in under a second -- same pacing
# pattern already used for Finnhub polling (see news/monitor.py).
_RESUBSCRIBE_REQUEST_GAP_SEC = 1.0
# Crypto entries are marketable LIMIT orders (IB rejects unit-denominated
# crypto market buys -- see OrderManager.place_bracket), priced this %
# through the last close so they fill immediately in the normal case while
# bounding worst-case entry slippage to the same amount.
CRYPTO_ENTRY_LIMIT_BUFFER_PCT = 0.3


def build_strategy(settings: Settings) -> Strategy:
    """Constructs the Strategy selected by settings.strategy. Shared by the
    live engine and the backtester so both run the exact same logic."""
    if settings.strategy == "ema_rsi_momentum":
        return EmaRsiVwapMomentum(
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
    if settings.strategy == "trendline_breakout":
        return TrendlineBreakout(
            trend_window=settings.trend_window,
            min_r_squared=settings.min_r_squared,
            atr_period=settings.atr_period,
            allow_shorting=settings.allow_shorting,
            entry_max_dist_atr=settings.trend_entry_max_dist_atr,
        )
    return VwapMeanReversion(
        rsi_period=settings.rsi_period,
        rsi_oversold=settings.rsi_oversold,
        rsi_overbought=settings.rsi_overbought,
        atr_period=settings.atr_period,
        vwap_dist_atr_mult=settings.vwap_dist_atr_mult,
        trend_ema_period=settings.trend_ema_period,
        allow_shorting=settings.allow_shorting,
    )


class TradingEngine:
    def __init__(self, settings: Settings, strategy: Strategy | None = None):
        self.settings = settings
        self.broker = BrokerConnection(settings)
        self.strategy = strategy or build_strategy(settings)
        self.risk = RiskManager(
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_concurrent_positions=settings.max_concurrent_positions,
            max_position_pct=settings.max_position_pct,
            max_weekly_loss_pct=settings.max_weekly_loss_pct,
            max_open_risk_pct=settings.max_open_risk_pct,
        )
        self.contracts: dict[str, Contract] = {}
        self.contract_meta: dict[str, ContractMeta] = {}
        self.journal = TradeJournal(Path(settings.log_dir) / "trades.jsonl")
        self.alerts = AlertSender(settings.alert_webhook_url)
        self.advisor: TradingAdvisor | None = None
        if settings.enable_ai_advisor:
            self.advisor = TradingAdvisor(
                api_key=settings.anthropic_api_key,
                model=settings.advisor_model,
                interval_min=settings.advisor_interval_min,
                log_dir=Path(settings.log_dir),
            )
        # Entry-to-stop risk (% of equity) per symbol with an open or
        # in-flight position -- summed into the portfolio heat handed to
        # RiskManager.can_open_new_position. Pruned once flat.
        self._open_risk_pct: dict[str, float] = {}
        # Entries placed within the current tick, counted immediately --
        # ib.positions() lags fills, so without this every symbol signaling
        # in the same tick would see the same stale position count and blow
        # through max_concurrent_positions together.
        self._entries_this_tick = 0
        # Kill-switch flatten fires once per trip, not every 10s tick.
        self._kill_flatten_done = False
        self._last_protection_check = 0.0
        # The newest bar's own start-timestamp per symbol, last seen by
        # _process_symbol -- None means no bar has been processed yet. Used
        # to detect a genuinely new closed bar; see _process_symbol's
        # docstring comment for why this must be a timestamp, not a bar count.
        self._last_bar_start: dict[str, object] = {}
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
                entry_delay_after_open_min=settings.no_entries_after_open_min,
            )
            for market_name in self.symbols_by_market
        }
        self._flattened_today: dict[str, bool] = {m: False for m in self.symbols_by_market}
        # UTC calendar date the risk manager's daily-loss baseline was last
        # reset for -- see _tick()'s rollover check. Set for real in start();
        # None here is just a placeholder before that runs.
        self._trading_day: object = None
        self._last_polled_refresh = 0.0
        self._last_stale_check = 0.0
        self._last_resubscribe_attempt: dict[str, float] = {}
        # Per-symbol adaptive cooldown: doubles (capped at 1h) each time a
        # resubscribe fails to advance the symbol's bar clock -- delayed
        # feeds and farm outages aren't fixable by resubscribing, and the
        # fixed cooldown alone let 22 futile retries per 5 minutes saturate
        # the pacing budget all afternoon (confirmed live).
        self._resubscribe_cooldown: dict[str, float] = {}
        self._bar_at_last_resubscribe: dict[str, object] = {}
        self._dashboard_task: asyncio.Task | None = None
        # Latest bar-strategy indicator/signal snapshot per symbol, read
        # directly by the live dashboard (webapp.py) -- a plain shared dict
        # rather than a copy, so dashboard reads always see the current
        # values with no extra plumbing. Only touched from this event loop
        # (the tick loop writes, the dashboard's request handler reads),
        # so no locking is needed.
        self.latest_signals: dict[str, dict] = {}

    async def _run_dashboard_safely(self) -> None:
        try:
            await run_dashboard(
                self.broker, self.settings, self.latest_signals, self.news_monitor, self.advisor
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dashboard failure must never take down trading
            log.exception("Status dashboard failed to start or crashed; trading continues.")

    async def start(self) -> None:
        await self.broker.connect_with_retry()
        self.bars = BarStream(self.broker.ib, self.settings.bar_size)
        self.orders = OrderManager(self.broker.ib)
        self.fx = FxConverter(
            self.broker.ib, cache_path=Path(self.settings.log_dir) / "fx_rates.json"
        )

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
            self.contract_meta[spec.symbol] = await self.broker.contract_meta(contract)
            self._last_bar_start[spec.symbol] = None

        if not self.contracts:
            raise RuntimeError(
                "No symbols could be qualified -- check the MARKETS/SYMBOLS config."
            )

        await asyncio.sleep(2)  # let the first snapshot of bars arrive
        equity = self.broker.account_net_liquidation()
        self.base_currency = self.broker.account_base_currency()
        # Warm every FX pair the universe can need, now -- a signal-time
        # subscription gets 10 seconds to produce a rate; a startup one
        # gets the whole session (and the persisted last-known rate covers
        # the gap either way). Confirmed live: a cold post-restart EURUSD
        # ticker stayed empty all afternoon and deferred every US entry.
        needed_currencies = sorted(
            {spec.currency for spec in self.symbol_specs if spec.currency != self.base_currency}
        )
        if needed_currencies:
            await self.fx.warm_up([(self.base_currency, ccy) for ccy in needed_currencies])
        self._trading_day = datetime.now(timezone.utc).date()
        # Restore (not reset) today's loss baseline and any latched kill
        # switch if this is a same-day restart -- a restart must not grant a
        # fresh daily loss budget against already-depleted equity.
        self.risk.enable_persistence(Path(self.settings.log_dir) / "risk_state.json")
        self.risk.start_or_restore_session(equity, self._trading_day)
        self.broker.enable_realized_pnl_persistence(
            Path(self.settings.log_dir) / "realized_pnl.json"
        )

        self.journal.attach(self.broker.ib)
        self.journal.seed_positions(
            [
                (p.contract.symbol, p.position, p.avgCost, p.contract.currency)
                for p in self.broker.ib.positions()
                if p.position
            ]
        )
        self.broker.on_critical_order_error = lambda req_id, code, message: self.alerts.send_soon(
            f"order-error-{code}", "TradingBot: order error", f"IB error {code}: {message}"
        )

        if self.settings.enable_news_monitor:
            self.news_monitor = NewsMonitor(
                self.settings,
                self.bars,
                self._is_symbol_market_open,
                self._should_flatten_shadow_trade,
                symbols=list(self.contracts),
            )
            log.info("News sentiment shadow-trading enabled (observation only).")
        if self.advisor is not None:
            log.info(
                "AI advisor enabled (model=%s, every %d min) -- advisory only, no order authority.",
                self.settings.advisor_model,
                self.settings.advisor_interval_min,
            )

        if self.settings.enable_dashboard:
            self._dashboard_task = asyncio.create_task(self._run_dashboard_safely())

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
            # Give the flatten orders a moment to reach IB before the socket
            # closes -- disconnecting immediately can drop them unsent,
            # leaving positions open after a "clean" shutdown.
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                pass
            self.bars.unsubscribe_all()
            self.fx.unsubscribe_all()
            if self._dashboard_task is not None:
                self._dashboard_task.cancel()
            self.broker.disconnect()
            if self.news_monitor is not None:
                await self.news_monitor.aclose()
            if self.advisor is not None:
                await self.advisor.aclose()
            await self.alerts.aclose()

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

    def _is_symbol_market_open(self, symbol: str) -> bool:
        """Passed into NewsMonitor so it can skip opening a shadow trade (or
        even calling Claude) for a symbol whose market is currently closed --
        without this, a news hit outside trading hours would "enter" at
        whatever price its last bar happened to close at, hours stale, not a
        real opportunity."""
        spec = self.spec_by_symbol.get(symbol)
        if spec is None:
            return False
        session = self.market_sessions.get(spec.market)
        if session is None:
            return False
        return session.is_open()

    def _should_flatten_shadow_trade(self, symbol: str, opened_at: str) -> bool:
        """Passed into NewsMonitor so an open shadow trade gets force-closed
        with the same no-overnight-risk discipline real positions get via
        session.should_flatten(), instead of just sitting open until it
        happens to hit stop/target/timeout (confirmed live: trades still
        open from the previous day). Also catches a trade that already
        carried over from an earlier calendar day than this check existed --
        should_flatten() alone only fires once *today's* window is reached,
        which could be many hours away; a trade opened on a prior day (in
        its own market's local timezone) already should have been flattened
        at least once by now and just never was."""
        spec = self.spec_by_symbol.get(symbol)
        if spec is None:
            return True  # unknown session -- fail safe, don't hold indefinitely
        session = self.market_sessions.get(spec.market)
        if session is None:
            return True
        if session.should_flatten():
            return True
        opened_local_date = datetime.fromisoformat(opened_at).astimezone(session.tz).date()
        return opened_local_date < session.now_local().date()

    async def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_polled_refresh >= POLLED_BARS_REFRESH_SEC:
            self._last_polled_refresh = now
            await self.bars.refresh_all_polled()
            for symbol in self.contracts:
                self.bars.log_latest(symbol)

        if now - self._last_stale_check >= STALE_BAR_CHECK_SEC:
            self._last_stale_check = now
            await self._check_stale_bars()

        equity = self.broker.account_net_liquidation()

        # RiskManager.start_new_session() latches a starting-equity baseline
        # (and clears the kill switch) once, at connect time -- it never
        # re-fires on its own. Left alone, "daily" loss tracking silently
        # becomes "loss since this process last restarted": on a bot that
        # now deliberately stays up for many hours (crypto's warmup, riding
        # out IB reconnects) rather than restarting every day, that drifts
        # further from "today's" P&L the longer it runs, and a kill switch
        # tripped on day 1 would stay latched forever rather than clearing
        # for day 2. A plain UTC-date rollover is the one boundary that's
        # unambiguous across a bot spanning multiple markets/timezones at
        # once, unlike any single market's own local session close.
        today = datetime.now(timezone.utc).date()
        if today != self._trading_day:
            # Capture the finished day's baselines/switch state BEFORE the
            # session reset wipes them -- the EOD summary describes the day
            # that just ended, not the fresh one.
            finished_day = self._trading_day
            finished_day_start = self.risk.day_start_equity
            finished_week_start = self.risk.week_start_equity
            finished_daily_kill = self.risk.daily_kill_switch_active
            finished_weekly_kill = self.risk.weekly_kill_switch_active
            self._trading_day = today
            self.risk.start_new_session(equity, today)
            log.info("New UTC trading day (%s): risk manager session reset, equity=%.2f", today, equity)
            if finished_day is not None:
                self._emit_eod_summary(
                    finished_day,
                    equity,
                    finished_day_start,
                    finished_week_start,
                    finished_daily_kill,
                    finished_weekly_kill,
                )

        if self.risk.check_loss_limits(equity):
            # Flatten once per trip, not every 10s for the rest of the day.
            if not self._kill_flatten_done:
                self.orders.flatten_all()
                self._kill_flatten_done = True
                which = "weekly" if self.risk.weekly_kill_switch_active else "daily"
                self.alerts.send_soon(
                    f"kill-switch-{which}",
                    f"TradingBot: {which} loss limit breached",
                    f"The {which} loss kill switch is active. All positions flattened; "
                    "no new entries until it resets.",
                )
            return
        self._kill_flatten_done = False

        if now - self._last_protection_check >= PROTECTION_CHECK_SEC:
            self._last_protection_check = now
            self._ensure_protective_stops()
            self.orders.cancel_stale_entries()

        self._manage_crypto_exits()

        # Pending (placed but unfilled) entries count as occupied position
        # slots; their risk stays counted until the position is flat again.
        pending_conids = self.orders.pending_entry_conids()
        open_position_count = 0
        for symbol, contract in self.contracts.items():
            has_position = self._position_qty(contract) != 0
            if has_position or contract.conId in pending_conids:
                open_position_count += 1
            elif symbol in self._open_risk_pct:
                self._open_risk_pct.pop(symbol)
        self._entries_this_tick = 0

        for market_name, session in self.market_sessions.items():
            await self._tick_market(market_name, session, equity, open_position_count)

        if self.advisor is not None and self.advisor.due:
            report = await self.advisor.maybe_run(self._advisor_snapshot(equity))
            if report is not None and report.get("health") == "CRITICAL":
                self.alerts.send_soon(
                    "advisor-critical",
                    "TradingBot: AI advisor reports CRITICAL",
                    str(report.get("assessment", ""))[:800],
                )

    async def _tick_market(
        self, market_name: str, session: MarketSession, equity: float, open_position_count: int
    ) -> None:
        symbols = self.symbols_by_market[market_name]

        if session.should_flatten():
            if not self._flattened_today[market_name]:
                self.orders.flatten_contracts([self.contracts[s] for s in symbols])
                self._flattened_today[market_name] = True
            for symbol in symbols:
                self._set_gate(symbol, "flatten window / market closing")
            return
        self._flattened_today[market_name] = False

        if not session.is_open():
            for symbol in symbols:
                self._set_gate(symbol, "market closed")
            return

        if session.should_stop_new_entries():
            entry_gate = "close buffer (no new entries)"
        elif session.in_opening_delay():
            entry_gate = f"opening delay (first {self.settings.no_entries_after_open_min}m)"
        else:
            entry_gate = None
        outside_rth = BUILTIN_MARKETS[market_name].outside_rth

        for symbol in symbols:
            await self._process_symbol(
                symbol, equity, open_position_count, entry_gate, outside_rth
            )

    async def _check_stale_bars(self) -> None:
        """Live (keepUpToDate=True) bar streams can silently stop delivering
        updates without ever raising an error -- an IB market data farm
        hiccup doesn't always surface as one. Polled symbols (crypto)
        self-heal via their own periodic refresh and don't need this. A
        symbol whose market is open but hasn't produced a new bar in a
        while almost certainly has a stalled feed, not just a quiet market:
        real markets produce a new bar every bar_size, always.

        A resubscribe can't fix an upstream farm-wide outage (confirmed
        live: the whole US watchlist going stale together, staying stale
        across repeated resubscribe attempts) -- it can only make things
        worse by burning through IB's historical-data pacing budget on
        requests that were never going to help, which appears to have been
        enough to choke the shared connection and make the dashboard
        unreachable too. RESUBSCRIBE_COOLDOWN_SEC bounds how often we'll
        retry any one symbol regardless of how long it stays stale."""
        threshold_sec = max(parse_bar_size_seconds(self.settings.bar_size) * 3, STALE_BAR_MIN_THRESHOLD_SEC)
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()

        stale_symbols = []
        for symbol in list(self.contracts):
            if not self.bars.has_live_subscription(symbol):
                continue
            spec = self.spec_by_symbol.get(symbol)
            if spec is None:
                continue
            session = self.market_sessions.get(spec.market)
            if session is None or not session.is_open():
                continue

            latest = self.bars.latest_bar_time(symbol)
            # Age is measured from the later of the last bar and TODAY'S
            # session open: bars from the previous session aren't "stale"
            # minutes into a new one (confirmed live: a 22-symbol
            # resubscribe burst at the Monday US open blew IB's pacing
            # budget and wiped Friday's bars with empty responses). An
            # empty live subscription (latest is None) ages from the open
            # too -- that's the recovery path for exactly that wipe, which
            # previously had none.
            reference = latest
            open_dt = session.today_open()
            if open_dt is not None:
                open_utc = open_dt.astimezone(timezone.utc)
                reference = max(latest, open_utc) if latest is not None else open_utc
            if reference is None:
                continue
            age_sec = (now - reference).total_seconds()
            if age_sec <= threshold_sec:
                continue
            stale_symbols.append((symbol, age_sec))

        if not stale_symbols:
            return

        resubscribed = 0
        on_cooldown = 0
        for i, (symbol, age_sec) in enumerate(stale_symbols):
            last_attempt = self._last_resubscribe_attempt.get(symbol)
            cooldown = self._resubscribe_cooldown.get(symbol, RESUBSCRIBE_COOLDOWN_SEC)
            if last_attempt is not None and now_monotonic - last_attempt < cooldown:
                on_cooldown += 1
                continue

            # Did the previous attempt actually help? If the bar clock
            # hasn't advanced since, back off exponentially (cap 1h) --
            # the problem is upstream, not the subscription.
            latest_now = self.bars.latest_bar_time(symbol)
            if last_attempt is not None and self._bar_at_last_resubscribe.get(symbol) == latest_now:
                cooldown = min(cooldown * 2, 3600.0)
                self._resubscribe_cooldown[symbol] = cooldown
                log.info(
                    "%s: previous resubscribe didn't advance the bar clock -- backing "
                    "off to %.0f min between attempts.",
                    symbol,
                    cooldown / 60,
                )
            else:
                self._resubscribe_cooldown[symbol] = RESUBSCRIBE_COOLDOWN_SEC
            self._bar_at_last_resubscribe[symbol] = latest_now

            if resubscribed > 0:
                await asyncio.sleep(_RESUBSCRIBE_REQUEST_GAP_SEC)
            self._last_resubscribe_attempt[symbol] = now_monotonic
            log.warning(
                "%s: no new bar in %.0f min while its market is open -- the live data "
                "feed may have stalled. Re-subscribing.",
                symbol,
                age_sec / 60,
            )
            try:
                await self.bars.resubscribe_live(symbol)
            except Exception:  # noqa: BLE001 - one bad resubscribe must not stop the rest
                log.exception("Failed to re-subscribe stale bar stream for %s", symbol)
            resubscribed += 1

        if len(stale_symbols) > 1:
            log.warning(
                "%d symbols had a stalled bar feed at once (%s) -- likely one shared IB "
                "market data farm issue, not %d separate ones. Resubscribed %d, %d still "
                "on a %ds cooldown from a recent attempt.",
                len(stale_symbols),
                ", ".join(s for s, _ in stale_symbols),
                len(stale_symbols),
                resubscribed,
                on_cooldown,
                RESUBSCRIBE_COOLDOWN_SEC,
            )

    def _ensure_protective_stops(self) -> None:
        """A position must never sit without a live stop-loss. The bracket
        normally guarantees it, but the children can die while the position
        survives: DAY orders expiring after an early close kept the position
        overnight, a child rejected at an off-tick price, a restart
        mid-bracket, manual intervention in TWS. Re-attaches a stop at the
        entry's originally intended level (from the trade journal's entry
        context) or, failing that, at the current ATR-based distance."""
        for symbol, contract in self.contracts.items():
            qty = self._position_qty(contract)
            if qty == 0:
                continue
            spec = self.spec_by_symbol.get(symbol)
            if spec is not None and spec.security_type == "CRYPTO":
                # ZeroHash rejects stop orders (387) -- re-attaching one
                # here would just error-loop. Crypto positions are guarded
                # by _check_synthetic_crypto_stops instead.
                continue
            session = self.market_sessions.get(spec.market) if spec else None
            if session is None or not session.is_open():
                continue  # a stop can't trigger while the market is closed; rechecked at open
            if self.orders.has_live_protective_stop(contract, qty):
                continue
            if self.orders.has_pending_close(contract, qty):
                continue  # a flatten is already in flight

            stop_price = self.journal.stop_price_for(symbol)
            if stop_price is None:
                stop_price = self._atr_fallback_stop(symbol, qty)
            if stop_price is None:
                log.error(
                    "%s: position of %s has NO live stop and no price data to place one "
                    "-- will retry next check.",
                    symbol,
                    qty,
                )
                continue
            meta = self.contract_meta.get(symbol, ContractMeta())
            outside_rth = BUILTIN_MARKETS[spec.market].outside_rth if spec else False
            self.orders.place_protective_stop(
                contract, qty, stop_price, outside_rth=outside_rth, meta=meta
            )
            self.alerts.send_soon(
                f"naked-position-{symbol}",
                f"TradingBot: {symbol} position had no stop-loss",
                f"Re-attached a protective stop @ {stop_price:.4f} to the open "
                f"{symbol} position ({qty:+g}). Check how its original stop disappeared.",
            )

    def _manage_crypto_exits(self) -> None:
        """Software-side exits for crypto positions, every tick. ZeroHash
        supports neither stop orders (error 387) nor attached child orders
        (error 201), all confirmed live -- so a crypto entry goes out bare
        and this method provides both exit legs afterwards:

        1. Stop: the entry's recorded level (trade journal context,
           persisted across restarts) enforced in software -- flatten at
           market once last price crosses it. Reaction granularity is the
           crypto bar-polling interval (~60s), the honest price of the
           venue not supporting exchange-side stops. A position found
           without a recorded stop gets an ATR-based level fixed once.
        2. Take-profit: a standalone (non-child) limit at the recorded
           target, placed once the position actually exists and sized to
           the real filled quantity -- re-placed if missing (restart), and
           cancelled automatically by every flatten path."""
        for symbol, contract in self.contracts.items():
            spec = self.spec_by_symbol.get(symbol)
            if spec is None or spec.security_type != "CRYPTO":
                continue
            qty = self._position_qty(contract)
            if qty == 0:
                continue
            df = self.bars.dataframe(symbol)
            if df is None or df.empty:
                continue
            last_price = float(df["close"].iloc[-1])

            stop_level = self.journal.stop_price_for(symbol)
            if stop_level is None:
                stop_level = self._atr_fallback_stop(symbol, qty)
                if stop_level is None:
                    continue
                # Record it so the level is fixed (not re-derived from a
                # moving price, which would trail and never trigger) and
                # survives restarts like any other entry context. Target 0
                # = "no meaningful target" for a recovered position.
                self.journal.record_entry_context(
                    symbol,
                    direction="LONG" if qty > 0 else "SHORT",
                    entry_ref_price=last_price,
                    stop_price=stop_level,
                    target_price=0.0,
                    strategy="recovered",
                )
                log.warning(
                    "%s: crypto position had no recorded stop -- fixed a synthetic "
                    "stop at %.6f (ATR-based from current price).",
                    symbol,
                    stop_level,
                )

            if self.orders.has_pending_close(contract, qty):
                continue  # a flatten is already in flight

            hit = last_price <= stop_level if qty > 0 else last_price >= stop_level
            if hit:
                log.warning(
                    "%s: synthetic crypto stop hit (last=%.6f vs stop=%.6f) -- flattening.",
                    symbol,
                    last_price,
                    stop_level,
                )
                self.orders.flatten_position(contract, qty)
                continue

            target_level = self.journal.target_price_for(symbol)
            if (
                target_level
                and target_level > 0
                and not self.orders.has_live_take_profit(contract, qty)
            ):
                meta = self.contract_meta.get(symbol, ContractMeta())
                self.orders.place_standalone_take_profit(contract, qty, target_level, meta=meta)

    def _atr_fallback_stop(self, symbol: str, position_qty: float) -> float | None:
        """Stop level one configured ATR-multiple away from the latest close
        -- bounded protection from *now* when the original intended stop is
        unknown (e.g. context lost across a restart)."""
        df = self.bars.dataframe(symbol)
        if df is None or len(df) < self.settings.atr_period + 1:
            return None
        atr_value = float(atr(df, self.settings.atr_period).iloc[-1])
        last_close = float(df["close"].iloc[-1])
        if atr_value <= 0 or last_close <= 0:
            return None
        distance = atr_value * self.settings.stop_atr_mult
        return last_close - distance if position_qty > 0 else last_close + distance

    def _emit_eod_summary(
        self,
        day,
        equity: float,
        day_start_equity: float | None,
        week_start_equity: float | None,
        daily_kill: bool,
        weekly_kill: bool,
    ) -> None:
        """Fires once at the UTC day rollover (the 23:55 UTC crypto flatten
        checkpoint has already passed, so the finished day is complete):
        appends a structured record to logs/eod_reports.jsonl, logs the
        readable version, and pushes a one-paragraph digest to the alert
        webhook. Failures are swallowed -- a reporting problem must never
        break the trading tick."""
        try:
            summary = build_eod_summary(
                Path(self.settings.log_dir),
                day,
                equity=equity,
                base_currency=self.base_currency,
                day_start_equity=day_start_equity,
                week_start_equity=week_start_equity,
                daily_kill_switch=daily_kill,
                weekly_kill_switch=weekly_kill,
                advisor_assessment=(
                    self.advisor.latest_report.get("assessment")
                    if self.advisor is not None and self.advisor.latest_report
                    else None
                ),
            )
            append_eod_record(Path(self.settings.log_dir), summary)
            log.info("End-of-day summary:\n%s", format_eod_text(summary))
            self.alerts.send_soon(
                f"eod-{summary['day']}",
                f"TradingBot: end of day {summary['day']}",
                format_eod_alert(summary),
            )
        except Exception:  # noqa: BLE001 - reporting must never break trading
            log.exception("Failed to build/emit the end-of-day summary for %s", day)

    def _advisor_snapshot(self, equity: float) -> dict:
        """Compact, structured state handed to the AI advisor: everything a
        supervising analyst needs, nothing it could misread as an
        instruction channel. Numbers only -- the advisor never sees or
        touches order flow."""
        log_dir = Path(self.settings.log_dir)
        live_stats, recent_trades = summarize_live_trades(log_dir / "trades.jsonl")
        shadow = gather_shadow_trading_status(log_dir / "shadow_trades.jsonl")
        positions = [
            {"symbol": p.contract.symbol, "qty": p.position, "avg_cost": p.avgCost}
            for p in self.broker.ib.positions()
            if p.position
        ]
        return {
            "utc_time": datetime.now(timezone.utc).isoformat(),
            "equity": equity,
            "base_currency": self.base_currency,
            "risk": {
                "daily_kill_switch_active": self.risk.daily_kill_switch_active,
                "weekly_kill_switch_active": self.risk.weekly_kill_switch_active,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "max_weekly_loss_pct": self.settings.max_weekly_loss_pct,
                "open_risk_pct": round(sum(self._open_risk_pct.values()), 3),
                "max_open_risk_pct": self.settings.max_open_risk_pct,
            },
            "live_trades": asdict(live_stats),
            "recent_round_trips": recent_trades[-5:],
            "shadow_trading": asdict(shadow),
            "open_positions": positions,
            "config": {
                "strategy": self.settings.strategy,
                "bar_size": self.settings.bar_size,
                "risk_per_trade_pct": self.settings.risk_per_trade_pct,
                "stop_atr_mult": self.settings.stop_atr_mult,
                "target_atr_mult": self.settings.target_atr_mult,
                "allow_shorting": self.settings.allow_shorting,
                "markets": {m: syms for m, syms in self.symbols_by_market.items()},
            },
            "latest_bar_signals": self.latest_signals,
        }

    def _set_gate(self, symbol: str, gate: str) -> None:
        """Publishes the reason this symbol isn't entering right now ("" =
        an entry was just placed) into the dashboard's per-symbol row --
        the 'why aren't we trading' indicator. Values persist between bars,
        so the row always shows the outcome of the latest decision."""
        self.latest_signals.setdefault(symbol, {})["gate"] = gate

    async def _process_symbol(
        self,
        symbol: str,
        equity: float,
        open_position_count: int,
        entry_gate: str | None,
        outside_rth: bool,
    ) -> None:
        contract = self.contracts[symbol]
        spec = self.spec_by_symbol[symbol]
        df = self.bars.dataframe(symbol)
        if df is None or df.empty:
            return

        # Keyed off the newest bar's own timestamp, not len(df) -- a
        # resubscribe/refresh (bars.resubscribe_live, refresh_polled) replaces
        # the whole bar list wholesale rather than appending to it, so its
        # length can just as easily go *down* as up (confirmed: a fresh "1 D"
        # pull right at a session's open returns far fewer bars than the
        # prior session's full count). Comparing lengths meant that dip
        # looked identical to "no new bar yet" forever after -- this symbol
        # would silently stop getting new signals until its bar count
        # organically regrew past the old high-water mark, with no error or
        # warning logged anywhere. A timestamp only ever moves forward in
        # real time regardless of how the underlying list was rebuilt.
        latest_bar_start = df.index[-1]
        last_seen = self._last_bar_start[symbol]
        if last_seen is not None and latest_bar_start <= last_seen:
            return  # still waiting for the current bar to close
        self._last_bar_start[symbol] = latest_bar_start

        closed = df.iloc[:-1]  # exclude the still-forming last bar
        if len(closed) < self.strategy.min_bars:
            self._set_gate(
                symbol, f"warming up ({len(closed)}/{self.strategy.min_bars} bars)"
            )
            return

        enriched = add_indicators(
            closed,
            self.settings.ema_fast,
            self.settings.ema_slow,
            self.settings.rsi_period,
            self.settings.atr_period,
            vwap_tz=BUILTIN_MARKETS[spec.market].timezone,
            trend_ema_period=self.settings.trend_ema_period,
        )

        position_qty = self._position_qty(contract)

        if position_qty != 0:
            self._set_gate(symbol, "in position")
            if self.strategy.is_exit_signal(enriched, position_is_long=position_qty > 0):
                log.info("%s: strategy exit signal, flattening.", symbol)
                self.orders.flatten_position(contract, position_qty)
            return

        if self.orders.has_pending_entry(contract.conId):
            self._set_gate(symbol, "entry order pending")
            return  # an entry order is already in flight -- don't stack a second one

        if entry_gate:
            self._set_gate(symbol, entry_gate)
            return
        count = open_position_count + self._entries_this_tick
        open_risk_pct = sum(self._open_risk_pct.values())
        if not self.risk.can_open_new_position(count, open_risk_pct=open_risk_pct):
            if self.risk.kill_switch_active:
                gate = "kill switch active"
            elif count >= self.risk.max_concurrent_positions:
                gate = f"max positions ({count}/{self.risk.max_concurrent_positions})"
            else:
                gate = (
                    f"portfolio heat cap ({open_risk_pct:.1f}%/"
                    f"{self.risk.max_open_risk_pct:g}%)"
                )
            self._set_gate(symbol, gate)
            return

        signal = self.strategy.generate_signal(enriched)
        last = enriched.iloc[-1]
        log.info(
            "%s: ema_fast=%.4f ema_slow=%.4f rsi=%.1f close=%.2f vwap=%.2f -> %s",
            symbol,
            last["ema_fast"],
            last["ema_slow"],
            last["rsi"],
            last["close"],
            last["vwap"],
            signal.value,
        )
        self.latest_signals[symbol] = {
            "signal": signal.value,
            "rsi": float(last["rsi"]),
            "close": float(last["close"]),
            "vwap": float(last["vwap"]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Optional, duck-typed: strategies whose entry logic isn't well
        # summarized by rsi/vwap alone (e.g. trendline_breakout's
        # slope/fit-quality) can expose their own extra fields here, picked
        # up by the dashboard's per-symbol table as a live sanity check.
        diagnostics_fn = getattr(self.strategy, "diagnostics", None)
        if diagnostics_fn is not None:
            self.latest_signals[symbol].update(diagnostics_fn(enriched))
        if signal == Signal.FLAT:
            session_bars = self.latest_signals[symbol].get("trend_session_bars")
            if session_bars is not None and session_bars < self.settings.trend_window + 1:
                # trendline_breakout refuses to fit across the overnight gap
                # -- show the same-session warmup progress instead of a
                # generic "no signal".
                self._set_gate(
                    symbol,
                    f"session warmup ({session_bars}/{self.settings.trend_window + 1} bars)",
                )
            else:
                self._set_gate(symbol, "no signal")
            return
        if signal == Signal.SHORT and spec.security_type == "CRYPTO":
            # IB has no spot-crypto shorting -- the order would only come
            # back rejected (confirmed live: XRP SHORT signal on a Sunday).
            log.info("%s: SHORT signal skipped -- IB does not support shorting spot crypto.", symbol)
            self._set_gate(symbol, "short unsupported (crypto)")
            return

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

        try:
            equity_local = (
                await self.fx.convert(equity, self.base_currency, spec.currency)
                if spec.currency != self.base_currency
                else equity
            )
        except Exception as exc:  # noqa: BLE001 - sizing without a rate is impossible
            # Revert the new-bar latch so the next tick re-evaluates this
            # same bar once the FX subscription has warmed up (or the FX
            # market has reopened) -- without this, the one failed attempt
            # silently discarded the signal (confirmed live: the first real
            # entry signal of a session, XRP on a EUR-base account, lost to
            # a quoteless weekend EURUSD ticker).
            log.warning(
                "%s: no FX rate for %s->%s yet (%s) -- deferring this bar's signal "
                "to the next tick instead of dropping it.",
                symbol,
                self.base_currency,
                spec.currency,
                exc,
            )
            self._last_bar_start[symbol] = last_seen
            self._set_gate(symbol, "waiting for FX rate")
            return
        meta = self.contract_meta.get(symbol, ContractMeta())
        if spec.security_type == "CRYPTO":
            # Fractional sizing -- whole-unit sizing made crypto untradeable
            # (1 BTC needs ~$120k notional; with a 20% notional cap that's
            # ~$600k equity before a single signal could ever fill).
            size_increment = meta.size_increment if meta.size_increment > 0 else 1e-8
            min_quantity = meta.min_size
        else:
            # Stocks stay whole-share, except venues with board lots larger
            # than one share (e.g. SEHK), where IB reports the lot as the
            # size increment and odd lots get rejected.
            size_increment = meta.size_increment if meta.size_increment > 1 else 1.0
            min_quantity = meta.min_size if meta.min_size > 1 else 0.0
        quantity = self.risk.position_size(
            equity_local,
            entry_price,
            stop_price,
            min_size_increment=size_increment,
            min_quantity=min_quantity,
        )
        if quantity <= 0:
            log.info("%s: signal %s but computed position size is 0, skipping.", symbol, signal)
            self._set_gate(symbol, "size 0 (risk budget vs price/stop)")
            return

        log.info(
            "%s: %s signal -> %s %s units @ ~%.2f %s, stop=%.2f, target=%.2f",
            symbol,
            signal,
            action,
            quantity,
            entry_price,
            spec.currency,
            stop_price,
            target_price,
        )
        entry_limit_price = None
        if spec.security_type == "CRYPTO":
            buffer = CRYPTO_ENTRY_LIMIT_BUFFER_PCT / 100
            entry_limit_price = entry_price * (1 + buffer if action == "BUY" else 1 - buffer)
        self.orders.place_bracket(
            contract,
            action,
            quantity,
            stop_price,
            target_price,
            outside_rth=outside_rth,
            meta=meta,
            entry_limit_price=entry_limit_price,
            # ZeroHash supports neither stop orders (387) nor child/hedge
            # orders (201), and buys must be IOC (201) -- all confirmed
            # live. Crypto entries go out bare as IOC limits; exits are
            # engine-managed, see _manage_crypto_exits.
            attach_exits=spec.security_type != "CRYPTO",
        )
        self.journal.record_entry_context(
            symbol,
            direction=signal.value,
            entry_ref_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            strategy=self.settings.strategy,
        )
        if equity_local > 0:
            self._open_risk_pct[symbol] = quantity * stop_dist / equity_local * 100.0
        self._entries_this_tick += 1
        self._set_gate(symbol, "")  # entered -- nothing blocking
