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


def test_open_trade_tracks_last_price_and_unrealized_r(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    tracker.update("AAPL", current_price=101.0)  # still between stop and target

    trade = tracker.open_trades["AAPL"]
    assert trade.last_price == 101.0
    assert trade.unrealized_r_multiple() == 0.5  # (101-100) / (100-98)


def test_unrealized_r_multiple_for_a_short():
    from tradingbot.news.shadow_trade import ShadowTrade

    trade = ShadowTrade(
        symbol="TSLA", direction="SHORT", entry_price=200.0, stop_price=204.0,
        target_price=192.0, opened_at="t", headline="h", confidence=0.8, rationale="r",
        last_price=196.0,
    )
    assert trade.unrealized_r_multiple() == 1.0  # (200-196) / (204-200)


def test_unrealized_r_multiple_is_none_before_any_price_update():
    from tradingbot.news.shadow_trade import ShadowTrade

    trade = ShadowTrade(
        symbol="AAPL", direction="LONG", entry_price=100.0, stop_price=98.0,
        target_price=104.0, opened_at="t", headline="h", confidence=0.8, rationale="r",
    )
    assert trade.unrealized_r_multiple() is None


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


def test_open_trades_survive_a_restart(tmp_path):
    # Simulates a restart: a fresh ShadowTradeTracker pointed at the same
    # log_path must pick up whatever was still open when the previous one
    # stopped, instead of silently forgetting it.
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )

    restarted = make_tracker(tmp_path)
    assert restarted.has_open("AAPL")
    trade = restarted.open_trades["AAPL"]
    assert trade.entry_price == 100.0
    assert trade.stop_price == 98.0
    assert trade.target_price == 104.0


def test_closing_a_trade_removes_it_from_the_persisted_state(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.open(
        "AAPL", "LONG", entry_price=100.0, stop_price=98.0, target_price=104.0,
        headline="h", confidence=0.8, rationale="r",
    )
    tracker.update("AAPL", current_price=105.0)  # hits target -> closes

    restarted = make_tracker(tmp_path)
    assert not restarted.has_open("AAPL")


def test_missing_open_state_file_starts_with_no_open_trades(tmp_path):
    tracker = make_tracker(tmp_path)
    assert tracker.open_trades == {}


def test_corrupt_open_state_file_is_ignored_not_raised(tmp_path):
    tmp_path.joinpath("open_shadow_trades.json").write_text("not json")
    tracker = make_tracker(tmp_path)
    assert tracker.open_trades == {}
