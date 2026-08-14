"""Client for the (now-retired) internal reindex route.

The server-side route (`build_graph_internal_route()`, previously wired
into `daemon.py`) was removed as part of the gortex integration rework: its
only production callers were the commit-hook and merge-triggered reindex
calls (both retired, R4 -- a configured gortex's own watcher is the sole
reindex trigger now) and the route's handler called `engine.update()`,
which is a no-op on every current `GraphEngine` implementation (`GortexEngine`,
`GraphifyEngine`) -- it would have accepted POSTs and silently done nothing.

`post_graph_internal_reindex()` itself is kept: `graph/index.py`'s
`reindex_on_save()` still calls it. `reindex_on_save()` has zero production
callers of its own (a separate, pre-existing gap this change doesn't touch),
so this client function currently has no live caller either -- if a future
save-trigger source is ever wired up, re-adding the server-side route (or
reconsidering this design) is a prerequisite, not assumed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from josu.delegate.daemon_client import post_json_to_internal_route

GRAPH_INTERNAL_REINDEX_PATH = "/graph/internal/reindex"


class GraphInternalError(RuntimeError):
    """Raised by `post_graph_internal_reindex()` when
    `GRAPH_INTERNAL_REINDEX_PATH` responds with a non-2xx, structured
    `{"error": ..., "detail": ...}` body. Colocated with the route it
    reports on (not `delegate/daemon_client.py`, despite mirroring
    `DelegateInternalError`'s shape there) -- this is a graph-domain
    concept, and `graph/index.py`'s only reason to import from the
    `delegate` package at all was this class living in the wrong place."""

    def __init__(self, error: str, detail: Any, *, status_code: int) -> None:
        self.error = error
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"{error}: {detail}")


async def post_graph_internal_reindex(
    base_url: str,
    root: Path,
    changed_files: list[Path],
    *,
    timeout: float = 120.0,
    token: str | None = None,
) -> None:
    """POST `{root, changed_files}` to `<base_url>` + `GRAPH_INTERNAL_REINDEX_PATH`
    -- the shared implementation behind `graph/index.py`'s `reindex_on_save()`,
    routing the actual graph mutation against the daemon's own live engine
    instead of a per-process throwaway one. Thin wrapper around
    `delegate/daemon_client.py`'s `post_json_to_internal_route()` (the
    domain-neutral POST-and-decode mechanics; `DaemonNotReachableError` and
    the auth-header handling live there too, genuinely shared across every
    internal route's client, not delegate-specific).

    Raises `DaemonNotReachableError` on a transport-level failure, or
    `GraphInternalError` on a non-2xx response. See module docstring: the
    server-side route this POSTs to no longer exists, so a real call today
    would hit whichever of those two the daemon's 404 for an unknown route
    maps to -- kept for `reindex_on_save()`'s sake, not currently exercised
    in production.
    """
    payload = {"root": str(root), "changed_files": [str(f) for f in changed_files]}
    await post_json_to_internal_route(
        base_url,
        GRAPH_INTERNAL_REINDEX_PATH,
        payload,
        error_cls=GraphInternalError,
        timeout=timeout,
        token=token,
    )
