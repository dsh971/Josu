"""Single daemon-owned queue serializing every delegate call.

A single Ollama instance serves one generation efficiently at a time, and a
single Claude Code turn can issue multiple tool calls in parallel -- so calls
must be strictly serialized from Phase 1 onward, not just once Phase 3 adds
more callers. The per-call timeout starts when execution actually begins, not
while queued: a call waiting behind another shouldn't have its own clock
already half-spent by the time it's dispatched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class DelegateQueue:
    """Serializes calls to a blocking `fn` behind a single asyncio lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[[], T], timeout: float) -> T:
        async with self._lock:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
