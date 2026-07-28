"""Single daemon-owned queue serializing every delegate call.

A single locally-served model handles one generation efficiently at a time,
and a single Claude Code turn can issue multiple tool calls in parallel --
so calls must be strictly serialized from Phase 1 onward, not just once
Phase 3 adds more callers. The per-call timeout starts when execution
actually begins, not while queued: a call waiting behind another shouldn't
have its own clock already half-spent by the time it's dispatched.

`run()` awaits its callable's coroutine directly rather than bridging it
through `asyncio.to_thread` -- U11's `client.py` is built on
`httpx.AsyncClient` specifically to be genuinely async, and bouncing that
back through a thread-pool worker would throw away the reason it's async in
the first place, for no concurrency gain: this queue already fully
serializes every call via its single lock regardless of sync or async.

`run_chain()` (U12) holds that SAME single lock across an entire ordered
sequence of `(candidate_fn, timeout)` attempts, not one acquisition per
candidate. Releasing and reacquiring the lock between candidates would let
an unrelated `run()`/`run_chain()` caller interleave mid-chain, defeating
the reason the queue exists in the first place -- see plan Key Technical
Decisions. Each candidate's own timeout still applies independently within
that single hold; only the lock spans the whole chain, not the clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


class DelegateQueue:
    """Serializes calls to a coroutine-returning `fn` behind a single asyncio lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[[], Awaitable[T]], timeout: float) -> T:
        async with self._lock:
            return await asyncio.wait_for(fn(), timeout=timeout)

    async def run_chain(
        self,
        attempts: Sequence[tuple[Callable[[], Awaitable[T]], float]],
        *,
        advance_on: tuple[type[BaseException], ...] = (),
    ) -> T:
        """Try each `(candidate_fn, timeout)` pair in `attempts`, in order,
        under ONE lock acquisition spanning the whole sequence.

        An exception whose type is in `advance_on` is treated as "this
        candidate failed, try the next one" -- caught here and the loop
        continues without releasing the lock. Any other exception (or the
        last `advance_on` exception once every attempt is exhausted)
        propagates out of `run_chain()`, releasing the lock via the
        surrounding `async with` on the way out same as `run()`.

        Deciding WHICH exception types belong in `advance_on` is entirely
        the caller's business (`delegate/chain.py`'s R24-vs-R34 decision
        tree) -- this method is generic queueing infrastructure and knows
        nothing about the delegate exception taxonomy itself.
        """
        if not attempts:
            raise ValueError("run_chain() requires at least one (candidate_fn, timeout) attempt")

        async with self._lock:
            last_exc: BaseException | None = None
            for fn, timeout in attempts:
                try:
                    return await asyncio.wait_for(fn(), timeout=timeout)
                except advance_on as exc:
                    last_exc = exc
                    continue
            assert last_exc is not None
            raise last_exc
