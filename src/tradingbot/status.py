"""Pure(ish) status/analytics gathering, shared by the CLI report
(tradingbot.report) and the live web dashboard (tradingbot.webapp) so
there's exactly one place that knows how to read account/position state
from IB and the persisted shadow-trade/news-analysis journals."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ib_async import ExecutionFilter

from tradingbot.broker.connection import BrokerConnection


def read_jsonl(path: Path) -> list[dict]:
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


@dataclass
class PositionStatus:
    symbol: str
    quantity: float
    avg_cost: float
    unrealized_pnl: float


@dataclass
class AccountStatus:
    equity: float
    base_currency: str
    positions: list[PositionStatus] = field(default_factory=list)
    realized_pnl_by_symbol: dict[str, float] = field(default_factory=dict)
    fills_by_symbol: dict[str, int] | None = None  # None if not requested


async def gather_account_status(
    broker: BrokerConnection, include_fills: bool = True
) -> AccountStatus:
    """Reads live-cached ib_async state -- positions()/portfolio() are local
    reads of data already kept fresh by the connection's streaming account
    subscription, not new IB requests. Only reqExecutionsAsync (for
    `fills_by_symbol`) makes an actual round trip, so callers that poll this
    frequently (e.g. a live dashboard) can skip it via include_fills=False."""
    portfolio = broker.ib.portfolio()
    positions_raw = [p for p in broker.ib.positions() if p.position != 0]
    by_conid = {p.contract.conId: p for p in portfolio}
    positions = [
        PositionStatus(
            symbol=p.contract.symbol,
            quantity=p.position,
            avg_cost=p.avgCost,
            unrealized_pnl=(
                by_conid[p.contract.conId].unrealizedPNL if p.contract.conId in by_conid else 0.0
            ),
        )
        for p in positions_raw
    ]
    # Read from the broker's own running tracker (see BrokerConnection),
    # not derived fresh from portfolio() -- that cache drops a symbol the
    # instant its position flattens to 0, taking its realizedPNL with it.
    realized_pnl_by_symbol = {
        symbol: pnl
        for symbol, pnl in broker.realized_pnl_by_symbol.items()
        if pnl not in (None, 0.0)
    }

    fills_by_symbol: dict[str, int] | None = None
    if include_fills:
        fills = await broker.ib.reqExecutionsAsync(ExecutionFilter())
        counts: dict[str, int] = defaultdict(int)
        for f in fills:
            counts[f.contract.symbol] += 1
        fills_by_symbol = dict(counts)

    return AccountStatus(
        equity=broker.account_net_liquidation(),
        base_currency=broker.account_base_currency(),
        positions=positions,
        realized_pnl_by_symbol=realized_pnl_by_symbol,
        fills_by_symbol=fills_by_symbol,
    )


@dataclass
class ShadowTradingStatus:
    closed: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float
    avg_r: float


def gather_shadow_trading_status(path: Path) -> ShadowTradingStatus:
    trades = read_jsonl(path)
    wins = [t for t in trades if t.get("status") == "WIN"]
    losses = [t for t in trades if t.get("status") == "LOSS"]
    timeouts = [t for t in trades if t.get("status") == "TIMEOUT"]
    r_values = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    avg_r = sum(r_values) / len(r_values) if r_values else 0.0
    win_rate = len(wins) / len(trades) if trades else 0.0
    return ShadowTradingStatus(
        closed=len(trades),
        wins=len(wins),
        losses=len(losses),
        timeouts=len(timeouts),
        win_rate=win_rate,
        avg_r=avg_r,
    )


@dataclass
class NewsAnalysisStatus:
    assessed: int
    skipped: int
    avg_confidence: float
    direction_counts: dict[str, int] = field(default_factory=dict)


def gather_news_analysis_status(path: Path) -> NewsAnalysisStatus:
    records = read_jsonl(path)
    assessed = [r for r in records if r.get("skipped_reason") is None]
    skipped = [r for r in records if r.get("skipped_reason") is not None]
    by_direction: dict[str, int] = defaultdict(int)
    for r in assessed:
        by_direction[r.get("direction") or "NONE"] += 1
    confidences = [r["confidence"] for r in assessed if r.get("confidence") is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return NewsAnalysisStatus(
        assessed=len(assessed),
        skipped=len(skipped),
        avg_confidence=avg_confidence,
        direction_counts=dict(by_direction),
    )


def gather_latest_news_by_symbol(path: Path) -> dict[str, dict]:
    """Most recent real assessment (not a shadow-open skip) per symbol --
    later records in the append-only journal overwrite earlier ones, so
    what's left is each symbol's latest reading."""
    latest: dict[str, dict] = {}
    for record in read_jsonl(path):
        if record.get("skipped_reason") is not None:
            continue
        symbol = record.get("symbol")
        if symbol:
            latest[symbol] = record
    return latest
