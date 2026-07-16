import numpy as np
import pandas as pd

from tradingbot.backtest.compare import compare_symbol, split_train_validation


def test_split_train_validation_is_chronological_and_covers_whole_frame():
    idx = pd.date_range("2024-01-02 09:30", periods=100, freq="5min", tz="UTC")
    df = pd.DataFrame({"close": range(100)}, index=idx)

    train, val = split_train_validation(df, train_fraction=0.7)

    assert len(train) == 70
    assert len(val) == 30
    assert train.index[-1] < val.index[0]
    assert pd.concat([train, val]).equals(df)


def make_ohlcv(n: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 09:30", periods=n, freq="5min", tz="UTC")
    close = pd.Series(np.linspace(100, 130, n), index=idx)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": pd.Series(1000, index=idx),
        }
    )


def test_compare_symbol_returns_summaries_for_both_variants_and_splits():
    df = make_ohlcv(300)

    comparison = compare_symbol(
        df,
        "TEST",
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        rsi_oversold=30,
        rsi_overbought=70,
        atr_period=14,
        vwap_dist_atr_mult=0.5,
        stop_atr_mult=1.5,
        target_atr_mult=2.5,
        trend_ema_period=50,
        allow_shorting=True,
    )

    assert comparison.symbol == "TEST"
    # Summaries must be computable (not raise) regardless of trade_count,
    # and each split's trades must not leak across the train/validation
    # chronological boundary.
    for split in (comparison.train, comparison.validation):
        assert split.baseline.trade_count == len(split.baseline_trades)
        assert split.filtered.trade_count == len(split.filtered_trades)

    train_end = comparison.train.baseline_trades + comparison.train.filtered_trades
    val_start = comparison.validation.baseline_trades + comparison.validation.filtered_trades
    if train_end and val_start:
        assert max(t.exit_time for t in train_end) <= min(t.entry_time for t in val_start)


def test_baseline_never_blocked_by_trend_even_in_a_steady_downtrend():
    # A strong steady downtrend keeps price below any reasonable trend EMA
    # the whole way -- the filtered variant's LONG entries should be
    # suppressed (or at least never exceed baseline's), while baseline
    # (trend_ema_period=0) never applies that filter at all.
    idx = pd.date_range("2024-01-02 09:30", periods=300, freq="5min", tz="UTC")
    close = pd.Series(np.linspace(130, 100, 300), index=idx)
    df = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": pd.Series(1000, index=idx),
        }
    )

    comparison = compare_symbol(
        df,
        "DOWN",
        ema_fast=9,
        ema_slow=21,
        rsi_period=14,
        rsi_oversold=30,
        rsi_overbought=70,
        atr_period=14,
        vwap_dist_atr_mult=0.5,
        stop_atr_mult=1.5,
        target_atr_mult=2.5,
        trend_ema_period=50,
        allow_shorting=False,
    )

    total_filtered = len(comparison.train.filtered_trades) + len(comparison.validation.filtered_trades)
    total_baseline = len(comparison.train.baseline_trades) + len(comparison.validation.baseline_trades)
    assert total_filtered <= total_baseline
