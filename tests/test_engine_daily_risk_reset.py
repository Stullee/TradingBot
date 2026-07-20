import asyncio
import tempfile
from datetime import timedelta
from types import SimpleNamespace

from tradingbot.config import Settings
from tradingbot.engine import TradingEngine


class FakeBars:
    async def refresh_all_polled(self) -> None:
        pass


class AlwaysClosedSession:
    def is_open(self) -> bool:
        return False

    def should_flatten(self) -> bool:
        return False


def run(coro):
    return asyncio.run(coro)


def make_engine() -> TradingEngine:
    # max_weekly_loss_pct=0 so these tests exercise the *daily* baseline
    # semantics in isolation (the weekly breaker has its own tests in
    # test_risk_manager.py -- with the default 5% it would trip on the
    # equity drops simulated here). log_dir is a temp dir because the day
    # rollover these tests trigger now also emits an EOD summary file.
    settings = Settings(
        ib_port=7497, symbols="AAPL", max_weekly_loss_pct=0, log_dir=tempfile.mkdtemp()
    )
    engine = TradingEngine(settings)
    engine.bars = FakeBars()
    engine.orders = SimpleNamespace(
        flatten_all=lambda: None,
        pending_entry_conids=lambda: set(),
        has_pending_entry=lambda con_id: False,
        cancel_stale_entries=lambda **kw: None,
    )
    for market in list(engine.market_sessions):
        engine.market_sessions[market] = AlwaysClosedSession()
    return engine


def set_equity(engine: TradingEngine, value: float) -> None:
    engine.broker.ib.wrapper.updateAccountValue("NetLiquidation", str(value), "USD", "")


def test_first_tick_establishes_the_daily_baseline():
    engine = make_engine()
    set_equity(engine, 100_000.0)
    run(engine._tick())
    assert engine.risk._starting_equity == 100_000.0
    assert engine._trading_day is not None


def test_kill_switch_does_not_clear_within_the_same_day():
    engine = make_engine()
    set_equity(engine, 100_000.0)
    run(engine._tick())

    set_equity(engine, 97_000.0)  # -3%, breaches the 2% default limit
    run(engine._tick())
    assert engine.risk.kill_switch_active is True

    set_equity(engine, 99_000.0)  # recovers, but still the same day
    run(engine._tick())
    assert engine.risk.kill_switch_active is True  # stays latched


def test_daily_risk_session_resets_on_a_new_utc_calendar_day():
    """The bug this guards against: RiskManager.start_new_session() was only
    ever called once, at connect time -- with the bot now deliberately
    staying up for many hours/days at a stretch (crypto's warmup, riding out
    IB reconnects) rather than restarting daily, "daily" loss tracking
    silently became "loss since the process last restarted," and a kill
    switch tripped on day 1 would never clear for day 2 without a manual
    restart."""
    engine = make_engine()
    set_equity(engine, 100_000.0)
    run(engine._tick())
    today = engine._trading_day

    set_equity(engine, 97_000.0)
    run(engine._tick())
    assert engine.risk.kill_switch_active is True

    # Simulate a new UTC calendar day.
    engine._trading_day = today - timedelta(days=1)
    set_equity(engine, 95_000.0)
    run(engine._tick())

    assert engine.risk.kill_switch_active is False
    assert engine.risk._starting_equity == 95_000.0
    assert engine._trading_day == today
