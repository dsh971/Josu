"""U14: the internal HTTP endpoint that routes CLI/hook-driven delegate
calls through the daemon's ONE shared `DelegateQueue`, instead of each
caller (`josu delegate`, the commit hook) constructing its own unshared
instance in a separate OS process.

A thin REST wrapper, not a second MCP surface -- MCP's SSE protocol is
built for Claude Code's tool-calling loop, not a one-shot CLI/hook call
(see plan U14 Approach). `cli.py`'s `_cmd_delegate` and
`proactive/watchers.py`'s commit-hook entry point are the two callers,
both `httpx.AsyncClient`s of this route mirroring `delegate/client.py`'s
pattern, applied to an internal call instead of an external one.

Two request shapes, validated by `DelegateInternalRequest` below (a
`pydantic.BaseModel`, mirroring `config/delegate.py`/`config/chains.py`'s
validation convention -- never a bare unchecked dict):

- `task_type` -- the general case (`josu delegate`): resolved against the
  daemon's real, already-loaded `chains_config`/`registry` via
  `delegate/chain.py`'s normal `execute_chain()` -> `resolve_chain()` path.
- `candidates` -- a pre-resolved, already-local-only candidate list (the
  commit hook's case). **This is the R39-preservation mechanism.**
  `proactive/watchers.py`'s `_local_only_execution_inputs()` still runs
  entirely client-side, exactly where it always has, BEFORE this endpoint
  is ever called -- it is what produces this candidate list. This handler
  never falls back to the daemon's real `chains_config`/`registry` for a
  `candidates` request: it builds a throwaway, single-chain `ChainsConfig`
  (`allow_remote=False`) from EXACTLY the candidates given, and additionally
  drops any non-`local` candidate defensively (belt-and-suspenders: even a
  future bug in a caller's own projection can't smuggle a remote candidate
  through this path). A bare `task_type="proactive_check"` request would
  resolve against the daemon's real config -- which may have
  `allow_remote=true` set globally -- reintroducing exactly the unbounded
  remote-cost risk R39 exists to prevent; that's why the commit hook uses
  the `candidates` shape, never `task_type`.

Trust boundary: this daemon already has two unauthenticated endpoints
(`/graph/sse`, `/delegate/sse`) whose only protection is loopback-only
binding -- an accepted residual risk noted in the plan's Tier 2 review.
The plan's stated preference is to scope this route to a Unix domain
socket instead, since it's narrower-purpose (CLI/hook-only, never an MCP
client) and a tighter boundary would cost little. That's deliberately NOT
done in this pass: `daemon.py`'s `run()` today is a single blocking
`uvicorn.run(app, host, port)` call with no async server lifecycle of its
own; serving this one route over a second, UDS-bound listener needs a
second concurrently-`await`ed `uvicorn.Server`, plus a client-discoverable
socket-path convention shared with `cli.py`/`watchers.py`, plus stale-
socket cleanup across daemon restarts -- a real, separable increment of
scope beyond closing THIS unit's actual gap (the queue wasn't shared at
all). This route is therefore mounted into the same `Starlette` app as
`/graph/sse`/`/delegate/sse` and inherits their existing loopback-TCP
posture, unchanged. Documented here as the plan's own named accepted
fallback, not a silent gap -- see plan Risks & Dependencies and U14's
Approach section.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate import chain
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS
from josu.delegate.queue import DelegateQueue
from josu.graph.engine import GraphEngine

DELEGATE_INTERNAL_PATH = "/delegate/internal"

# Size caps: a malformed or hostile oversized body must fail cleanly with a
# structured error, never an unhandled exception or unbounded memory read
# (U14 test scenario). Generous for a bounded sub-task's text/scope, well
# below anything that would strain a local process.
MAX_BODY_BYTES = 2_000_000
MAX_TASK_CHARS = 200_000
MAX_SCOPE_BYTES = 200_000

# An internal-only chain key used to wire a client-supplied, pre-resolved
# candidate list through `execute_chain()`'s `chains_config`/`registry`
# parameters. Never a real `task_type` a developer would configure in
# `josu.toml`'s `[[delegation.chains]]`, so it can never collide with one.
_PRE_RESOLVED_CHAIN_KEY = "__josu_internal_pre_resolved__"


class DelegateInternalRequest(BaseModel):
    """POST `/delegate/internal`'s validated body.

    Exactly one of `task_type` or a non-empty `candidates` list must be
    given -- see module docstring for what each shape means and why the
    distinction is load-bearing for R39.
    """

    task: str = Field(..., max_length=MAX_TASK_CHARS)
    scope: dict[str, Any] | None = None
    task_type: str | None = None
    candidates: list[DelegateCandidate] | None = None
    timeout: float | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "DelegateInternalRequest":
        has_task_type = self.task_type is not None
        has_candidates = bool(self.candidates)
        if has_task_type == has_candidates:
            raise ValueError(
                "exactly one of `task_type` (general case) or a non-empty "
                "`candidates` list (pre-resolved local-only case) is required"
            )
        if self.scope is not None and len(json.dumps(self.scope)) > MAX_SCOPE_BYTES:
            raise ValueError(f"scope exceeds the maximum size of {MAX_SCOPE_BYTES} bytes")
        return self


def _error_response(error: str, detail: Any, *, status_code: int) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status_code)


def build_delegate_internal_route(
    *,
    queue: DelegateQueue,
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
    graph_engine: GraphEngine | None = None,
    client_factory: chain.ClientFactory | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    path: str = DELEGATE_INTERNAL_PATH,
) -> Route:
    """Build the Starlette `Route` handling U14's internal POST endpoint.

    `queue`/`chains_config`/`registry` must be the SAME instances
    `daemon.py`'s `create_app()` passes into `build_delegate_server()` --
    the whole point of this unit is that the MCP tool and this route
    serialize through one shared `DelegateQueue`, not two independently
    constructed ones.
    """

    async def handle(request: Request) -> JSONResponse:
        # Check the declared `Content-Length` FIRST, before ever reading the
        # body -- an oversized request is rejected without buffering it into
        # memory at all. The post-read length check just below remains as a
        # fallback for a chunked request with no `Content-Length` header (a
        # declared size isn't guaranteed to be present or honest, so both
        # checks are needed; this one just avoids the common case where the
        # header IS present and already tells us enough to reject early).
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                declared_bytes = int(declared_length)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > MAX_BODY_BYTES:
                return _error_response(
                    "request_too_large",
                    f"request body exceeds the maximum of {MAX_BODY_BYTES} bytes",
                    status_code=413,
                )

        body_bytes = await request.body()
        if len(body_bytes) > MAX_BODY_BYTES:
            return _error_response(
                "request_too_large",
                f"request body exceeds the maximum of {MAX_BODY_BYTES} bytes",
                status_code=413,
            )

        try:
            raw = json.loads(body_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _error_response("invalid_json", str(exc), status_code=400)

        try:
            payload = DelegateInternalRequest.model_validate(raw)
        except ValidationError as exc:
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return _error_response("invalid_request", errors, status_code=400)

        if payload.candidates:
            # R39: build the resolution inputs from ONLY what the client
            # supplied -- the daemon's real `chains_config`/`registry`
            # (which may have `allow_remote=true` set globally) is never
            # consulted for this request shape. The `local` filter here is
            # a defensive second layer on top of the client's own
            # `_local_only_execution_inputs()` projection, not a
            # replacement for it.
            local_candidates = [c for c in payload.candidates if c.local]
            resolved_task_type = _PRE_RESOLVED_CHAIN_KEY
            resolved_registry: Mapping[str, DelegateCandidate] = {
                c.name: c for c in local_candidates
            }
            resolved_chains_config = ChainsConfig(
                chains=[
                    DelegationChain(
                        task_type=_PRE_RESOLVED_CHAIN_KEY,
                        candidates=[c.name for c in local_candidates],
                        explicit_order=True,
                    )
                ],
                allow_remote=False,
            )
        else:
            assert payload.task_type is not None
            resolved_task_type = payload.task_type
            resolved_registry = registry
            resolved_chains_config = chains_config

        # Parity with `delegate/server.py`'s MCP tool (`call_tool()`): derive
        # `scope_root` from `scope={"path": ...}` the same way, so a
        # `local_model.py` R13 file-context fallback triggers identically
        # whether a caller reaches `execute_chain()` through the MCP tool or
        # through this HTTP route. Previously missing here, silently losing
        # that fallback for every caller of this endpoint.
        scope_root = (
            Path(payload.scope["path"])
            if isinstance(payload.scope, dict) and "path" in payload.scope
            else None
        )

        try:
            outcome = await chain.execute_chain(
                resolved_task_type,
                payload.task,
                payload.scope,
                chains_config=resolved_chains_config,
                registry=resolved_registry,
                queue=queue,
                graph_engine=graph_engine,
                scope_root=scope_root,
                client_factory=client_factory,
                timeout=payload.timeout or timeout,
            )
        except chain.NoCandidatesError as exc:
            return _error_response("no_candidates", str(exc), status_code=422)
        except chain.ChainExhaustedError as exc:
            return _error_response("chain_exhausted", str(exc), status_code=502)

        return JSONResponse(
            {"result": outcome.result, "caveats": outcome.caveats, "model": outcome.model}
        )

    return Route(path, endpoint=handle, methods=["POST"])
