import json
from datetime import datetime, timedelta, timezone

from tradingbot.news.shadow_trade import ShadowTradeTracker


def make_tracker(tmp_path, max_hold_min: int = 240) -> ShadowTradeTracker:
    return ShadowTradeTracker(log_path=tmp_path / "shadow_trades.jsonl", max_hold_min=max_hold_min)


def test_long_trade_hits_target_is_a_win(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    tracker.update("AAPL", current_price=105.0)

    assert not tracker.has_open("AAPL")
    lines = tmp_path.joinpath("shadow_trades.jsonl").read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["status"] == "WIN"
    assert record["r_multiple"] == 2.5  # (105-100) / (100-98)


def test_long_trade_hits_stop_is_a_loss(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    tracker.update("AAPL", current_price=97.0)

    record = json.loads(tmp_path.joinpath("shadow_trades.jsonl").read_text().strip())
    assert record["status"] == "LOSS"
    assert record["r_multiple"] == -1.5  # (97-100) / (100-98)


def test_short_trade_hits_target_is_a_win(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "TSLA", "SHORT", entry_price=200.0, stop_price=204.0, target_price=192.0,
        headline="h", confidence=0.8, rationale="r",
    )
    tracker.update("TSLA", current_price=190.0)

    record = json.loads(tmp_path.joinpath("shadow_trades.jsonl").read_text().strip())
    assert record["status"] == "WIN"
    assert record["r_multiple"] == 2.5  # (200-190) / (204-200)


def test_trade_times_out_if_neither_level_hit(tmp_path):
    tracker = make_tracker(tmp_path, max_hold_min=60)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    tracker.update("AAPL", current_price=101.0, now=later)

    record = json.loads(tmp_path.joinpath("shadow_trades.jsonl").read_text().strip())
    assert record["status"] == "TIMEOUT"
    assert record["r_multiple"] == 0.5  # (101-100) / (100-98)


def test_no_close_while_price_between_stop_and_target(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    tracker.update("AAPL", current_price=101.0)

    assert tracker.has_open("AAPL")
    assert not tmp_path.joinpath("shadow_trades.jsonl").exists()


def test_has_open_blocks_until_closed(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    assert tracker.has_open("AAPL")
    tracker.update("AAPL", current_price=104.0)
    assert not tracker.has_open("AAPL")


def test_update_on_symbol_with_no_open_trade_is_a_noop(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.update("AAPL", current_price=100.0)  # should not raise
    assert not tracker.has_open("AAPL")
