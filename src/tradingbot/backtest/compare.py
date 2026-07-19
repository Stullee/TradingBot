"""Train/validation split comparison: runs two VwapMeanReversion variants
(trend filter on vs off) over the same fetched history, on both a train
slice and a held-out validation slice, so a change like the trend filter
can be judged on out-of-sample data instead of just "does it help on the
window I tuned it against."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from tradingbot.backtest.simulator import Trade, simulate_enriched
from tradingbot.backtest.stats import Summary, summarize
from tradingbot.data.indicators import add_indicators
from tradingbot.strategy.vwap_mean_reversion import VwapMeanReversion

# Chronological split -- validation is the more recent slice, since that's
# the closer analogue to "how would this behave going forward" than a
# random shuffle would be (which would leak future bars' indicator/session
# context into the train slice and vice versa).
TRAIN_FRACTION = 0.7


@dataclass
class SplitResult:
    baseline_trades: list[Trade]
    filtered_trades: list[Trade]

    @property
    def baseline(self) -> Summary:
        return summarize(self.baseline_trades)

    @property
    def filtered(self) -> Summary:
        return summarize(self.filtered_trades)


@dataclass
class SymbolComparison:
    symbol: str
    train: SplitResult
    validation: SplitResult


def split_train_validation(
    df: pd.DataFrame, train_fraction: float = TRAIN_FRACTION
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Splits a sorted, time-indexed frame chronologically. Not date-aware
    (a plain row-count split), which is fine for this purpose -- both
    slices still span many sessions for any duration long enough to backtest."""
    split_idx = int(len(df) * train_fraction)
    return df.iloc[:split_idx], df.iloc[split_idx:]


def compare_symbol(
    df: pd.DataFrame,
    symbol: str,
    ema_fast: int,
    ema_slow: int,
    rsi_period: int,
    rsi_oversold: float,
    rsi_overbought: float,
    atr_period: int,
    vwap_dist_atr_mult: float,
    stop_atr_mult: float,
    target_atr_mult: float,
    trend_ema_period: int,
    allow_shorting: bool,
    vwap_tz: str = "US/Eastern",
    train_fraction: float = TRAIN_FRACTION,
    commission_per_share: float = 0.0,
    slippage_bps: float = 0.0,
) -> SymbolComparison:
    """Enriches `df` once (with the trend_ema column present, since the
    filtered variant needs it -- the baseline variant simply never reads
    it), splits into train/validation, and runs both strategy variants over
    both slices."""
    enriched = add_indicators(
        df, ema_fast, ema_slow, rsi_period, atr_period, vwap_tz, trend_ema_period
    )
    train_df, val_df = split_train_validation(enriched, train_fraction)

    baseline_strategy = VwapMeanReversion(
        rsi_period=rsi_period,
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
        atr_period=atr_period,
        vwap_dist_atr_mult=vwap_dist_atr_mult,
        trend_ema_period=0,
        allow_shorting=allow_shorting,
    )
    filtered_strategy = VwapMeanReversion(
        rsi_period=rsi_period,
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
        atr_period=atr_period,
        vwap_dist_atr_mult=vwap_dist_atr_mult,
        trend_ema_period=trend_ema_period,
        allow_shorting=allow_shorting,
    )

    def run(slice_df: pd.DataFrame) -> SplitResult:
        return SplitResult(
            baseline_trades=simulate_enriched(
                slice_df,
                baseline_strategy,
                symbol,
                stop_atr_mult,
                target_atr_mult,
                commission_per_share=commission_per_share,
                slippage_bps=slippage_bps,
            ),
            filtered_trades=simulate_enriched(
                slice_df,
                filtered_strategy,
                symbol,
                stop_atr_mult,
                target_atr_mult,
                commission_per_share=commission_per_share,
                slippage_bps=slippage_bps,
            ),
        )

    return SymbolComparison(symbol=symbol, train=run(train_df), validation=run(val_df))
