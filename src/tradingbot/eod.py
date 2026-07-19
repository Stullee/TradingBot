"""End-of-day summary: one structured record per UTC trading day answering
"what happened today and was it right" without grepping logs.

Sources (all already persisted by the live engine):
  - logs/trades.jsonl          real round trips + fills (net P&L, R, costs)
  - logs/shadow_trades.jsonl   news shadow-trade outcomes
  - logs/news_analysis.jsonl   news batches assessed
  - risk state                 day/week baselines + kill switches (live only)

The live engine emits one automatically at the UTC day rollover (23:55 UTC
crypto flatten happens first, so the day is complete), appends it to
logs/eod_reports.jsonl, logs a readable block, and pushes a compact version
to the alert webhook. `python -m tradingbot.eod [YYYY-MM-DD]` builds one
on demand from the journals alone (no IB connection; equity fields show as
n/a for days the live risk state doesn't cover)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

EOD_FILENAME = "eod_reports.jsonl"


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


def _on_day(record_ts: str | None, day: date) -> bool:
    return bool(record_ts) and record_ts[:10] == day.isoformat()


def build_eod_summary(
    log_dir: Path,
    day: date,
    equity: float | None = None,
    base_currency: str | None = None,
    day_start_equity: float | None = None,
    week_start_equity: float | None = None,
    daily_kill_switch: bool = False,
    weekly_kill_switch: bool = False,
    advisor_assessment: str | None = None,
) -> dict:
    log_dir = Path(log_dir)

    round_trips = [
        t
        for t in _read_jsonl(log_dir / "trades.jsonl")
        if t.get("type") == "round_trip" and _on_day(t.get("closed_at"), day)
    ]
    fills = [
        t
        for t in _read_jsonl(log_dir / "trades.jsonl")
        if t.get("type") == "fill" and _on_day(t.get("time"), day)
    ]

    wins = [t for t in round_trips if (t.get("net_pnl") or 0) > 0]
    r_values = [t["r_multiple"] for t in round_trips if t.get("r_multiple") is not None]
    net_by_ccy: dict[str, float] = {}
    commissions_by_ccy: dict[str, float] = {}
    r_by_symbol: dict[str, float] = {}
    slippage_bps: list[float] = []
    for t in round_trips:
        ccy = t.get("currency") or "USD"
        net_by_ccy[ccy] = round(net_by_ccy.get(ccy, 0.0) + (t.get("net_pnl") or 0.0), 4)
        commissions_by_ccy[ccy] = round(
            commissions_by_ccy.get(ccy, 0.0) + (t.get("commissions") or 0.0), 4
        )
        if t.get("r_multiple") is not None:
            symbol = t.get("symbol") or "?"
            r_by_symbol[symbol] = round(r_by_symbol.get(symbol, 0.0) + t["r_multiple"], 3)
        intended = t.get("intended_entry")
        if intended and t.get("avg_entry"):
            direction = 1.0 if t.get("direction") == "LONG" else -1.0
            adverse = (t["avg_entry"] - intended) * direction
            slippage_bps.append(adverse / intended * 10_000)

    shadow_closed = [
        t
        for t in _read_jsonl(log_dir / "shadow_trades.jsonl")
        if _on_day(t.get("closed_at"), day)
    ]
    shadow_wins = sum(1 for t in shadow_closed if t.get("status") == "WIN")
    news_batches = [
        r
        for r in _read_jsonl(log_dir / "news_analysis.jsonl")
        if _on_day(r.get("assessed_at"), day) and r.get("skipped_reason") is None
    ]

    day_change_pct = None
    if equity is not None and day_start_equity:
        day_change_pct = round((equity - day_start_equity) / day_start_equity * 100, 3)
    week_change_pct = None
    if equity is not None and week_start_equity:
        week_change_pct = round((equity - week_start_equity) / week_start_equity * 100, 3)

    return {
        "type": "eod_summary",
        "day": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trades_closed": len(round_trips),
        "wins": len(wins),
        "losses": len(round_trips) - len(wins),
        "win_rate": round(len(wins) / len(round_trips), 3) if round_trips else None,
        "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "total_r": round(sum(r_values), 3) if r_values else None,
        "net_pnl_by_currency": net_by_ccy,
        "commissions_by_currency": commissions_by_ccy,
        "r_by_symbol": r_by_symbol,
        "avg_entry_slippage_bps": (
            round(sum(slippage_bps) / len(slippage_bps), 2) if slippage_bps else None
        ),
        "fills": len(fills),
        "equity": equity,
        "base_currency": base_currency,
        "day_start_equity": day_start_equity,
        "day_change_pct": day_change_pct,
        "week_change_pct": week_change_pct,
        "daily_kill_switch": daily_kill_switch,
        "weekly_kill_switch": weekly_kill_switch,
        "shadow_closed": len(shadow_closed),
        "shadow_wins": shadow_wins,
        "news_batches_assessed": len(news_batches),
        "advisor_assessment": advisor_assessment,
    }


def _fmt_ccy_map(values: dict[str, float]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{v:+,.2f} {c}" for c, v in sorted(values.items()))


def format_eod_text(s: dict) -> str:
    """Multi-line human-readable rendering of a summary record."""
    lines = [f"=== End of day {s['day']} (UTC) ==="]
    if s["trades_closed"]:
        lines.append(
            f"Real trades: {s['trades_closed']} closed | {s['wins']}W/{s['losses']}L "
            f"({s['win_rate']:.0%}) | avg R {s['avg_r']:+.2f} | total {s['total_r']:+.2f}R"
            if s["avg_r"] is not None
            else f"Real trades: {s['trades_closed']} closed | {s['wins']}W/{s['losses']}L"
        )
        commissions = ", ".join(
            f"{v:,.2f} {c}" for c, v in sorted(s["commissions_by_currency"].items())
        ) or "-"
        lines.append(
            f"  Net P&L: {_fmt_ccy_map(s['net_pnl_by_currency'])} | "
            f"commissions: {commissions}"
        )
        if s.get("avg_entry_slippage_bps") is not None:
            lines.append(f"  Avg entry slippage: {s['avg_entry_slippage_bps']:+.1f} bps (adverse=+)")
        if s["r_by_symbol"]:
            ranked = sorted(s["r_by_symbol"].items(), key=lambda x: -x[1])
            lines.append(
                "  By symbol: " + ", ".join(f"{sym} {r:+.1f}R" for sym, r in ranked)
            )
    else:
        lines.append("Real trades: none closed today.")

    if s.get("equity") is not None:
        ccy = s.get("base_currency") or ""
        day_part = (
            f" | day {s['day_change_pct']:+.2f}%" if s.get("day_change_pct") is not None else ""
        )
        week_part = (
            f" | week {s['week_change_pct']:+.2f}%" if s.get("week_change_pct") is not None else ""
        )
        lines.append(f"Equity: {s['equity']:,.2f} {ccy}{day_part}{week_part}")

    if s.get("daily_kill_switch") or s.get("weekly_kill_switch"):
        which = [
            name
            for name, active in (
                ("daily", s.get("daily_kill_switch")),
                ("weekly", s.get("weekly_kill_switch")),
            )
            if active
        ]
        lines.append(f"KILL SWITCH ACTIVE: {', '.join(which)}")

    lines.append(
        f"Shadow trades closed: {s['shadow_closed']}"
        + (f" ({s['shadow_wins']} wins)" if s["shadow_closed"] else "")
        + f" | news batches assessed: {s['news_batches_assessed']}"
    )
    if s.get("advisor_assessment"):
        lines.append(f"Advisor: {s['advisor_assessment'][:400]}")
    return "\n".join(lines)


def format_eod_alert(s: dict) -> str:
    """One-paragraph version for the webhook notification."""
    if s["trades_closed"]:
        trades = (
            f"{s['trades_closed']} trades ({s['wins']}W/{s['losses']}L, "
            f"avg {s['avg_r']:+.2f}R), net {_fmt_ccy_map(s['net_pnl_by_currency'])}"
            if s["avg_r"] is not None
            else f"{s['trades_closed']} trades, net {_fmt_ccy_map(s['net_pnl_by_currency'])}"
        )
    else:
        trades = "no trades"
    equity = (
        f" Equity {s['equity']:,.0f} {s.get('base_currency') or ''}"
        + (f" ({s['day_change_pct']:+.2f}% day)" if s.get("day_change_pct") is not None else "")
        if s.get("equity") is not None
        else ""
    )
    kill = ""
    if s.get("daily_kill_switch") or s.get("weekly_kill_switch"):
        kill = " KILL SWITCH ACTIVE."
    return f"{s['day']}: {trades}.{equity}.{kill}".replace("..", ".")


def append_eod_record(log_dir: Path, summary: dict) -> None:
    path = Path(log_dir) / EOD_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(summary) + "\n")


def load_latest_eod(log_dir: Path) -> dict | None:
    records = _read_jsonl(Path(log_dir) / EOD_FILENAME)
    return records[-1] if records else None


def main() -> None:
    """CLI: summarize a day from the persisted journals, offline.
    Usage: python -m tradingbot.eod [YYYY-MM-DD]   (default: today UTC;
    pass yesterday's date after midnight UTC to re-print the finished day)."""
    from tradingbot.config import load_settings

    settings = load_settings()
    if len(sys.argv) > 1:
        day = date.fromisoformat(sys.argv[1])
    else:
        day = datetime.now(timezone.utc).date()

    # Equity/baselines are only knowable live; reuse the persisted risk
    # state when it covers the requested day.
    risk_state: dict = {}
    state_path = Path(settings.log_dir) / "risk_state.json"
    if state_path.exists():
        try:
            candidate = json.loads(state_path.read_text())
            if candidate.get("date") == day.isoformat():
                risk_state = candidate
        except (json.JSONDecodeError, OSError):
            pass

    summary = build_eod_summary(
        Path(settings.log_dir),
        day,
        day_start_equity=risk_state.get("starting_equity"),
        week_start_equity=risk_state.get("week_start_equity"),
        daily_kill_switch=bool(risk_state.get("kill_switch_active")),
        weekly_kill_switch=bool(risk_state.get("weekly_kill_active")),
    )
    print(format_eod_text(summary))


if __name__ == "__main__":
    main()
