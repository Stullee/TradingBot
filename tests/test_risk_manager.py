from tradingbot.risk.manager import RiskManager


def make_rm(**overrides) -> RiskManager:
    defaults = dict(
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=2.0,
        max_concurrent_positions=3,
        max_position_pct=20.0,
    )
    defaults.update(overrides)
    return RiskManager(**defaults)


def test_position_size_respects_risk_budget():
    rm = make_rm(risk_per_trade_pct=1.0, max_position_pct=100.0)
    equity = 100_000
    entry, stop = 50.0, 49.0  # $1 risk/share
    qty = rm.position_size(equity, entry, stop)
    assert qty == 1000  # 1% of 100k = 1000 / $1 risk per share


def test_position_size_capped_by_max_notional():
    rm = make_rm(risk_per_trade_pct=10.0, max_position_pct=5.0)
    equity = 100_000
    entry, stop = 50.0, 49.5  # tiny risk/share -> risk sizing would be huge
    qty = rm.position_size(equity, entry, stop)
    assert qty * entry <= equity * 0.05 + 1e-6


def test_position_size_zero_when_stop_equals_entry():
    rm = make_rm()
    assert rm.position_size(100_000, 50.0, 50.0) == 0


def test_can_open_new_position_respects_max_concurrent():
    rm = make_rm(max_concurrent_positions=2)
    rm.start_new_session(100_000)
    assert rm.can_open_new_position(0) is True
    assert rm.can_open_new_position(1) is True
    assert rm.can_open_new_position(2) is False


def test_daily_loss_kill_switch_latches():
    rm = make_rm(max_daily_loss_pct=2.0)
    rm.start_new_session(100_000)
    assert rm.check_daily_loss_limit(99_500) is False  # -0.5%
    assert rm.kill_switch_active is False
    assert rm.check_daily_loss_limit(97_500) is True  # -2.5%, breach
    assert rm.kill_switch_active is True
    assert rm.check_daily_loss_limit(99_900) is True  # stays latched even if equity recovers


def test_kill_switch_blocks_new_positions():
    rm = make_rm()
    rm.start_new_session(100_000)
    rm.check_daily_loss_limit(97_000)
    assert rm.can_open_new_position(0) is False


def test_new_session_resets_kill_switch():
    rm = make_rm()
    rm.start_new_session(100_000)
    rm.check_daily_loss_limit(97_000)
    assert rm.kill_switch_active is True
    rm.start_new_session(100_000)
    assert rm.kill_switch_active is False
