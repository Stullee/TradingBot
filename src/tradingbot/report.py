"""Standalone status/analytics report: `python -m tradingbot.report`

Answers "how many trades have been done, how successful, how's the news
signal doing" without hand-parsing log files. Read-only: connects to IB
only to read account/position/execution state, places no orders, and
connects with a different client id than the live engine so both can run
at the same time. For a live, continuously-updating version of the same
data, see tradingbot.webapp."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import load_settings
from tradingbot.status import (
    AccountStatus,
    NewsAnalysisStatus,
    ShadowTradingStatus,
    gather_account_status,
    gather_news_analysis_status,
    gather_shadow_trading_status,
)

log = logging.getLogger(__name__)


def _print_account(status: AccountStatus) -> None:
    print(f"=== Account ===\nEquity: {status.equity:,.2f} {status.base_currency}\n")

    print(f"=== Open Positions ({len(status.positions)}) ===")
    if not status.positions:
        print("(none)")
    else:
        for p in status.positions:
            print(
                f"{p.symbol:8s} qty={p.quantity:>10.2f} "
                f"avgCost={p.avg_cost:>10.4f} unrealizedPNL={p.unrealized_pnl:>10.2f}"
            )
    print()

    print(f"=== Today's Realized P&L by Symbol ({len(status.realized_pnl_by_symbol)} symbols) ===")
    if not status.realized_pnl_by_symbol:
        print("(none yet)")
    else:
        total = 0.0
        for symbol, pnl in sorted(status.realized_pnl_by_symbol.items(), key=lambda x: -x[1]):
            print(f"{symbol:8s} realizedPNL={pnl:>10.2f}")
            total += pnl
        print(f"{'TOTAL':8s} realizedPNL={total:>10.2f}")
    print()

    fills = status.fills_by_symbol or {}
    total_fills = sum(fills.values())
    print(f"=== Today's Fills ({total_fills} total across {len(fills)} symbols) ===")
    if not fills:
        print("(none yet)")
    else:
        for symbol, count in sorted(fills.items(), key=lambda x: -x[1]):
            print(f"{symbol:8s} fills={count}")
    print(
        "\nNote: this counts individual fills, not round-trip trades -- one "
        "position entry/exit can be several partial fills. Use the realized "
        "P&L above for actual profitability, this is an activity count.\n"
    )


def _print_shadow_trading(status: ShadowTradingStatus, path: Path) -> None:
    print(f"=== News Shadow-Trading ({path.name}) ===")
    if status.closed == 0:
        print("No shadow trades closed yet.\n")
        return
    print(
        f"Closed: {status.closed}  Win: {status.wins}  Loss: {status.losses}  "
        f"Timeout: {status.timeouts}  Flattened: {status.flattened}  "
        f"Win rate: {status.win_rate:.0%}  Avg R: {status.avg_r:+.2f}\n"
    )


def _print_news_analysis(status: NewsAnalysisStatus, path: Path) -> None:
    print(f"=== News Analysis ({path.name}) ===")
    if status.assessed == 0 and status.skipped == 0:
        print("No articles assessed yet.\n")
        return
    print(
        f"Batches assessed: {status.assessed}  Skipped (shadow trade already open): "
        f"{status.skipped}  Avg confidence: {status.avg_confidence:.2f}"
    )
    print(f"Direction breakdown: {status.direction_counts}\n")


async def run() -> None:
    settings = load_settings()

    # Distinct client id so this can run alongside the live/paper engine
    # (same host/port) without IB rejecting one connection as a duplicate.
    report_settings = settings.model_copy(update={"ib_client_id": settings.ib_client_id + 99})
    broker = BrokerConnection(report_settings)
    await broker.connect_with_retry()
    try:
        account_status = await gather_account_status(broker)
    finally:
        broker.disconnect()
    _print_account(account_status)

    log_dir = Path(settings.log_dir)
    shadow_path = log_dir / "shadow_trades.jsonl"
    news_path = log_dir / "news_analysis.jsonl"
    _print_shadow_trading(gather_shadow_trading_status(shadow_path), shadow_path)
    _print_news_analysis(gather_news_analysis_status(news_path), news_path)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet IB connection chatter
    asyncio.run(run())


if __name__ == "__main__":
    main()
