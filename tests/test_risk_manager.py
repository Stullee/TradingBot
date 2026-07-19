from datetime import date

from tradingbot.risk.manager import RiskManager, floor_to_increment


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


def test_position_size_fractional_for_crypto_like_increments():
    """Whole-unit sizing made crypto untradeable: 1% of a 10k account can
    never buy a whole BTC. A fractional increment sizes it properly."""
    rm = make_rm(risk_per_trade_pct=1.0, max_position_pct=50.0)
    qty = rm.position_size(10_000, 100_000.0, 98_000.0, min_size_increment=1e-6)
    # risk budget 100 / 2000 risk-per-unit = 0.05 units; notional cap
    # 5000/100k = 0.05 -> 0.05 exactly
    assert abs(qty - 0.05) < 1e-9
    # ...whereas whole-unit sizing returns 0 (the old dead-crypto behavior)
    assert rm.position_size(10_000, 100_000.0, 98_000.0, min_size_increment=1.0) == 0


def test_position_size_respects_board_lot_increment():
    rm = make_rm(risk_per_trade_pct=1.0, max_position_pct=100.0)
    # 1% of 100k = 1000 budget / 2.5 risk = 400 shares -> floored to 400
    # with lot 100; with lot 300 -> 300.
    assert rm.position_size(100_000, 50.0, 47.5, min_size_increment=100) == 400
    assert rm.position_size(100_000, 50.0, 47.5, min_size_increment=300) == 300


def test_position_size_zero_below_min_quantity():
    rm = make_rm(risk_per_trade_pct=1.0, max_position_pct=100.0)
    assert rm.position_size(100_000, 50.0, 49.0, min_size_increment=100, min_quantity=2000) == 0


def test_floor_to_increment_cleans_float_dust():
    assert floor_to_increment(0.37, 0.1) == 0.3
    assert floor_to_increment(399.999999, 100) == 300
    assert floor_to_increment(0.05, 1e-6) == 0.05


def test_can_open_new_position_respects_max_concurrent():
    rm = make_rm(max_concurrent_positions=2)
    rm.start_new_session(100_000)
    assert rm.can_open_new_position(0) is True
    assert rm.can_open_new_position(1) is True
    assert rm.can_open_new_position(2) is False


def test_can_open_new_position_respects_portfolio_heat_cap():
    rm = make_rm(risk_per_trade_pct=1.0, max_open_risk_pct=3.0, max_concurrent_positions=10)
    rm.start_new_session(100_000)
    # 2% already at risk + 1% new = 3% -> exactly at the cap, allowed
    assert rm.can_open_new_position(2, open_risk_pct=2.0) is True
    # 2.5% at risk + 1% new = 3.5% -> over the cap
    assert rm.can_open_new_position(2, open_risk_pct=2.5) is False
    # cap disabled -> heat ignored
    rm_off = make_rm(risk_per_trade_pct=1.0, max_open_risk_pct=0.0)
    rm_off.start_new_session(100_000)
    assert rm_off.can_open_new_position(2, open_risk_pct=99.0) is True


def test_daily_loss_kill_switch_latches():
    rm = make_rm(max_daily_loss_pct=2.0)
    rm.start_new_session(100_000)
    assert rm.check_loss_limits(99_500) is False  # -0.5%
    assert rm.kill_switch_active is False
    assert rm.check_loss_limits(97_500) is True  # -2.5%, breach
    assert rm.kill_switch_active is True
    assert rm.check_loss_limits(99_900) is True  # stays latched even if equity recovers


def test_kill_switch_blocks_new_positions():
    rm = make_rm()
    rm.start_new_session(100_000)
    rm.check_loss_limits(97_000)
    assert rm.can_open_new_position(0) is False


def test_new_session_resets_kill_switch():
    rm = make_rm()
    rm.start_new_session(100_000)
    rm.check_loss_limits(97_000)
    assert rm.kill_switch_active is True
    rm.start_new_session(100_000)
    assert rm.kill_switch_active is False


def test_weekly_loss_limit_survives_daily_resets():
    """Five straight losing days never trip a daily limit; the weekly
    baseline catches the cumulative bleed and stays latched across daily
    session resets within the same ISO week."""
    rm = make_rm(max_daily_loss_pct=2.0, max_weekly_loss_pct=5.0)
    monday, tuesday, wednesday = date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)
    rm.start_new_session(100_000, monday)
    assert rm.check_loss_limits(98_100) is False  # -1.9% day 1, no trip
    rm.start_new_session(98_100, tuesday)
    assert rm.check_loss_limits(96_300) is False  # -1.8% day 2, cumulative -3.7%
    rm.start_new_session(96_300, wednesday)
    assert rm.check_loss_limits(94_900) is True  # cumulative -5.1% -> weekly trip
    assert rm.weekly_kill_switch_active is True
    assert rm.daily_kill_switch_active is False
    # a new day inside the same week does NOT clear it
    rm.start_new_session(94_900, date(2026, 7, 16))
    assert rm.kill_switch_active is True
    # the next ISO week does
    rm.start_new_session(94_900, date(2026, 7, 20))
    assert rm.kill_switch_active is False


def test_state_persists_and_restores_across_restart(tmp_path):
    """A same-day restart must restore the morning's baseline and any
    latched kill switch -- not re-latch from already-depleted equity."""
    state_path = tmp_path / "risk_state.json"
    today = date(2026, 7, 17)

    rm = make_rm(max_daily_loss_pct=2.0)
    rm.enable_persistence(state_path)
    rm.start_or_restore_session(100_000, today)
    assert rm.check_loss_limits(97_000) is True  # trip and persist

    rm2 = make_rm(max_daily_loss_pct=2.0)
    rm2.enable_persistence(state_path)
    rm2.start_or_restore_session(97_000, today)  # restart at depleted equity
    assert rm2.kill_switch_active is True  # still latched
    assert rm2._starting_equity == 100_000  # original baseline, not 97k

    # ...but a different (next) day starts fresh
    rm3 = make_rm(max_daily_loss_pct=2.0)
    rm3.enable_persistence(state_path)
    rm3.start_or_restore_session(97_000, date(2026, 7, 18))
    assert rm3.kill_switch_active is False
    assert rm3._starting_equity == 97_000
