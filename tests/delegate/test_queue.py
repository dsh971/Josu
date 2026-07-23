"""Tests for the delegate queue's serialization and timeout semantics (U2)."""

from __future__ import annotations

import time

import pytest

from josu.delegate.queue import DelegateQueue


@pytest.mark.asyncio
async def test_concurrent_calls_are_serialized_not_overlapping():
    queue = DelegateQueue()
    intervals: list[tuple[float, float]] = []

    def make_task(duration: float):
        def _run():
            start = time.monotonic()
            time.sleep(duration)
            intervals.append((start, time.monotonic()))
            return "done"

        return _run

    import asyncio

    results = await asyncio.gather(
        queue.run(make_task(0.1), timeout=5),
        queue.run(make_task(0.1), timeout=5),
    )

    assert results == ["done", "done"]
    assert len(intervals) == 2
    (start_a, end_a), (start_b, end_b) = sorted(intervals)
    # The second call's execution window must not begin before the first ends.
    assert start_b >= end_a


@pytest.mark.asyncio
async def test_timeout_clock_starts_at_execution_not_arrival():
    queue = DelegateQueue()

    def slow():
        time.sleep(0.2)
        return "done"

    def fast():
        return "fast-done"

    import asyncio

    # `slow` occupies the lock for 0.2s. `fast`'s own timeout (0.05s) should
    # only start once `slow` releases the lock and `fast` begins executing --
    # since `fast` itself finishes instantly, it must NOT time out even though
    # it waited behind `slow` for longer than its own timeout budget.
    results = await asyncio.gather(
        queue.run(slow, timeout=5),
        queue.run(fast, timeout=0.05),
    )
    assert results == ["done", "fast-done"]


@pytest.mark.asyncio
async def test_run_raises_timeout_error_when_execution_exceeds_budget():
    queue = DelegateQueue()

    def slow():
        time.sleep(0.2)
        return "done"

    with pytest.raises(TimeoutError):
        await queue.run(slow, timeout=0.01)
