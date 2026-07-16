import json
from pathlib import Path

from tradingbot.status import (
    gather_latest_news_by_symbol,
    gather_news_analysis_status,
    gather_shadow_trading_status,
    read_jsonl,
)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_read_jsonl_missing_file_returns_empty_list(tmp_path):
    assert read_jsonl(tmp_path / "nope.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text('{"a": 1}\nnot json\n{"a": 2}\n')
    assert read_jsonl(path) == [{"a": 1}, {"a": 2}]


def test_shadow_trading_status_empty(tmp_path):
    status = gather_shadow_trading_status(tmp_path / "shadow_trades.jsonl")
    assert status.closed == 0
    assert status.win_rate == 0.0
    assert status.avg_r == 0.0


def test_shadow_trading_status_computes_win_rate_and_avg_r(tmp_path):
    path = tmp_path / "shadow_trades.jsonl"
    write_jsonl(
        path,
        [
            {"status": "WIN", "r_multiple": 2.0},
            {"status": "LOSS", "r_multiple": -1.0},
            {"status": "TIMEOUT", "r_multiple": 0.3},
        ],
    )
    status = gather_shadow_trading_status(path)
    assert status.closed == 3
    assert status.wins == 1
    assert status.losses == 1
    assert status.timeouts == 1
    assert status.win_rate == 1 / 3
    assert status.avg_r == (2.0 - 1.0 + 0.3) / 3


def test_news_analysis_status_separates_skipped_from_assessed(tmp_path):
    path = tmp_path / "news_analysis.jsonl"
    write_jsonl(
        path,
        [
            {"direction": "LONG", "confidence": 0.8, "skipped_reason": None},
            {"direction": "NONE", "confidence": 0.1, "skipped_reason": None},
            {"direction": None, "confidence": None, "skipped_reason": "shadow trade already open"},
        ],
    )
    status = gather_news_analysis_status(path)
    assert status.assessed == 2
    assert status.skipped == 1
    assert status.direction_counts == {"LONG": 1, "NONE": 1}
    assert status.avg_confidence == (0.8 + 0.1) / 2


def test_latest_news_by_symbol_keeps_only_the_most_recent_per_symbol(tmp_path):
    path = tmp_path / "news_analysis.jsonl"
    write_jsonl(
        path,
        [
            {"symbol": "AAPL", "direction": "NONE", "confidence": 0.1, "skipped_reason": None},
            {"symbol": "AAPL", "direction": "LONG", "confidence": 0.8, "skipped_reason": None},
            {"symbol": "MSFT", "direction": "SHORT", "confidence": 0.7, "skipped_reason": None},
        ],
    )
    latest = gather_latest_news_by_symbol(path)
    assert latest["AAPL"]["direction"] == "LONG"  # the later of the two AAPL records
    assert latest["MSFT"]["direction"] == "SHORT"


def test_latest_news_by_symbol_ignores_skipped_records(tmp_path):
    path = tmp_path / "news_analysis.jsonl"
    write_jsonl(
        path,
        [
            {"symbol": "AAPL", "direction": "LONG", "confidence": 0.8, "skipped_reason": None},
            {"symbol": "AAPL", "direction": None, "confidence": None, "skipped_reason": "shadow trade already open"},
        ],
    )
    latest = gather_latest_news_by_symbol(path)
    assert latest["AAPL"]["direction"] == "LONG"  # the skip doesn't overwrite the real read
