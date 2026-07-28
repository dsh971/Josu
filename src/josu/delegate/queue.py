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
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class DelegateQueue:
    """Serializes calls to a coroutine-returning `fn` behind a single asyncio lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[[], Awaitable[T]], timeout: float) -> T:
        async with self._lock:
            return await asyncio.wait_for(fn(), timeout=timeout)
