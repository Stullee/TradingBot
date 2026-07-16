import pandas as pd

from tradingbot.strategy.base import Signal
from tradingbot.strategy.vwap_mean_reversion import VwapMeanReversion


def make_strategy(allow_shorting: bool = False, trend_ema_period: int = 0) -> VwapMeanReversion:
    # trend_ema_period=0 disables the trend filter by default here so these
    # tests keep exercising core mean-reversion logic in isolation -- the
    # filter itself is covered separately below.
    return VwapMeanReversion(
        rsi_period=14,
        rsi_oversold=30,
        rsi_overbought=70,
        atr_period=14,
        vwap_dist_atr_mult=0.5,
        trend_ema_period=trend_ema_period,
        allow_shorting=allow_shorting,
    )


def base_df(n: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 09:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "close": [100.0] * n,
            "rsi": [50.0] * n,
            "vwap": [100.0] * n,
            "atr": [1.0] * n,
            "trend_ema": [100.0] * n,
        },
        index=idx,
    )


def test_long_signal_on_oversold_dip_with_reversal_bar():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 98.0  # 2 ATR below vwap
    df.iloc[-1, df.columns.get_loc("rsi")] = 25.0
    df.iloc[-2, df.columns.get_loc("close")] = 97.5  # prior bar lower -> this bar reverses up
    assert strat.generate_signal(df) == Signal.LONG


def test_no_signal_without_enough_bars():
    strat = make_strategy()
    df = base_df(strat.min_bars - 1)
    assert strat.generate_signal(df) == Signal.FLAT


def test_no_long_signal_if_rsi_not_oversold():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 98.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 55.0  # not oversold
    df.iloc[-2, df.columns.get_loc("close")] = 97.5
    assert strat.generate_signal(df) == Signal.FLAT


def test_no_long_signal_if_not_far_enough_below_vwap():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 99.8  # within threshold of vwap
    df.iloc[-1, df.columns.get_loc("rsi")] = 25.0
    df.iloc[-2, df.columns.get_loc("close")] = 99.7
    assert strat.generate_signal(df) == Signal.FLAT


def test_no_long_signal_without_reversal_bar():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 98.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 25.0
    df.iloc[-2, df.columns.get_loc("close")] = 98.5  # still falling, no reversal yet
    assert strat.generate_signal(df) == Signal.FLAT


def test_short_disabled_by_default():
    strat = make_strategy(allow_shorting=False)
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 75.0
    df.iloc[-2, df.columns.get_loc("close")] = 102.5
    assert strat.generate_signal(df) == Signal.FLAT


def test_short_signal_when_enabled():
    strat = make_strategy(allow_shorting=True)
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 75.0
    df.iloc[-2, df.columns.get_loc("close")] = 102.5
    assert strat.generate_signal(df) == Signal.SHORT


def test_is_exit_signal_for_long_once_price_reverts_to_vwap():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 100.5
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.is_exit_signal(df, position_is_long=True)
    assert not strat.is_exit_signal(df, position_is_long=False)


def test_is_exit_signal_for_short_once_price_reverts_to_vwap():
    strat = make_strategy()
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 99.5
    df.iloc[-1, df.columns.get_loc("vwap")] = 100.0
    assert strat.is_exit_signal(df, position_is_long=False)
    assert not strat.is_exit_signal(df, position_is_long=True)


def test_trend_filter_blocks_long_dip_buy_in_a_downtrend():
    strat = make_strategy(trend_ema_period=50)
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 98.0  # 2 ATR below vwap
    df.iloc[-1, df.columns.get_loc("rsi")] = 25.0
    df.iloc[-2, df.columns.get_loc("close")] = 97.5  # reversal bar
    # Trend EMA above price -> downtrend -> LONG blocked even though the
    # rest of the setup (oversold dip + reversal) is otherwise valid.
    df["trend_ema"] = 105.0
    assert strat.generate_signal(df) == Signal.FLAT


def test_trend_filter_allows_long_dip_buy_in_an_uptrend():
    strat = make_strategy(trend_ema_period=50)
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 98.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 25.0
    df.iloc[-2, df.columns.get_loc("close")] = 97.5
    # Trend EMA below price -> uptrend -> LONG (buy the dip) allowed.
    df["trend_ema"] = 95.0
    assert strat.generate_signal(df) == Signal.LONG


def test_trend_filter_blocks_short_sell_rip_in_an_uptrend():
    strat = make_strategy(allow_shorting=True, trend_ema_period=50)
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 75.0
    df.iloc[-2, df.columns.get_loc("close")] = 102.5
    # Trend EMA below price -> uptrend -> SHORT blocked.
    df["trend_ema"] = 95.0
    assert strat.generate_signal(df) == Signal.FLAT


def test_trend_filter_allows_short_sell_rip_in_a_downtrend():
    strat = make_strategy(allow_shorting=True, trend_ema_period=50)
    df = base_df(strat.min_bars + 1)
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("rsi")] = 75.0
    df.iloc[-2, df.columns.get_loc("close")] = 102.5
    # Trend EMA above price -> downtrend -> SHORT (sell the rip) allowed.
    df["trend_ema"] = 105.0
    assert strat.generate_signal(df) == Signal.SHORT
