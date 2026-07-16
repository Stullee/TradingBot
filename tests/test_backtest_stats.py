import math

import pandas as pd
import pytest

from tradingbot.backtest.simulator import Trade
from tradingbot.backtest.stats import summarize


def make_trade(r_multiple: float, symbol: str = "TEST") -> Trade:
    """Builds a Trade with a fixed entry/stop (risk=2.0) whose exit_price is
    chosen so t.r_multiple comes out to exactly the requested value."""
    entry, stop = 100.0, 98.0  # risk = 2.0
    exit_price = entry + r_multiple * (entry - stop)
    return Trade(
        symbol=symbol,
        side="LONG",
        entry_time=pd.Timestamp("2024-01-02 09:30", tz="UTC"),
        entry_price=entry,
        exit_time=pd.Timestamp("2024-01-02 09:35", tz="UTC"),
        exit_price=exit_price,
        stop_price=stop,
        target_price=entry + 10,
        exit_reason="signal",
    )


def test_empty_trade_list():
    s = summarize([])
    assert s.trade_count == 0
    assert s.win_rate == 0.0
    assert s.avg_r == 0.0
    assert s.profit_factor == 0.0


def test_summary_stats_match_hand_calculation():
    r_values = [2.0, -1.0, 1.5, -1.0, -1.0, 3.0]
    trades = [make_trade(r) for r in r_values]
    s = summarize(trades)

    assert s.trade_count == 6
    assert s.win_rate == pytest.approx(0.5)
    assert s.avg_r == pytest.approx(3.5 / 6)
    assert s.avg_win_r == pytest.approx(6.5 / 3)
    assert s.avg_loss_r == pytest.approx(-1.0)
    assert s.profit_factor == pytest.approx(6.5 / 3.0)
    assert s.max_drawdown_r == pytest.approx(2.0)
    assert s.max_consecutive_losses == 2
    assert s.trade_r_sharpe == pytest.approx(0.32396, abs=1e-3)


def test_all_wins_gives_infinite_profit_factor():
    trades = [make_trade(1.0), make_trade(2.0)]
    s = summarize(trades)
    assert s.profit_factor == math.inf
    assert s.win_rate == 1.0


def test_all_losses():
    trades = [make_trade(-1.0), make_trade(-1.0)]
    s = summarize(trades)
    assert s.win_rate == 0.0
    assert s.profit_factor == 0.0
    assert s.max_consecutive_losses == 2


def test_single_trade_sharpe_is_zero_not_divide_by_zero():
    s = summarize([make_trade(1.5)])
    assert s.trade_count == 1
    assert s.trade_r_sharpe == 0.0
