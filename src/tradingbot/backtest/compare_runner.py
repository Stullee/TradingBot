"""Trend-filter before/after comparison: `python -m tradingbot.backtest.compare_runner`

Fetches the same history the regular backtester would (BACKTEST_DURATION,
same symbol universe), then for every symbol runs two VwapMeanReversion
variants -- trend filter off (the old unconditional countertrend behavior)
and trend filter on (TREND_EMA_PERIOD, the current default) -- over both a
train slice and a held-out, more-recent validation slice. This is always a
VwapMeanReversion comparison regardless of STRATEGY, since the trend filter
only applies to that strategy; if STRATEGY=ema_rsi_momentum is configured,
the comparison still runs (against VwapMeanReversion) as a "would the
filter help" check, and a note is printed to that effect.

Point of this split: judging the filter only on the exact window it was
designed against risks just tuning to noise in that window. If it only
looks good on the train slice and falls apart on validation, that's a sign
it doesn't generalize.

Same non-order-placing, bounded-concurrency, separate-client-id behavior as
tradingbot.backtest.runner -- see that module's docstring for the pacing
caveats, which apply here identically since this issues the same kind of
historical-data requests."""
from __future__ import annotations

import asyncio
import logging

from tradingbot.backtest.compare import TRAIN_FRACTION, SymbolComparison, compare_symbol
from tradingbot.backtest.fetch import fetch_history
from tradingbot.backtest.simulator import Trade
from tradingbot.backtest.stats import Summary, summarize
from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings, load_settings
from tradingbot.markets import BUILTIN_MARKETS
from tradingbot.symbols import SymbolSpec

log = logging.getLogger(__name__)

_FETCH_CONCURRENCY = 5


def _fmt(s: Summary) -> str:
    pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    return (
        f"trades={s.trade_count:3d} win_rate={s.win_rate:5.0%} "
        f"avg_R={s.avg_r:+.2f} profit_factor={pf} max_dd_R={s.max_drawdown_r:.2f}"
    )


async def _process_symbol(
    broker: BrokerConnection,
    settings: Settings,
    spec: SymbolSpec,
    sem: asyncio.Semaphore,
) -> tuple[str, SymbolComparison | None]:
    preset = BUILTIN_MARKETS[spec.market]
    async with sem:
        try:
            contract = await broker.qualify_contract(
                spec.security_type, spec.symbol, spec.exchange, spec.currency
            )
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest
            return f"{spec.symbol:8s} SKIPPED: {exc}", None

        is_crypto = spec.security_type == "CRYPTO"
        try:
            df = await fetch_history(
                broker.ib,
                contract,
                settings.bar_size,
                settings.backtest_duration,
                use_rth=not preset.outside_rth,
                what_to_show="AGGTRADES" if is_crypto else "TRADES",
            )
        except Exception as exc:  # noqa: BLE001 - one bad fetch must not stop the rest
            return f"{spec.symbol:8s} SKIPPED (history fetch failed): {exc}", None

    if df is None or df.empty:
        return f"{spec.symbol:8s} no historical data returned", None

    comparison = compare_symbol(
        df,
        spec.symbol,
        settings.ema_fast,
        settings.ema_slow,
        settings.rsi_period,
        settings.rsi_oversold,
        settings.rsi_overbought,
        settings.atr_period,
        settings.vwap_dist_atr_mult,
        settings.stop_atr_mult,
        settings.target_atr_mult,
        settings.trend_ema_period,
        settings.allow_shorting,
        vwap_tz=preset.timezone,
        commission_per_share=settings.backtest_commission_per_share,
        slippage_bps=settings.backtest_slippage_bps,
    )
    line = (
        f"{spec.symbol:8s} bars={len(df):5d}  "
        f"train  baseline: {_fmt(comparison.train.baseline)}  |  filtered: {_fmt(comparison.train.filtered)}\n"
        f"{'':8s}          "
        f"val    baseline: {_fmt(comparison.validation.baseline)}  |  filtered: {_fmt(comparison.validation.filtered)}"
    )
    return line, comparison


def _print_aggregate(label: str, baseline: list[Trade], filtered: list[Trade]) -> None:
    baseline.sort(key=lambda t: t.exit_time)
    filtered.sort(key=lambda t: t.exit_time)
    b, f = summarize(baseline), summarize(filtered)
    print(f"\n=== {label} (all symbols pooled) ===")
    print(f"  baseline (no trend filter): {_fmt(b)}  trade_R_sharpe={b.trade_r_sharpe:.2f}")
    print(f"  filtered (trend filter on): {_fmt(f)}  trade_R_sharpe={f.trade_r_sharpe:.2f}")


async def run() -> None:
    settings = load_settings()
    print(
        f"Comparing VwapMeanReversion with trend_ema_period=0 (baseline) vs "
        f"trend_ema_period={settings.trend_ema_period} (filtered).\n"
        f"Duration: {settings.backtest_duration}  Bar size: {settings.bar_size}  "
        f"Train/validation split: {TRAIN_FRACTION:.0%}/{1 - TRAIN_FRACTION:.0%} "
        "(chronological, validation = more recent)\n"
    )
    if settings.strategy != "vwap_mean_reversion":
        print(
            f"Note: STRATEGY={settings.strategy!r} is configured, but the trend "
            "filter only applies to vwap_mean_reversion -- this comparison runs "
            "against that strategy regardless, as a \"would the filter help\" check.\n"
        )

    # Distinct client id so this can run alongside the live engine and/or
    # the regular backtester without IB rejecting a duplicate connection.
    compare_settings = settings.model_copy(update={"ib_client_id": settings.ib_client_id + 51})
    broker = BrokerConnection(compare_settings)
    await broker.connect_with_retry()

    all_train_baseline: list[Trade] = []
    all_train_filtered: list[Trade] = []
    all_val_baseline: list[Trade] = []
    all_val_filtered: list[Trade] = []
    try:
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
        tasks = [
            asyncio.create_task(_process_symbol(broker, settings, spec, sem))
            for spec in settings.symbol_specs
        ]
        for coro in asyncio.as_completed(tasks):
            line, comparison = await coro
            print(line)
            if comparison is not None:
                all_train_baseline.extend(comparison.train.baseline_trades)
                all_train_filtered.extend(comparison.train.filtered_trades)
                all_val_baseline.extend(comparison.validation.baseline_trades)
                all_val_filtered.extend(comparison.validation.filtered_trades)
    finally:
        broker.disconnect()

    _print_aggregate("Train", all_train_baseline, all_train_filtered)
    _print_aggregate("Validation (held-out)", all_val_baseline, all_val_filtered)
    print(
        "\nA filter that only wins on Train and not on Validation is likely "
        "tuned to that specific window rather than a real, generalizing edge. "
        "Treat this as a rough signal, not proof -- see the regular backtester's "
        "caveats (overfitting, look-ahead bias, no slippage/commission modeling) "
        "before trusting it too far."
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet IB connection chatter
    asyncio.run(run())


if __name__ == "__main__":
    main()
