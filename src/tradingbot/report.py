"""Standalone status/analytics report: `python -m tradingbot.report`

Answers "how many trades have been done, how successful, how's the news
signal doing" without hand-parsing log files. Read-only: connects to IB
only to read account/position/execution state, places no orders, and
connects with a different client id than the live engine so both can run
at the same time."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path

from ib_async import ExecutionFilter

from tradingbot.broker.connection import BrokerConnection
from tradingbot.config import load_settings

log = logging.getLogger(__name__)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


async def _print_account_and_positions(broker: BrokerConnection) -> None:
    equity = broker.account_net_liquidation()
    base_ccy = broker.account_base_currency()
    print(f"=== Account ===\nEquity: {equity:,.2f} {base_ccy}\n")

    portfolio = broker.ib.portfolio()
    positions = [p for p in broker.ib.positions() if p.position != 0]
    print(f"=== Open Positions ({len(positions)}) ===")
    if not positions:
        print("(none)")
    else:
        by_conid = {p.contract.conId: p for p in portfolio}
        for p in positions:
            item = by_conid.get(p.contract.conId)
            unrealized = item.unrealizedPNL if item else 0.0
            print(
                f"{p.contract.symbol:8s} qty={p.position:>10.2f} "
                f"avgCost={p.avgCost:>10.4f} unrealizedPNL={unrealized:>10.2f}"
            )
    print()

    realized = [p for p in portfolio if p.realizedPNL not in (None, 0.0)]
    print(f"=== Today's Realized P&L by Symbol ({len(realized)} symbols) ===")
    if not realized:
        print("(none yet)")
    else:
        total = 0.0
        for p in sorted(realized, key=lambda x: -x.realizedPNL):
            print(f"{p.contract.symbol:8s} realizedPNL={p.realizedPNL:>10.2f}")
            total += p.realizedPNL
        print(f"{'TOTAL':8s} realizedPNL={total:>10.2f}")
    print()


async def _print_todays_fills(broker: BrokerConnection) -> None:
    fills = await broker.ib.reqExecutionsAsync(ExecutionFilter())
    by_symbol: dict[str, int] = defaultdict(int)
    for f in fills:
        by_symbol[f.contract.symbol] += 1
    total = sum(by_symbol.values())
    print(f"=== Today's Fills ({total} total across {len(by_symbol)} symbols) ===")
    if not by_symbol:
        print("(none yet)")
    else:
        for symbol, count in sorted(by_symbol.items(), key=lambda x: -x[1]):
            print(f"{symbol:8s} fills={count}")
    print(
        "\nNote: this counts individual fills, not round-trip trades -- one "
        "position entry/exit can be several partial fills. Use the realized "
        "P&L above for actual profitability, this is an activity count.\n"
    )


def _print_shadow_trading_stats(path: Path) -> None:
    trades = _read_jsonl(path)
    print(f"=== News Shadow-Trading ({path.name}) ===")
    if not trades:
        print("No shadow trades closed yet.\n")
        return

    wins = [t for t in trades if t.get("status") == "WIN"]
    losses = [t for t in trades if t.get("status") == "LOSS"]
    timeouts = [t for t in trades if t.get("status") == "TIMEOUT"]
    r_values = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    avg_r = sum(r_values) / len(r_values) if r_values else 0.0
    win_rate = len(wins) / len(trades) if trades else 0.0
    print(
        f"Closed: {len(trades)}  Win: {len(wins)}  Loss: {len(losses)}  "
        f"Timeout: {len(timeouts)}  Win rate: {win_rate:.0%}  Avg R: {avg_r:+.2f}\n"
    )


def _print_news_analysis_stats(path: Path) -> None:
    records = _read_jsonl(path)
    print(f"=== News Analysis ({path.name}) ===")
    if not records:
        print("No articles assessed yet.\n")
        return

    assessed = [r for r in records if r.get("skipped_reason") is None]
    skipped = [r for r in records if r.get("skipped_reason") is not None]
    by_direction: dict[str, int] = defaultdict(int)
    for r in assessed:
        by_direction[r.get("direction") or "NONE"] += 1
    confidences = [r["confidence"] for r in assessed if r.get("confidence") is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    print(
        f"Batches assessed: {len(assessed)}  Skipped (shadow trade already open): "
        f"{len(skipped)}  Avg confidence: {avg_confidence:.2f}"
    )
    print(f"Direction breakdown: {dict(by_direction)}\n")


async def run() -> None:
    settings = load_settings()

    # Distinct client id so this can run alongside the live/paper engine
    # (same host/port) without IB rejecting one connection as a duplicate.
    report_settings = settings.model_copy(update={"ib_client_id": settings.ib_client_id + 99})
    broker = BrokerConnection(report_settings)
    await broker.connect_with_retry()
    try:
        await _print_account_and_positions(broker)
        await _print_todays_fills(broker)
    finally:
        broker.disconnect()

    log_dir = Path(settings.log_dir)
    _print_shadow_trading_stats(log_dir / "shadow_trades.jsonl")
    _print_news_analysis_stats(log_dir / "news_analysis.jsonl")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # quiet IB connection chatter
    asyncio.run(run())


if __name__ == "__main__":
    main()
