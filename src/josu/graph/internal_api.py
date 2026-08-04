"""Internal HTTP route so reindex-triggering processes (the `josu run` CLI,
the commit-hook subprocess) can reindex against the DAEMON's own live graph
engine instance, instead of each constructing a throwaway engine of its own.

Bug this closes (found during doc review of the graphify-to-gortex plan
revision): `orchestrator/run.py`'s post-merge reindex, and
`proactive/watchers.py`'s commit/save triggers, previously called
`graph/index.py`'s reindex functions against an engine object that process
itself constructed -- never the daemon's. The daemon's own served graph
(what every MCP query actually reads) never picked up those writes, so
`josu run`'s "reindexed N files" report was reindexing a graph nothing
ever queries again. Mirrors `delegate/internal_api.py`'s U14 fix for the
exact same class of bug on the delegate-queue side.

A thin REST wrapper (not a second MCP surface), matching
`delegate/internal_api.py`'s own reasoning: reindex-triggering is a one-shot
call from a CLI or a git hook subprocess, not Claude Code's own tool-calling
loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from josu.delegate.daemon_client import post_json_to_internal_route
from josu.graph.engine import GraphEngine, GraphEngineUnavailableError
from josu.internal_route_http import error_response, read_bounded_json_body

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
    -- the shared implementation behind `graph/index.py`'s three reindex
    triggers, routing the actual graph mutation against the daemon's own
    live engine instead of a per-process throwaway one. Thin wrapper around
    `delegate/daemon_client.py`'s `post_json_to_internal_route()` (the
    domain-neutral POST-and-decode mechanics; `DaemonNotReachableError` and
    the auth-header handling live there too, genuinely shared across every
    internal route's client, not delegate-specific).

    Raises `DaemonNotReachableError` on a transport-level failure, or
    `GraphInternalError` on a non-2xx response.
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

# Same order-of-magnitude bound as `delegate/internal_api.py`'s
# `MAX_BODY_BYTES` -- a changed-file-path list is small; this is a
# defense-in-depth cap against a malformed/hostile oversized body, not a
# realistic legitimate-request size.
MAX_BODY_BYTES = 2_000_000
MAX_CHANGED_FILES = 10_000


class ReindexInternalRequest(BaseModel):
    """POST `/graph/internal/reindex`'s validated body: the directory root
    to reindex, and the exact, already-known set of changed file paths
    (from a commit, a save event, or a completed merge) to re-extract."""

    root: str = Field(..., min_length=1)
    changed_files: list[str] = Field(default_factory=list, max_length=MAX_CHANGED_FILES)


def build_graph_internal_route(
    *, engine: GraphEngine, path: str = GRAPH_INTERNAL_REINDEX_PATH
) -> Route:
    """Build the Starlette `Route` handling the internal reindex endpoint.

    `engine` must be the SAME instance `daemon.py`'s `create_app()`
    constructs and passes to `graph/server.py`'s MCP tool -- the whole
    point of this route is that a reindex write and an MCP query both hit
    the one live graph the daemon actually serves.
    """

    async def handle(request: Request) -> JSONResponse:
        raw = await read_bounded_json_body(request, max_bytes=MAX_BODY_BYTES)
        if isinstance(raw, JSONResponse):
            return raw

        try:
            payload = ReindexInternalRequest.model_validate(raw)
        except ValidationError as exc:
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return error_response("invalid_request", errors, status_code=400)

        try:
            await engine.update(
                Path(payload.root), [Path(f) for f in payload.changed_files]
            )
        except GraphEngineUnavailableError as exc:
            # Distinguishable from a validation error so callers (and U6's
            # run log, via the reindex-trigger client below) can tell "the
            # request was fine but the graph engine itself is down/erroring"
            # apart from "the caller sent something malformed" -- a silently
            # swallowed update() failure would leave the graph arbitrarily
            # stale with no visible signal (see plan Key Technical
            # Decisions).
            return error_response("engine_unavailable", str(exc), status_code=502)

        return JSONResponse({"status": "ok"})

    return Route(path, endpoint=handle, methods=["POST"])
