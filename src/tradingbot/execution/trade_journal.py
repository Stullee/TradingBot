"""Journal of the bot's *real* trades: every fill (with commission) and every
completed round trip, appended to logs/trades.jsonl.

This is the ground truth for "is the bot actually profitable": win rate,
average R and net P&L here include commissions and real fill prices --
unlike the backtester (a model) or the day's realized P&L (which IB resets
and which says nothing about per-trade expectancy). Round trips carry the
entry's original stop distance (recorded by the engine at order placement),
so live results are directly comparable to backtest/shadow R-multiples.

Restart-safe: fills are deduplicated by IB execution id (IB replays the
day's executions on every reconnect), already-journaled ids are reloaded
from the file at startup, and open-position entry context is persisted to a
sidecar file so a round trip closed after a restart still gets its R."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ib_async import IB, Fill, Trade

log = logging.getLogger(__name__)

# A commission report normally follows its execution within milliseconds; a
# pending fill older than this gets journaled without one rather than lost.
_PENDING_COMMISSION_MAX_AGE_SEC = 60.0

# IB uses DBL_MAX as "unset" in commission reports.
_UNSET_THRESHOLD = 1e300


def _sign(x: float) -> float:
    return 1.0 if x > 0 else -1.0


@dataclass
class EntryContext:
    """What the engine knew when it placed the entry -- lets the eventual
    round trip report an R-multiple against the *intended* risk."""

    direction: str  # "LONG" | "SHORT"
    entry_ref_price: float
    stop_price: float
    target_price: float
    strategy: str
    placed_at: str  # ISO


@dataclass
class _Cycle:
    """Accumulates fills from flat back to flat for one symbol."""

    symbol: str
    currency: str
    direction: float  # +1 long cycle, -1 short cycle
    opened_at: str | None
    position: float = 0.0
    entry_qty: float = 0.0
    entry_value: float = 0.0
    exit_qty: float = 0.0
    exit_value: float = 0.0
    commissions: float = 0.0
    seeded: bool = False  # started from a pre-existing position, entry avg is IB's avgCost


@dataclass
class LiveTradeStats:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_r: float | None = None
    net_pnl_by_currency: dict[str, float] = field(default_factory=dict)
    today_closed: int = 0
    today_net_pnl_by_currency: dict[str, float] = field(default_factory=dict)
    r_by_symbol: dict[str, float] = field(default_factory=dict)


class TradeJournal:
    def __init__(self, path: Path):
        self.path = path
        self.context_path = path.parent / "trade_entry_context.json"
        self._contexts: dict[str, EntryContext] = self._load_contexts()
        self._cycles: dict[str, _Cycle] = {}
        self._pending: dict[str, tuple[Fill, float]] = {}
        self._seen_exec_ids: set[str] = self._load_seen_exec_ids()

    # --- wiring ------------------------------------------------------------
    def attach(self, ib: IB) -> None:
        ib.execDetailsEvent += self._on_exec_details
        ib.commissionReportEvent += self._on_commission_report

    def seed_positions(self, items: list[tuple[str, float, float, str]]) -> None:
        """(symbol, signed_qty, avg_cost_per_unit, currency) for positions
        already open when the journal attaches (restart mid-position) -- so
        their eventual closing fills pair against a sensible entry instead
        of looking like an orphan exit."""
        for symbol, qty, avg_cost, currency in items:
            if qty == 0 or symbol in self._cycles:
                continue
            self._cycles[symbol] = _Cycle(
                symbol=symbol,
                currency=currency,
                direction=_sign(qty),
                opened_at=None,
                position=qty,
                entry_qty=abs(qty),
                entry_value=abs(qty) * avg_cost,
                seeded=True,
            )

    def record_entry_context(
        self,
        symbol: str,
        direction: str,
        entry_ref_price: float,
        stop_price: float,
        target_price: float,
        strategy: str,
    ) -> None:
        self._contexts[symbol] = EntryContext(
            direction=direction,
            entry_ref_price=entry_ref_price,
            stop_price=stop_price,
            target_price=target_price,
            strategy=strategy,
            placed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write_contexts()

    def stop_price_for(self, symbol: str) -> float | None:
        """The stop the engine intended for this symbol's current position,
        if known -- used by the protective-stop reconciliation pass."""
        ctx = self._contexts.get(symbol)
        return ctx.stop_price if ctx else None

    # --- persistence -------------------------------------------------------
    def _load_contexts(self) -> dict[str, EntryContext]:
        if not self.context_path.exists():
            return {}
        try:
            raw = json.loads(self.context_path.read_text())
            return {s: EntryContext(**fields) for s, fields in raw.items()}
        except (json.JSONDecodeError, OSError, TypeError):
            log.warning("Could not read %s, starting with no entry context.", self.context_path)
            return {}

    def _write_contexts(self) -> None:
        try:
            self.context_path.parent.mkdir(parents=True, exist_ok=True)
            self.context_path.write_text(
                json.dumps({s: asdict(c) for s, c in self._contexts.items()})
            )
        except OSError:
            log.warning("Could not persist entry context to %s", self.context_path)

    def _load_seen_exec_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        seen: set[str] = set()
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "fill" and record.get("exec_id"):
                    seen.add(record["exec_id"])
        return seen

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    # --- event handlers ----------------------------------------------------
    def _on_exec_details(self, trade: Trade, fill: Fill) -> None:
        exec_id = fill.execution.execId
        if not exec_id or exec_id in self._seen_exec_ids:
            return
        self._pending[exec_id] = (fill, time.monotonic())
        self._flush_stale_pending()

    def _on_commission_report(self, trade: Trade, fill: Fill, report) -> None:
        exec_id = fill.execution.execId
        if not exec_id or exec_id in self._seen_exec_ids:
            return
        self._pending.pop(exec_id, None)
        commission = report.commission if abs(report.commission or 0.0) < _UNSET_THRESHOLD else 0.0
        realized = report.realizedPNL
        if realized is not None and (realized == 0.0 or abs(realized) >= _UNSET_THRESHOLD):
            realized = None
        self._record_fill(fill, commission=commission or 0.0, realized_pnl=realized)

    def _flush_stale_pending(self) -> None:
        now = time.monotonic()
        for exec_id, (fill, seen_at) in list(self._pending.items()):
            if now - seen_at > _PENDING_COMMISSION_MAX_AGE_SEC:
                del self._pending[exec_id]
                log.warning(
                    "No commission report for execution %s (%s) -- journaling without one.",
                    exec_id,
                    fill.contract.symbol,
                )
                self._record_fill(fill, commission=0.0, realized_pnl=None)

    # --- core --------------------------------------------------------------
    def _record_fill(self, fill: Fill, commission: float, realized_pnl: float | None) -> None:
        execution = fill.execution
        self._seen_exec_ids.add(execution.execId)
        signed_qty = float(execution.shares) * (1.0 if execution.side == "BOT" else -1.0)
        fill_time = fill.time or datetime.now(timezone.utc)
        time_iso = fill_time.isoformat()
        symbol = fill.contract.symbol
        self._append(
            {
                "type": "fill",
                "symbol": symbol,
                "currency": fill.contract.currency,
                "side": execution.side,
                "qty": abs(signed_qty),
                "price": execution.price,
                "commission": commission,
                "realized_pnl": realized_pnl,
                "exec_id": execution.execId,
                "time": time_iso,
            }
        )
        self._apply_fill(
            symbol, fill.contract.currency, signed_qty, execution.price, commission, time_iso
        )

    def _apply_fill(
        self,
        symbol: str,
        currency: str,
        signed_qty: float,
        price: float,
        commission: float,
        time_iso: str,
    ) -> None:
        per_unit_commission = commission / abs(signed_qty) if signed_qty else 0.0
        remaining = signed_qty
        while abs(remaining) > 1e-12:
            cycle = self._cycles.get(symbol)
            if cycle is None:
                cycle = _Cycle(
                    symbol=symbol,
                    currency=currency,
                    direction=_sign(remaining),
                    opened_at=time_iso,
                )
                self._cycles[symbol] = cycle

            if _sign(remaining) == cycle.direction:
                qty = abs(remaining)
                cycle.entry_qty += qty
                cycle.entry_value += qty * price
            else:
                qty = min(abs(remaining), abs(cycle.position))
                if qty <= 0:
                    # Degenerate (opposite fill against an empty cycle) --
                    # drop the empty cycle and re-loop to open a fresh one.
                    del self._cycles[symbol]
                    continue
                cycle.exit_qty += qty
                cycle.exit_value += qty * price

            cycle.position += _sign(remaining) * qty
            cycle.commissions += per_unit_commission * qty
            remaining -= _sign(remaining) * qty

            if cycle.exit_qty > 0 and abs(cycle.position) < 1e-9:
                self._finalize_cycle(cycle, closed_at=time_iso)
                del self._cycles[symbol]

    def _finalize_cycle(self, cycle: _Cycle, closed_at: str) -> None:
        qty = min(cycle.entry_qty, cycle.exit_qty)
        if qty <= 0 or cycle.entry_qty <= 0 or cycle.exit_qty <= 0:
            return
        avg_entry = cycle.entry_value / cycle.entry_qty
        avg_exit = cycle.exit_value / cycle.exit_qty
        gross = (avg_exit - avg_entry) * qty * cycle.direction
        net = gross - cycle.commissions

        context = self._contexts.pop(cycle.symbol, None)
        self._write_contexts()
        r_multiple = None
        if context is not None:
            risk_per_unit = abs(context.entry_ref_price - context.stop_price)
            if risk_per_unit > 0:
                r_multiple = round((net / qty) / risk_per_unit, 3)

        record = {
            "type": "round_trip",
            "symbol": cycle.symbol,
            "currency": cycle.currency,
            "direction": "LONG" if cycle.direction > 0 else "SHORT",
            "qty": qty,
            "avg_entry": round(avg_entry, 6),
            "avg_exit": round(avg_exit, 6),
            "gross_pnl": round(gross, 4),
            "commissions": round(cycle.commissions, 4),
            "net_pnl": round(net, 4),
            "r_multiple": r_multiple,
            "strategy": context.strategy if context else None,
            "intended_stop": context.stop_price if context else None,
            "intended_target": context.target_price if context else None,
            "seeded_entry": cycle.seeded,
            "opened_at": cycle.opened_at,
            "closed_at": closed_at,
        }
        self._append(record)
        log.info(
            "[JOURNAL] Round trip %s %s x%s: net %.2f %s (R=%s)",
            record["direction"],
            cycle.symbol,
            qty,
            net,
            cycle.currency,
            "n/a" if r_multiple is None else f"{r_multiple:+.2f}",
        )


def summarize_live_trades(path: Path, recent_limit: int = 10) -> tuple[LiveTradeStats, list[dict]]:
    """Aggregates the journal's round_trip records: overall + today's net
    P&L (per currency -- cross-currency sums would be meaningless without FX
    conversion), win rate and average R, plus the most recent trades for
    display. Returns (stats, recent_round_trips)."""
    stats = LiveTradeStats()
    if not path.exists():
        return stats, []
    round_trips: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "round_trip":
                round_trips.append(record)

    today = datetime.now(timezone.utc).date().isoformat()
    r_values: list[float] = []
    for t in round_trips:
        stats.closed += 1
        net = t.get("net_pnl") or 0.0
        ccy = t.get("currency") or "USD"
        stats.net_pnl_by_currency[ccy] = stats.net_pnl_by_currency.get(ccy, 0.0) + net
        if net > 0:
            stats.wins += 1
        else:
            stats.losses += 1
        if t.get("r_multiple") is not None:
            r_values.append(t["r_multiple"])
        symbol = t.get("symbol") or "?"
        if t.get("r_multiple") is not None:
            stats.r_by_symbol[symbol] = round(
                stats.r_by_symbol.get(symbol, 0.0) + t["r_multiple"], 3
            )
        closed_at = t.get("closed_at") or ""
        if closed_at[:10] == today:
            stats.today_closed += 1
            stats.today_net_pnl_by_currency[ccy] = (
                stats.today_net_pnl_by_currency.get(ccy, 0.0) + net
            )

    if stats.closed:
        stats.win_rate = stats.wins / stats.closed
    if r_values:
        stats.avg_r = sum(r_values) / len(r_values)
    return stats, round_trips[-recent_limit:]
