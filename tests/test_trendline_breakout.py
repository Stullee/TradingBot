import pandas as pd
import pytest

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


def test_diagnostics_reports_the_current_fit():
    """This is what the live dashboard shows per symbol as a sanity check
    on what the strategy currently sees -- must match the same fit
    generate_signal itself acts on, not some separately-computed value."""
    strat = make_strategy()
    diagnostics = strat.diagnostics(df_from(_TIGHT_RISE_THEN_BREAKOUT))

    assert diagnostics["trend_slope"] == pytest.approx(1.0)
    assert diagnostics["trend_r_squared"] == pytest.approx(0.8, abs=1e-6)
    assert diagnostics["trend_line_value"] == pytest.approx(104.0)
    # (105 - 104) / 104 * 100
    assert diagnostics["trend_distance_pct"] == pytest.approx(0.9615, abs=1e-3)


def test_diagnostics_empty_without_enough_bars():
    strat = make_strategy()
    df = df_from(_TIGHT_RISE_THEN_BREAKOUT)
    assert strat.diagnostics(df.iloc[: strat.min_bars - 1]) == {}


# --- session scoping (the VOW3 weekend-gap lesson) -------------------------

def df_with_sessions(closes_by_day: list[list[float]]) -> pd.DataFrame:
    frames = []
    for i, day_closes in enumerate(closes_by_day):
        idx = pd.date_range(f"2024-01-{i + 2:02d} 09:30", periods=len(day_closes), freq="5min", tz="UTC")
        frame = pd.DataFrame({"close": day_closes}, index=idx)
        frame["session_date"] = idx[0].date()
        frames.append(frame)
    return pd.concat(frames)


def test_fit_uses_only_the_current_session():
    """Confirmed live (VOW3, Monday 2026-07-20): a weekend gap-down plus
    Friday's bars in the window made a clean Monday uptrend read as 'no
    valid trend' for hours. With session scoping, the young session simply
    waits; without it, the gap-polluted fit reports a falling line."""
    strat = make_strategy()
    friday = [110.0, 109.5, 109.0, 108.5, 108.0]  # drifting down into the weekend
    monday_early = [100.0, 100.5, 101.0]  # gap down, then a clean rise begins
    df = df_with_sessions([friday, monday_early])

    # Session-scoped: too few Monday bars for a fit -> patient FLAT, and
    # diagnostics show the warmup progress instead of a bogus fit.
    assert strat.generate_signal(df) == Signal.FLAT
    assert strat.diagnostics(df) == {"trend_session_bars": 3}

    # Without session info the window spans the gap: the "trend" is the
    # gap-down, slope negative -- the exact pollution being prevented.
    polluted = strat.diagnostics(df.drop(columns=["session_date"]))
    assert polluted["trend_slope"] < 0


def test_signal_fires_once_the_session_has_enough_bars():
    strat = make_strategy()
    friday = [110.0, 109.5, 109.0, 108.5, 108.0]
    monday = [100.0, 100.5, 101.0, 101.5, 102.0, 105.0]  # 6 bars = window+1
    df = df_with_sessions([friday, monday])
    assert strat.generate_signal(df) == Signal.LONG
    diagnostics = strat.diagnostics(df)
    assert diagnostics["trend_session_bars"] == 6
    assert diagnostics["trend_slope"] > 0


def test_exit_defers_to_bracket_when_session_too_young():
    strat = make_strategy()
    df = df_with_sessions([[110.0, 109.5, 109.0, 108.5, 108.0], [100.0, 99.0]])
    assert strat.is_exit_signal(df, position_is_long=True) is False
