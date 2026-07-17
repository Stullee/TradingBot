import pandas as pd

from tradingbot.strategy.base import Signal
from tradingbot.strategy.trendline_breakout import TrendlineBreakout

_IDX = pd.date_range("2024-01-02 09:30", periods=8, freq="5min", tz="UTC")


def make_strategy(allow_shorting: bool = False, min_r_squared: float = 0.7) -> TrendlineBreakout:
    return TrendlineBreakout(
        trend_window=5, min_r_squared=min_r_squared, atr_period=2, allow_shorting=allow_shorting
    )


def df_from(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes}, index=_IDX[: len(closes)])


# First two values are padding outside the 5-bar trend window (never read by
# _fit) -- only the trailing 5 bars of each fixture matter.
_TIGHT_RISE_THEN_BREAKOUT = [1.0, 2.0, 100.0, 100.5, 101.0, 101.5, 102.0, 105.0]
_TIGHT_FALL_THEN_BREAKDOWN = [200.0, 199.0, 102.0, 101.5, 101.0, 100.5, 100.0, 97.0]
_NOISY_ZIGZAG = [1.0, 2.0, 100.0, 101.0, 99.0, 102.0, 98.0, 103.0]


def test_long_signal_on_tight_rising_trendline_breakout():
    strat = make_strategy()
    assert strat.generate_signal(df_from(_TIGHT_RISE_THEN_BREAKOUT)) == Signal.LONG


def test_no_signal_without_enough_bars():
    strat = make_strategy()
    df = df_from(_TIGHT_RISE_THEN_BREAKOUT)
    assert strat.generate_signal(df.iloc[: strat.min_bars - 1]) == Signal.FLAT


def test_no_signal_on_a_noisy_zigzag_even_if_it_nets_upward():
    """A series that ends up higher over the window isn't the same as a
    genuine trend -- a loose/noisy fit (low R-squared) must not fire."""
    strat = make_strategy()
    assert strat.generate_signal(df_from(_NOISY_ZIGZAG)) == Signal.FLAT


def test_stricter_r_squared_threshold_blocks_an_otherwise_valid_breakout():
    strat = make_strategy(min_r_squared=0.95)  # the fixture's actual fit is ~0.8
    assert strat.generate_signal(df_from(_TIGHT_RISE_THEN_BREAKOUT)) == Signal.FLAT


def test_short_disabled_by_default():
    strat = make_strategy(allow_shorting=False)
    assert strat.generate_signal(df_from(_TIGHT_FALL_THEN_BREAKDOWN)) == Signal.FLAT


def test_short_signal_when_enabled():
    strat = make_strategy(allow_shorting=True)
    assert strat.generate_signal(df_from(_TIGHT_FALL_THEN_BREAKDOWN)) == Signal.SHORT


def test_is_exit_signal_for_long_once_price_falls_back_below_its_trendline():
    strat = make_strategy()
    still_above = df_from(_TIGHT_RISE_THEN_BREAKOUT)
    assert not strat.is_exit_signal(still_above, position_is_long=True)

    fallen_below = df_from([1.0, 2.0, 100.0, 100.5, 101.0, 101.5, 102.0, 99.0])
    assert strat.is_exit_signal(fallen_below, position_is_long=True)


def test_is_exit_signal_for_short_once_price_rises_back_above_its_trendline():
    strat = make_strategy(allow_shorting=True)
    still_below = df_from(_TIGHT_FALL_THEN_BREAKDOWN)
    assert not strat.is_exit_signal(still_below, position_is_long=False)

    risen_above = df_from([200.0, 199.0, 102.0, 101.5, 101.0, 100.5, 100.0, 103.0])
    assert strat.is_exit_signal(risen_above, position_is_long=False)
