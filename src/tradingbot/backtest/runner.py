"""Standalone backtest CLI: `python -m tradingbot.backtest.runner`

Fetches historical bars for the configured symbol universe (same MARKETS/
SYMBOLS config as live trading) and runs the configured strategy against
them bar-by-bar, printing per-symbol and pooled-aggregate expectancy stats.
Uses BACKTEST_DURATION (see config.py) for how far back to pull.

This never places orders or touches the account -- it only requests
historical data, and connects with a different client id (live client id +
50) than the live/paper engine so both can run at the same time without IB
rejecting one for a duplicate client id."""
from __future__ import annotations

import asyncio
import logging

from tradingbot.backtest.fetch import fetch_history
from tradingbot.backtest.simulator import simulate
from tradingbot.backtest.stats import summarize
from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import load_settings
from tradingbot.engine import build_strategy
from tradingbot.markets import BUILTIN_MARKETS

log = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    strategy = build_strategy(settings)
    print(f"Strategy: {settings.strategy}  Duration: {settings.backtest_duration}  "
          f"Bar size: {settings.bar_size}\n")

    # Distinct client id so this can run alongside the live/paper engine
    # (same host/port) without IB rejecting one connection as a duplicate.
    backtest_settings = settings.model_copy(
        update={"ib_client_id": settings.ib_client_id + 50}
    )
    broker = BrokerConnection(backtest_settings)
    await broker.connect_with_retry()

    all_trades = []
    try:
        for spec in settings.symbol_specs:
            preset = BUILTIN_MARKETS[spec.market]
            try:
                contract = await broker.qualify_contract(
                    spec.security_type, spec.symbol, spec.exchange, spec.currency
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest
                print(f"{spec.symbol:8s} SKIPPED: {exc}")
                continue

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
                print(f"{spec.symbol:8s} SKIPPED (history fetch failed): {exc}")
                continue

            if df is None or df.empty:
                print(f"{spec.symbol:8s} no historical data returned")
                continue

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
            )
            all_trades.extend(trades)
            s = summarize(trades)
            pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
            print(
                f"{spec.symbol:8s} bars={len(df):5d} trades={s.trade_count:3d} "
                f"win_rate={s.win_rate:5.0%} avg_R={s.avg_r:+.2f} profit_factor={pf} "
                f"max_dd_R={s.max_drawdown_r:.2f} max_consec_loss={s.max_consecutive_losses}"
            )
    finally:
        broker.disconnect()

    print("\n=== Aggregate (all symbols pooled) ===")
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
            f"\nExpectancy is {verdict} ({overall.avg_r:+.3f} R/trade average). "
            "Treat this as a rough signal, not proof -- see the backtesting "
            "caveats (overfitting, look-ahead bias, no slippage/commission "
            "modeling) before trusting it too far."
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet IB connection chatter
    asyncio.run(run())


if __name__ == "__main__":
    main()
