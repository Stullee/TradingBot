import json
from pathlib import Path

from tradingbot.report import _print_news_analysis_stats, _print_shadow_trading_stats, _read_jsonl


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_read_jsonl_missing_file_returns_empty_list(tmp_path):
    assert _read_jsonl(tmp_path / "nope.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text('{"a": 1}\nnot json\n{"a": 2}\n')
    assert _read_jsonl(path) == [{"a": 1}, {"a": 2}]


def test_shadow_trading_stats_no_file(tmp_path, capsys):
    _print_shadow_trading_stats(tmp_path / "shadow_trades.jsonl")
    out = capsys.readouterr().out
    assert "No shadow trades closed yet" in out


def test_shadow_trading_stats_computes_win_rate_and_avg_r(tmp_path, capsys):
    path = tmp_path / "shadow_trades.jsonl"
    write_jsonl(
        path,
        [
            {"status": "WIN", "r_multiple": 2.0},
            {"status": "LOSS", "r_multiple": -1.0},
            {"status": "TIMEOUT", "r_multiple": 0.3},
        ],
    )
    _print_shadow_trading_stats(path)
    out = capsys.readouterr().out
    assert "Closed: 3" in out
    assert "Win: 1" in out
    assert "Loss: 1" in out
    assert "Timeout: 1" in out
    assert "Win rate: 33%" in out


def test_news_analysis_stats_separates_skipped_from_assessed(tmp_path, capsys):
    path = tmp_path / "news_analysis.jsonl"
    write_jsonl(
        path,
        [
            {"direction": "LONG", "confidence": 0.8, "skipped_reason": None},
            {"direction": "NONE", "confidence": 0.1, "skipped_reason": None},
            {"direction": None, "confidence": None, "skipped_reason": "shadow trade already open"},
        ],
    )
    _print_news_analysis_stats(path)
    out = capsys.readouterr().out
    assert "Batches assessed: 2" in out
    assert "Skipped (shadow trade already open): 1" in out
    assert "'LONG': 1" in out
    assert "'NONE': 1" in out
