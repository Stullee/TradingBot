"""Trade-list statistics for backtest results. Works in R-multiples (P&L in
units of initial risk) rather than dollar P&L, since the simulator doesn't
model account equity/compounding -- this keeps the numbers meaningful
regardless of position size and comparable across symbols.

Note on "Sharpe": this is a per-trade mean/stdev of R-multiples, a common
quick proxy in trading system evaluation -- NOT the annualized-daily-return
Sharpe ratio the reference report describes (that needs an equity curve
with a time basis, which this per-symbol simulation doesn't produce)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from tradingbot.backtest.simulator import Trade


@dataclass
class Summary:
    trade_count: int
    win_rate: float
    avg_r: float  # expectancy in R-multiples
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float  # sum(wins) / sum(abs(losses)), inf if no losses
    max_drawdown_r: float  # on the cumulative-R curve
    trade_r_sharpe: float  # mean(R) / stdev(R); see module docstring
    max_consecutive_losses: int


def summarize(trades: list[Trade]) -> Summary:
    if not trades:
        return Summary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)

    r_multiples = [t.r_multiple for t in trades]
    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r <= 0]

    n = len(r_multiples)
    win_rate = len(wins) / n
    avg_r = sum(r_multiples) / n
    avg_win_r = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss_r = (sum(losses) / len(losses)) if losses else 0.0

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else math.inf

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_multiples:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    if n > 1:
        mean = avg_r
        variance = sum((r - mean) ** 2 for r in r_multiples) / (n - 1)
        stdev = math.sqrt(variance)
        sharpe = (mean / stdev) if stdev > 0 else 0.0
    else:
        sharpe = 0.0

    max_consec_losses = 0
    current_streak = 0
    for r in r_multiples:
        if r <= 0:
            current_streak += 1
            max_consec_losses = max(max_consec_losses, current_streak)
        else:
            current_streak = 0

    return Summary(
        trade_count=n,
        win_rate=win_rate,
        avg_r=avg_r,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        profit_factor=profit_factor,
        max_drawdown_r=max_dd,
        trade_r_sharpe=sharpe,
        max_consecutive_losses=max_consec_losses,
    )
