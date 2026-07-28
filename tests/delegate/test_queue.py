"""Tests for the delegate queue's serialization and timeout semantics (U2, U11).

U11 changes `run()` to await a coroutine-returning callable directly instead
of bridging a sync callable through `asyncio.to_thread` -- these tests now
pass `async def` callables, matching the new contract.

U12 adds `run_chain()`: one lock acquisition spanning an ordered sequence of
`(candidate_fn, timeout)` attempts, not one acquisition per candidate. The
tests below use plain `RuntimeError`/`ValueError` as the `advance_on` type
rather than any delegate-specific exception -- `run_chain()` is generic
queueing infrastructure and doesn't know about `chain.py`'s exception
taxonomy at all (that decision belongs entirely to the caller).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from josu.delegate.queue import DelegateQueue


@pytest.mark.asyncio
async def test_concurrent_calls_are_serialized_not_overlapping():
    queue = DelegateQueue()
    intervals: list[tuple[float, float]] = []

    def make_task(duration: float):
        async def _run():
            start = time.monotonic()
            await asyncio.sleep(duration)
            intervals.append((start, time.monotonic()))
            return "done"

        return _run

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

    async def slow():
        await asyncio.sleep(0.2)
        return "done"

    async def fast():
        return "fast-done"

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

    async def slow():
        await asyncio.sleep(0.2)
        return "done"

    with pytest.raises(TimeoutError):
        await queue.run(slow, timeout=0.01)


@pytest.mark.asyncio
async def test_run_chain_advances_past_failing_attempt_to_a_succeeding_one():
    queue = DelegateQueue()

    async def first():
        raise RuntimeError("first candidate down")

    async def second():
        return "second-succeeded"

    result = await queue.run_chain(
        [(first, 5), (second, 5)], advance_on=(RuntimeError,)
    )
    assert result == "second-succeeded"


@pytest.mark.asyncio
async def test_run_chain_reraises_last_advance_on_exception_once_exhausted():
    queue = DelegateQueue()

    async def first():
        raise RuntimeError("first down")

    async def second():
        raise RuntimeError("second down too")

    with pytest.raises(RuntimeError, match="second down too"):
        await queue.run_chain(
            [(first, 5), (second, 5)], advance_on=(RuntimeError,)
        )


@pytest.mark.asyncio
async def test_run_chain_does_not_advance_on_an_exception_outside_advance_on():
    """An exception not in `advance_on` propagates immediately -- the
    remaining attempts are never tried. `run_chain()` itself is generic
    queueing infrastructure with no opinion on which exception types belong
    in `advance_on`; that decision (including that a malformed-response
    error, once `delegate()`'s own same-candidate retry is exhausted, DOES
    belong in `advance_on`) is entirely `chain.py`'s business."""
    queue = DelegateQueue()
    second_called = False

    async def first():
        raise ValueError("not an advance-worthy failure")

    async def second():
        nonlocal second_called
        second_called = True
        return "should never run"

    with pytest.raises(ValueError, match="not an advance-worthy failure"):
        await queue.run_chain(
            [(first, 5), (second, 5)], advance_on=(RuntimeError,)
        )
    assert second_called is False


@pytest.mark.asyncio
async def test_run_chain_empty_attempts_raises_value_error():
    queue = DelegateQueue()
    with pytest.raises(ValueError):
        await queue.run_chain([], advance_on=(RuntimeError,))


@pytest.mark.asyncio
async def test_run_chain_holds_one_lock_across_the_full_multi_candidate_sequence():
    """A second, unrelated call is blocked for the full duration of an
    in-progress chain -- not just a single candidate's own attempt -- proving
    `run_chain()` holds ONE lock acquisition across the whole ordered
    sequence rather than releasing and reacquiring between candidates."""
    queue = DelegateQueue()

    async def first_candidate():
        await asyncio.sleep(0.1)
        raise RuntimeError("first candidate down")

    async def second_candidate():
        await asyncio.sleep(0.1)
        return "second-done"

    async def unrelated_call():
        return "unrelated-done"

    chain_task = asyncio.create_task(
        queue.run_chain(
            [(first_candidate, 5), (second_candidate, 5)], advance_on=(RuntimeError,)
        )
    )
    # Give the chain a moment to acquire the lock and start its first
    # candidate, so `unrelated_call` below genuinely has to wait behind it.
    await asyncio.sleep(0.03)

    unrelated_start = time.monotonic()
    unrelated_result = await queue.run(unrelated_call, timeout=5)
    unrelated_wait = time.monotonic() - unrelated_start

    chain_result = await chain_task
    assert chain_result == "second-done"
    assert unrelated_result == "unrelated-done"
    # unrelated_call only got the lock after BOTH candidates finished
    # (~0.2s total, minus the 0.03s head start) -- not just after the first
    # candidate released it (~0.07s remaining).
    assert unrelated_wait >= 0.14


@pytest.mark.asyncio
async def test_run_chain_each_candidates_own_timeout_still_applies_independently():
    queue = DelegateQueue()

    async def slow_first():
        await asyncio.sleep(0.2)
        return "should have timed out"

    async def fast_second():
        return "second-done"

    # `slow_first`'s own timeout (0.01s) is exceeded, which raises
    # `TimeoutError` -- if that's in `advance_on`, the chain moves on to
    # `fast_second` despite `slow_first`'s generous sleep, proving each
    # attempt's timeout is enforced independently within the single lock hold.
    result = await queue.run_chain(
        [(slow_first, 0.01), (fast_second, 5)], advance_on=(TimeoutError,)
    )
    assert result == "second-done"
