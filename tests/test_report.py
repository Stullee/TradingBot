from pathlib import Path

from tradingbot.report import _print_news_analysis, _print_shadow_trading
from tradingbot.status import NewsAnalysisStatus, ShadowTradingStatus


def test_print_shadow_trading_empty(capsys):
    _print_shadow_trading(ShadowTradingStatus(0, 0, 0, 0, 0.0, 0.0), Path("shadow_trades.jsonl"))
    out = capsys.readouterr().out
    assert "No shadow trades closed yet" in out


def test_print_shadow_trading_with_data(capsys):
    status = ShadowTradingStatus(closed=3, wins=1, losses=1, timeouts=1, win_rate=1 / 3, avg_r=0.4)
    _print_shadow_trading(status, Path("shadow_trades.jsonl"))
    out = capsys.readouterr().out
    assert "Closed: 3" in out
    assert "Win: 1" in out
    assert "Win rate: 33%" in out
    assert "Avg R: +0.40" in out


def test_print_news_analysis_empty(capsys):
    _print_news_analysis(NewsAnalysisStatus(0, 0, 0.0, {}), Path("news_analysis.jsonl"))
    out = capsys.readouterr().out
    assert "No articles assessed yet" in out


def test_print_news_analysis_with_data(capsys):
    status = NewsAnalysisStatus(
        assessed=2, skipped=1, avg_confidence=0.45, direction_counts={"LONG": 1, "NONE": 1}
    )
    _print_news_analysis(status, Path("news_analysis.jsonl"))
    out = capsys.readouterr().out
    assert "Batches assessed: 2" in out
    assert "Skipped (shadow trade already open): 1" in out
    assert "'LONG': 1" in out
