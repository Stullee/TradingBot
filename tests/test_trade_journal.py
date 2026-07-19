import json
from datetime import datetime, timezone
from types import SimpleNamespace

from ib_async import CommissionReport, Contract, Execution, Fill

from tradingbot.execution.trade_journal import TradeJournal, summarize_live_trades


def make_fill(symbol: str, side: str, shares: float, price: float, exec_id: str) -> Fill:
    contract = Contract(symbol=symbol, currency="USD")
    execution = Execution(execId=exec_id, side=side, shares=shares, price=price)
    return Fill(
        contract=contract,
        execution=execution,
        commissionReport=CommissionReport(),
        time=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
    )


def deliver(journal: TradeJournal, fill: Fill, commission: float, realized=None) -> None:
    """Simulates ib_async's event sequence: execDetails then commissionReport."""
    trade = SimpleNamespace()
    journal._on_exec_details(trade, fill)
    report = CommissionReport(
        execId=fill.execution.execId, commission=commission, realizedPNL=realized or 0.0
    )
    journal._on_commission_report(trade, fill, report)


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_round_trip_with_context_reports_net_r(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    journal.record_entry_context(
        "AAPL", direction="LONG", entry_ref_price=50.0, stop_price=49.5,
        target_price=51.0, strategy="vwap_mean_reversion",
    )
    deliver(journal, make_fill("AAPL", "BOT", 100, 50.0, "e1"), commission=1.0)
    deliver(journal, make_fill("AAPL", "SLD", 100, 51.0, "e2"), commission=1.0, realized=98.0)

    records = read_records(tmp_path / "trades.jsonl")
    assert [r["type"] for r in records] == ["fill", "fill", "round_trip"]
    rt = records[-1]
    assert rt["direction"] == "LONG"
    assert rt["qty"] == 100
    assert rt["gross_pnl"] == 100.0
    assert rt["commissions"] == 2.0
    assert rt["net_pnl"] == 98.0
    # net R against the intended half-point risk: (98/100) / 0.5
    assert rt["r_multiple"] == 1.96
    assert rt["strategy"] == "vwap_mean_reversion"


def test_partial_fills_average_into_one_round_trip(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    deliver(journal, make_fill("MSFT", "BOT", 60, 50.0, "e1"), commission=0.5)
    deliver(journal, make_fill("MSFT", "BOT", 40, 50.5, "e2"), commission=0.5)
    deliver(journal, make_fill("MSFT", "SLD", 100, 49.0, "e3"), commission=1.0)

    rt = read_records(tmp_path / "trades.jsonl")[-1]
    assert rt["type"] == "round_trip"
    assert rt["avg_entry"] == 50.2
    assert rt["avg_exit"] == 49.0
    assert rt["gross_pnl"] == -120.0
    assert rt["net_pnl"] == -122.0
    assert rt["r_multiple"] is None  # no entry context recorded


def test_short_round_trip_signs_pnl_correctly(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    deliver(journal, make_fill("TSLA", "SLD", 10, 200.0, "e1"), commission=0.5)
    deliver(journal, make_fill("TSLA", "BOT", 10, 195.0, "e2"), commission=0.5)
    rt = read_records(tmp_path / "trades.jsonl")[-1]
    assert rt["direction"] == "SHORT"
    assert rt["gross_pnl"] == 50.0
    assert rt["net_pnl"] == 49.0


def test_duplicate_executions_are_ignored(tmp_path):
    """IB replays the day's executions on reconnect -- each execId must be
    journaled exactly once, including across a restart."""
    journal = TradeJournal(tmp_path / "trades.jsonl")
    fill = make_fill("AAPL", "BOT", 100, 50.0, "dup-1")
    deliver(journal, fill, commission=1.0)
    deliver(journal, fill, commission=1.0)  # replay within the same run
    assert len(read_records(tmp_path / "trades.jsonl")) == 1

    journal2 = TradeJournal(tmp_path / "trades.jsonl")  # restart
    deliver(journal2, fill, commission=1.0)  # replay after restart
    assert len(read_records(tmp_path / "trades.jsonl")) == 1


def test_seeded_position_pairs_with_its_closing_fill(tmp_path):
    """A restart mid-position: the closing fill must pair against the seeded
    entry (IB avgCost), not look like an orphan exit."""
    journal = TradeJournal(tmp_path / "trades.jsonl")
    journal.seed_positions([("NVDA", 50, 100.0, "USD")])
    deliver(journal, make_fill("NVDA", "SLD", 50, 102.0, "e1"), commission=1.0)
    rt = read_records(tmp_path / "trades.jsonl")[-1]
    assert rt["type"] == "round_trip"
    assert rt["seeded_entry"] is True
    assert rt["avg_entry"] == 100.0
    assert rt["net_pnl"] == 99.0


def test_entry_context_survives_restart(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    journal.record_entry_context(
        "AMD", direction="LONG", entry_ref_price=100.0, stop_price=98.0,
        target_price=105.0, strategy="trendline_breakout",
    )
    journal2 = TradeJournal(tmp_path / "trades.jsonl")  # restart
    assert journal2.stop_price_for("AMD") == 98.0
    journal2.seed_positions([("AMD", 10, 100.0, "USD")])
    deliver(journal2, make_fill("AMD", "SLD", 10, 104.0, "e9"), commission=0.4)
    rt = read_records(tmp_path / "trades.jsonl")[-1]
    # (net 39.6 / 10) / 2.0 risk per share
    assert rt["r_multiple"] == 1.98


def test_summarize_live_trades(tmp_path):
    journal = TradeJournal(tmp_path / "trades.jsonl")
    journal.record_entry_context("AAPL", "LONG", 50.0, 49.0, 52.0, "vwap_mean_reversion")
    deliver(journal, make_fill("AAPL", "BOT", 100, 50.0, "e1"), commission=1.0)
    deliver(journal, make_fill("AAPL", "SLD", 100, 52.0, "e2"), commission=1.0)
    journal.record_entry_context("MSFT", "LONG", 400.0, 396.0, 410.0, "vwap_mean_reversion")
    deliver(journal, make_fill("MSFT", "BOT", 10, 400.0, "e3"), commission=1.0)
    deliver(journal, make_fill("MSFT", "SLD", 10, 396.0, "e4"), commission=1.0)

    stats, recent = summarize_live_trades(tmp_path / "trades.jsonl")
    assert stats.closed == 2
    assert stats.wins == 1
    assert stats.losses == 1
    assert stats.win_rate == 0.5
    assert stats.net_pnl_by_currency["USD"] == 198.0 - 42.0
    assert stats.r_by_symbol["AAPL"] > 0 > stats.r_by_symbol["MSFT"]
    assert len(recent) == 2
