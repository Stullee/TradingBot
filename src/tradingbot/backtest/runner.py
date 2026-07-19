"""Standalone backtest CLI: `python -m tradingbot.backtest.runner`

Fetches historical bars for the configured symbol universe (same MARKETS/
SYMBOLS config as live trading) and runs the configured strategy against
them bar-by-bar, printing per-symbol and pooled-aggregate expectancy stats.
Uses BACKTEST_DURATION (see config.py) for how far back to pull.

This never places orders or touches the account -- it only requests
historical data, and connects with a different client id (live client id +
50) than the live/paper engine so both can run at the same time without IB
rejecting one for a duplicate client id.

Fetches run with bounded concurrency (see _FETCH_CONCURRENCY) to overlap
each request's own network latency, but concurrency alone doesn't bound
the *rate* new requests get submitted -- IB enforces a hard historical-data
pacing limit (roughly 60 requests per rolling 10-minute window per
connection), and if individual fetches resolve faster than that budget
allows, concurrency alone blows straight through it (confirmed live: the
first couple of symbols in a 60+ symbol universe went through fine, then
every one after started timing out and getting cancelled server-side by
IB, mid-run). _PACING_GAP_SEC paces new dispatches to stay safely under
that budget regardless of how fast any given fetch resolves, so a full run
across a large symbol universe still has a floor of several minutes -- concurrency
overlaps latency within that floor, it doesn't shrink the floor itself."""
from __future__ import annotations

import asyncio
import logging
import time

from tradingbot.backtest.fetch import fetch_history
from tradingbot.backtest.simulator import Trade, simulate
from tradingbot.backtest.stats import summarize
from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import Settings, load_settings
from tradingbot.engine import build_strategy
from tradingbot.markets import BUILTIN_MARKETS
from tradingbot.strategy.base import Strategy
from tradingbot.symbols import SymbolSpec

log = logging.getLogger(__name__)

_FETCH_CONCURRENCY = 5
# IB's historical-data pacing limit is ~60 requests per rolling 10-minute
# window -- 11s between dispatches keeps a full run to ~55/10min, with a
# safety margin rather than cutting it exactly at the ceiling.
_PACING_GAP_SEC = 11.0


class _PacingGate:
    """Serializes only the moment a new IB historical-data request gets
    dispatched, spacing successive dispatches at least _PACING_GAP_SEC
    apart -- what happens after each task passes through (awaiting its own
    fetch_history call) still overlaps freely with the others."""

    def __init__(self, min_gap_sec: float):
        self._min_gap = min_gap_sec
        self._lock = asyncio.Lock()
        self._last_dispatch = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            remaining = self._last_dispatch + self._min_gap - now
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_dispatch = time.monotonic()


async def _process_symbol(
    broker: BrokerConnection,
    strategy: Strategy,
    settings: Settings,
    spec: SymbolSpec,
    sem: asyncio.Semaphore,
    pacing: _PacingGate,
) -> tuple[str, list[Trade]]:
    """Qualifies + fetches history for one symbol (bounded by `sem` so only
    _FETCH_CONCURRENCY IB requests are in flight at once, paced by `pacing`
    so new ones aren't dispatched faster than IB's budget allows), then
    simulates. Returns (print-ready summary line, trades) -- errors are
    caught and turned into a line rather than raised, so one bad symbol
    doesn't cancel the others' already-in-flight requests."""
    preset = BUILTIN_MARKETS[spec.market]
    async with sem:
        try:
            contract = await broker.qualify_contract(
                spec.security_type, spec.symbol, spec.exchange, spec.currency
            )
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest
            return f"{spec.symbol:8s} SKIPPED: {exc}", []

        is_crypto = spec.security_type == "CRYPTO"
        await pacing.wait_turn()
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
            return f"{spec.symbol:8s} SKIPPED (history fetch failed): {exc}", []

    if df is None or df.empty:
        return f"{spec.symbol:8s} no historical data returned", []

    trades = simulate(
        df,
        strategy,
        spec.symbol,
        settings.ema_fast,
        settings.ema_slow,
        settings.rsi_period,
        settings.atr_period,
        settings.stop_atr_mult,
        settings.target_atr_mult,
        vwap_tz=preset.timezone,
        trend_ema_period=settings.trend_ema_period,
        commission_per_share=settings.backtest_commission_per_share,
        slippage_bps=settings.backtest_slippage_bps,
    )
    s = summarize(trades)
    pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    line = (
        f"{spec.symbol:8s} bars={len(df):5d} trades={s.trade_count:3d} "
        f"win_rate={s.win_rate:5.0%} avg_R={s.avg_r:+.2f} profit_factor={pf} "
        f"max_dd_R={s.max_drawdown_r:.2f} max_consec_loss={s.max_consecutive_losses}"
    )
    return line, trades


async def run() -> None:
    settings = load_settings()
    strategy = build_strategy(settings)
    print(f"Strategy: {settings.strategy}  Duration: {settings.backtest_duration}  "
          f"Bar size: {settings.bar_size}")
    print(
        f"Fill model: entries at next bar's open, slippage={settings.backtest_slippage_bps:g} bps "
        f"per market fill, commission={settings.backtest_commission_per_share:g}/share/side\n"
    )

    # Distinct client id so this can run alongside the live/paper engine
    # (same host/port) without IB rejecting one connection as a duplicate.
    backtest_settings = settings.model_copy(
        update={"ib_client_id": settings.ib_client_id + 50}
    )
    broker = BrokerConnection(backtest_settings)
    await broker.connect_with_retry()

    all_trades: list[Trade] = []
    try:
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
        pacing = _PacingGate(_PACING_GAP_SEC)
        tasks = [
            asyncio.create_task(_process_symbol(broker, strategy, settings, spec, sem, pacing))
            for spec in settings.symbol_specs
        ]
        # as_completed rather than gather: prints progress as each symbol
        # finishes (order varies run to run) instead of going silent until
        # everything's done, which matters for a run that can take minutes.
        for coro in asyncio.as_completed(tasks):
            line, trades = await coro
            print(line)
            all_trades.extend(trades)
    finally:
        broker.disconnect()

    print("\n=== Aggregate (all symbols pooled) ===")
    # win_rate/avg_R/profit_factor/sharpe don't depend on order, but
    # max_drawdown_r and max_consecutive_losses do -- all_trades was
    # accumulated in symbol-completion order, not merged chronologically,
    # so sort by exit time first or those two numbers reflect a nonsensical
    # sequence instead of a real cross-symbol timeline.
    all_trades.sort(key=lambda t: t.exit_time)
    overall = summarize(all_trades)
    pf = "inf" if overall.profit_factor == float("inf") else f"{overall.profit_factor:.2f}"
    print(
        f"trades={overall.trade_count}  win_rate={overall.win_rate:.1%}  "
        f"avg_R={overall.avg_r:+.3f}  profit_factor={pf}  "
        f"max_dd_R={overall.max_drawdown_r:.2f}  trade_R_sharpe={overall.trade_r_sharpe:.2f}  "
        f"max_consec_loss={overall.max_consecutive_losses}"
    )
    if overall.trade_count > 0:
        verdict = "positive" if overall.avg_r > 0 else "negative"
        print(
            f"\nExpectancy is {verdict} ({overall.avg_r:+.3f} R/trade average), "
            "after the configured slippage/commission model with next-bar-open "
            "entry fills. Treat this as a rough signal, not proof -- a "
            "good-looking window can still be overfit to that window."
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet IB connection chatter
    asyncio.run(run())


if __name__ == "__main__":
    main()
