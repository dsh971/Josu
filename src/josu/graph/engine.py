"""Swappable graph-engine interface (R10).

`gortex.py`'s `GortexEngine` is the v1 implementation of this interface.
Nothing outside this module should import gortex directly -- `server.py`
and any future consumer only see the shape defined here, so a future engine
swap only touches this file and its implementation, never the MCP surface.

Async (not sync): the sync shape this Protocol originally carried was fine
for graphify's in-process, in-memory call, but gortex is reached over real
network I/O with a multi-second timeout -- a synchronous call at this layer
would block the daemon's single asyncio event loop for the full timeout
duration on every graph query, freezing every worktree's session, the
delegate queue, and proactive checks at once (a materially larger blast
radius than any one task's own circuit-breaker budget). Mirrors U11's
`delegate/client.py` sync-to-async conversion, not a new idiom.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class GraphEngine(Protocol):
    """Minimal surface a context-graph implementation must provide."""

    async def build(self, root: Path) -> None:
        """(Re)build the graph for the given directory tree, in place."""
        ...

    async def update(self, root: Path, changed_files: list[Path]) -> None:
        """Incrementally re-index only the given changed files (R14)."""
        ...

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Fuzzy/substring search over node labels; returns matching nodes
        plus their immediate neighbors for context."""
        ...

    async def execute(self, operation: str, params: dict) -> dict:
        """Dispatch a specific graph operation by name. Valid operations
        are engine-defined; an unrecognized or rejected operation raises a
        `GraphEngineUnavailableError` (or a plain `ValueError` for an
        engine with a fixed, small operation set, e.g. the old
        graphify-era engine) -- either way, always a type every caller's
        existing `except (KeyError, ValueError, RuntimeError)` already
        catches, never a bare, uncaught exception."""
        ...


class GraphEngineUnavailableError(RuntimeError):
    """Common base for a graph engine being unreachable/erroring/timing out
    -- as opposed to a genuine "no data for this query" miss, which engines
    signal by simply returning an empty result, not by raising.

    A `RuntimeError` subclass so the existing exception contract
    (`graph/server.py`'s `call_tool` catches `(KeyError, ValueError,
    RuntimeError)`; `delegate/local_model.py`'s `_graph_context()` catches
    `RuntimeError` to trigger R13's file-exploration fallback) keeps working
    unmodified for any concrete engine that raises this or a subclass of it
    -- callers never need to know which engine is behind the Protocol.

    `reason` carries an internal, engine-defined classification (e.g. for
    `GortexEngine`: "unreachable", "timeout", "http-error", "warming") --
    the external fallback behavior is identical regardless of `reason`, but
    it's preserved on the exception (and folds into `str(exc)`, so it's
    visible wherever the exception message itself is logged) rather than
    discarded, for whenever a caller wants to branch or aggregate on it.
    Not currently wired into `observability/runlog.py`'s `GraphQueryEvent`
    -- that event type has no production caller at all yet, independent of
    this change.
    """

    def __init__(self, detail: str, *, reason: str = "unavailable") -> None:
        self.reason = reason
        super().__init__(detail)
