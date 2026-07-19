import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from tradingbot.eod import (
    append_eod_record,
    build_eod_summary,
    format_eod_alert,
    format_eod_text,
    load_latest_eod,
)

DAY = date(2026, 7, 20)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def seed_logs(log_dir: Path) -> None:
    write_jsonl(
        log_dir / "trades.jsonl",
        [
            {"type": "fill", "symbol": "NVDA", "time": "2026-07-20T14:00:00+00:00"},
            {"type": "fill", "symbol": "NVDA", "time": "2026-07-20T15:00:00+00:00"},
            {
                "type": "round_trip", "symbol": "NVDA", "currency": "USD",
                "direction": "LONG", "qty": 10, "avg_entry": 100.2, "avg_exit": 103.0,
                "net_pnl": 26.0, "commissions": 2.0, "r_multiple": 0.87,
                "intended_entry": 100.0, "closed_at": "2026-07-20T15:00:00+00:00",
            },
            {
                "type": "round_trip", "symbol": "DBK", "currency": "EUR",
                "direction": "SHORT", "qty": 50, "avg_entry": 19.9, "avg_exit": 20.3,
                "net_pnl": -21.0, "commissions": 1.0, "r_multiple": -1.05,
                "intended_entry": 20.0, "closed_at": "2026-07-20T10:30:00+00:00",
            },
            {  # previous day -- must be excluded
                "type": "round_trip", "symbol": "ETH", "currency": "USD",
                "direction": "LONG", "qty": 0.05, "avg_entry": 3000, "avg_exit": 3100,
                "net_pnl": 5.0, "commissions": 0.5, "r_multiple": 1.0,
                "intended_entry": 3000, "closed_at": "2026-07-19T20:00:00+00:00",
            },
        ],
    )
    write_jsonl(
        log_dir / "shadow_trades.jsonl",
        [
            {"status": "WIN", "closed_at": "2026-07-20T16:00:00+00:00"},
            {"status": "LOSS", "closed_at": "2026-07-20T17:00:00+00:00"},
            {"status": "WIN", "closed_at": "2026-07-19T16:00:00+00:00"},  # excluded
        ],
    )
    write_jsonl(
        log_dir / "news_analysis.jsonl",
        [
            {"symbol": "NVDA", "assessed_at": "2026-07-20T14:05:00+00:00"},
            {"symbol": "AAPL", "assessed_at": "2026-07-19T14:05:00+00:00"},  # excluded
        ],
    )


def test_build_summary_filters_to_the_requested_day(tmp_path):
    seed_logs(tmp_path)
    s = build_eod_summary(
        tmp_path, DAY,
        equity=10_412.0, base_currency="EUR",
        day_start_equity=10_444.0, week_start_equity=10_500.0,
    )
    assert s["trades_closed"] == 2  # the 2026-07-19 ETH trade is excluded
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == 0.5
    assert s["fills"] == 2
    assert s["net_pnl_by_currency"] == {"USD": 26.0, "EUR": -21.0}
    assert s["commissions_by_currency"] == {"USD": 2.0, "EUR": 1.0}
    assert s["r_by_symbol"] == {"NVDA": 0.87, "DBK": -1.05}
    assert s["avg_r"] == round((0.87 - 1.05) / 2, 3)
    # NVDA LONG paid 100.2 vs 100 intended (+20 bps adverse); DBK SHORT
    # sold 19.9 vs 20.0 intended (+50 bps adverse) -> avg +35 bps
    assert s["avg_entry_slippage_bps"] == 35.0
    assert s["day_change_pct"] == round((10_412 - 10_444) / 10_444 * 100, 3)
    assert s["shadow_closed"] == 2 and s["shadow_wins"] == 1
    assert s["news_batches_assessed"] == 1


def test_text_and_alert_renderings_contain_the_key_figures(tmp_path):
    seed_logs(tmp_path)
    s = build_eod_summary(
        tmp_path, DAY, equity=10_412.0, base_currency="EUR",
        day_start_equity=10_444.0, daily_kill_switch=True,
    )
    text = format_eod_text(s)
    assert "2026-07-20" in text
    assert "1W/1L" in text
    assert "+26.00 USD" in text and "-21.00 EUR" in text
    assert "NVDA +0.9R" in text
    assert "KILL SWITCH ACTIVE: daily" in text
    alert = format_eod_alert(s)
    assert "2026-07-20" in alert and "2 trades" in alert and "KILL SWITCH" in alert


def test_empty_day_renders_cleanly(tmp_path):
    s = build_eod_summary(tmp_path, DAY)
    assert s["trades_closed"] == 0 and s["avg_r"] is None
    assert "none closed today" in format_eod_text(s)
    assert "no trades" in format_eod_alert(s)


def test_append_and_load_latest_roundtrip(tmp_path):
    first = build_eod_summary(tmp_path, date(2026, 7, 19))
    second = build_eod_summary(tmp_path, DAY)
    append_eod_record(tmp_path, first)
    append_eod_record(tmp_path, second)
    latest = load_latest_eod(tmp_path)
    assert latest["day"] == "2026-07-20"


def test_engine_emits_summary_on_rollover(tmp_path):
    from tradingbot.config import Settings
    from tradingbot.engine import TradingEngine

    seed_logs(tmp_path)
    settings = Settings(
        ib_port=7497, symbols="AAPL", log_dir=str(tmp_path), max_weekly_loss_pct=0
    )
    engine = TradingEngine(settings)
    sent = []
    engine.alerts = SimpleNamespace(send_soon=lambda key, title, msg: sent.append((key, msg)))

    engine._emit_eod_summary(
        DAY, equity=10_412.0, day_start_equity=10_444.0,
        week_start_equity=10_500.0, daily_kill=False, weekly_kill=False,
    )

    latest = load_latest_eod(tmp_path)
    assert latest is not None and latest["day"] == "2026-07-20"
    assert latest["trades_closed"] == 2
    assert sent and sent[0][0] == "eod-2026-07-20"
    assert "2 trades" in sent[0][1]
