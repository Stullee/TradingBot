from datetime import datetime
from zoneinfo import ZoneInfo

from tradingbot.config import Settings
from tradingbot.engine import TradingEngine


class FakeSession:
    """Stands in for MarketSession -- _should_flatten_shadow_trade only ever
    calls these three members."""

    def __init__(self, should_flatten_result: bool, now_local: datetime):
        self._should_flatten_result = should_flatten_result
        self._now_local = now_local
        self.tz = now_local.tzinfo

    def should_flatten(self) -> bool:
        return self._should_flatten_result

    def now_local(self) -> datetime:
        return self._now_local


def make_engine() -> TradingEngine:
    return TradingEngine(Settings(ib_port=7497, markets="EU:SAP"))


def test_flattens_when_todays_close_window_is_reached():
    engine = make_engine()
    now = datetime(2026, 7, 17, 17, 26, tzinfo=ZoneInfo("Europe/Berlin"))
    engine.market_sessions["EU"] = FakeSession(should_flatten_result=True, now_local=now)

    opened_today = now.replace(hour=10, minute=0).isoformat()
    assert engine._should_flatten_shadow_trade("SAP", opened_today) is True


def test_does_not_flatten_a_trade_opened_earlier_today_before_the_close_window():
    engine = make_engine()
    now = datetime(2026, 7, 17, 10, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    engine.market_sessions["EU"] = FakeSession(should_flatten_result=False, now_local=now)

    opened_today = now.replace(hour=9, minute=15).isoformat()
    assert engine._should_flatten_shadow_trade("SAP", opened_today) is False


def test_flattens_a_trade_carried_over_from_an_earlier_calendar_day():
    """The bug this guards against: a shadow trade opened yesterday and
    never flattened (this check didn't exist yet) would otherwise stay open
    until today's own close window is reached, many hours away -- but it
    already should have been flattened at least once by now and just
    wasn't, so it must close immediately once this check runs, not wait for
    today's window."""
    engine = make_engine()
    now = datetime(2026, 7, 17, 10, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    engine.market_sessions["EU"] = FakeSession(should_flatten_result=False, now_local=now)

    opened_yesterday = now.replace(day=16, hour=14, minute=0).isoformat()
    assert engine._should_flatten_shadow_trade("SAP", opened_yesterday) is True


def test_unknown_symbol_fails_safe_to_flatten():
    engine = make_engine()
    assert engine._should_flatten_shadow_trade("NOPE", datetime.now().isoformat()) is True
