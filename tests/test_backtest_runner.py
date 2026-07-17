import asyncio
import time

from tradingbot.backtest.runner import _PacingGate


def run(coro):
    return asyncio.run(coro)


def test_first_turn_does_not_wait():
    gate = _PacingGate(min_gap_sec=0.05)
    start = time.monotonic()
    run(gate.wait_turn())
    assert time.monotonic() - start < 0.05


def test_second_turn_waits_out_the_remaining_gap():
    """The bug this guards against: bounded concurrency alone doesn't bound
    the *rate* new IB historical-data requests get dispatched -- if
    individual fetches resolve faster than IB's pacing budget allows,
    concurrency blows straight through it. The gate must force a real wait
    between successive dispatches regardless of how fast callers ask for a
    turn."""
    gate = _PacingGate(min_gap_sec=0.05)
    start = time.monotonic()
    run(gate.wait_turn())
    run(gate.wait_turn())
    assert time.monotonic() - start >= 0.05


def test_turns_taken_concurrently_are_still_spaced_out():
    gate = _PacingGate(min_gap_sec=0.05)
    start = time.monotonic()

    async def scenario():
        await asyncio.gather(*(gate.wait_turn() for _ in range(4)))

    run(scenario())
    # 4 turns, each at least 0.05s after the previous -> at least 3 gaps.
    assert time.monotonic() - start >= 0.05 * 3


def test_no_extra_wait_once_the_gap_has_already_elapsed():
    gate = _PacingGate(min_gap_sec=0.05)
    run(gate.wait_turn())
    run(asyncio.sleep(0.06))

    start = time.monotonic()
    run(gate.wait_turn())
    assert time.monotonic() - start < 0.05
