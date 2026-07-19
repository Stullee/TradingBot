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
from tradingbot.eod import format_eod_text, load_latest_eod
from tradingbot.status import (
    AccountStatus,
    NewsAnalysisStatus,
    ShadowTradingStatus,
    gather_account_status,
    gather_live_trades_status,
    gather_news_analysis_status,
    gather_shadow_trading_status,
    read_jsonl,
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


def _print_live_trades(path: Path) -> None:
    stats, recent = gather_live_trades_status(path)
    print(f"=== Real Trades ({path.name}, net of commissions) ===")
    if stats.closed == 0:
        print("No completed round trips journaled yet.\n")
        return
    avg_r = "n/a" if stats.avg_r is None else f"{stats.avg_r:+.2f}"
    pnl = ", ".join(f"{v:+,.2f} {c}" for c, v in stats.net_pnl_by_currency.items()) or "-"
    today = ", ".join(f"{v:+,.2f} {c}" for c, v in stats.today_net_pnl_by_currency.items()) or "-"
    print(
        f"Closed: {stats.closed}  Win rate: {stats.win_rate:.0%}  Avg R (net): {avg_r}\n"
        f"Net P&L all time: {pnl}   today: {today}"
    )
    if stats.r_by_symbol:
        ranked = sorted(stats.r_by_symbol.items(), key=lambda x: x[1])
        worst = ", ".join(f"{s} {r:+.1f}R" for s, r in ranked[:3])
        best = ", ".join(f"{s} {r:+.1f}R" for s, r in ranked[-3:][::-1])
        print(f"Best symbols: {best}\nWorst symbols: {worst}")
    if recent:
        print("Recent:")
        for t in recent[-5:]:
            r = "n/a" if t.get("r_multiple") is None else f"{t['r_multiple']:+.2f}R"
            print(
                f"  {(t.get('closed_at') or '')[:16]:16s} {t.get('symbol', ''):8s} "
                f"{t.get('direction', ''):5s} net={t.get('net_pnl', 0):+.2f} {t.get('currency', '')} ({r})"
            )
    print()


def _print_advisor(path: Path) -> None:
    reports = read_jsonl(path)
    print(f"=== AI Advisor ({path.name}) ===")
    if not reports:
        print("No advisor reports yet (ENABLE_AI_ADVISOR=false, or none generated).\n")
        return
    latest = reports[-1]
    print(f"[{latest.get('health', '?')}] {latest.get('generated_at', '')}")
    print(latest.get("assessment", ""))
    for rec in latest.get("recommendations", []):
        print(f"  - ({rec.get('priority')}) {rec.get('title')}: {rec.get('detail')}")
    print()


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
    if status.assessed == 0:
        print("No articles assessed yet.\n")
        return
    print(f"Batches assessed: {status.assessed}  Avg confidence: {status.avg_confidence:.2f}")
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
    _print_live_trades(log_dir / "trades.jsonl")
    shadow_path = log_dir / "shadow_trades.jsonl"
    news_path = log_dir / "news_analysis.jsonl"
    _print_shadow_trading(gather_shadow_trading_status(shadow_path), shadow_path)
    _print_news_analysis(gather_news_analysis_status(news_path), news_path)
    _print_advisor(log_dir / "advisor_reports.jsonl")

    latest_eod = load_latest_eod(log_dir)
    if latest_eod is not None:
        print(format_eod_text(latest_eod))
    else:
        print(
            "No end-of-day summary generated yet (the engine emits one at each "
            "UTC day rollover; `python -m tradingbot.eod` builds one on demand)."
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet IB connection chatter
    asyncio.run(run())


if __name__ == "__main__":
    main()
