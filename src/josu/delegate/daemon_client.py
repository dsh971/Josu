"""Shared daemon-client helper (U13/U14 simplify pass).

`cli.py`'s `_cmd_delegate`/`_cmd_run`, `orchestrator/run.py`'s `run_task()`,
and `proactive/watchers.py`'s commit-hook entry point each independently
hand-rolled an `httpx` client + `except httpx.TransportError` block, with
slightly different "daemon not reachable" message text depending on which
file you happened to be reading. This module is the one place the
domain-neutral pieces of that logic live now:

- `DaemonNotReachableError` -- one canonical exception/message, raised on a
  transport-level failure talking to the daemon. Shared by every internal
  daemon route's client, not delegate-specific despite this module's
  location (graph/internal_api.py's `post_graph_internal_reindex()` uses it
  too) -- kept here rather than split out, since every existing caller
  already imports it from this path.
- `check_daemon_reachable()` -- a bare HTTP GET against the daemon's base
  URL used as a pure reachability probe (any response, even a 404, proves a
  listener is there). Sync, since every current caller
  (`orchestrator/run.py`'s `run_task()`, `cli.py`'s `_cmd_run`) calls it
  from synchronous code, before any `asyncio.run()` boundary.
- `_auth_headers()` / `post_json_to_internal_route()` -- the shared
  POST-JSON-and-decode-with-fallback mechanics every internal route's
  client needs (declared-error-shape decode, non-JSON-body fallback,
  transport-failure translation). `post_delegate_internal()` below is the
  delegate route's thin wrapper around it; `graph/internal_api.py`'s
  `post_graph_internal_reindex()` is the graph route's -- kept OUT of this
  module (unlike an earlier version of this file) since `GraphInternalError`
  is a graph-domain concept, not a delegate one, and belongs colocated with
  the route it serves.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from josu.delegate.internal_api import DELEGATE_INTERNAL_PATH

# How much of a non-JSON response body to include in an internal-route
# error's fallback message -- generous enough to show a caller what
# actually came back (e.g. the start of an HTML error page or a bare
# traceback string from Starlette's default error handler), bounded so a
# huge/binary body never blows up the error message itself.
MAX_RAW_BODY_CHARS_IN_ERROR = 500


class DaemonNotReachableError(RuntimeError):
    """Raised when the josu daemon isn't reachable at `host:port` -- the one
    canonical type/message every caller of this module raises on a
    transport-level failure talking to the daemon (a connection refusal or a
    timeout, `httpx.TransportError`)."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(
            f"josu daemon not reachable at {host}:{port} -- start it with `josu daemon start`"
        )

    @classmethod
    def from_base_url(cls, base_url: str) -> "DaemonNotReachableError":
        """Build the error from a `http://host:port` base URL instead of a
        separate `host`/`port` pair -- `post_delegate_internal()`'s callers
        only have a `base_url`, not the pair `check_daemon_reachable()`'s
        callers pass directly."""
        parsed = urlsplit(base_url)
        return cls(parsed.hostname or base_url, parsed.port or 0)


class DelegateInternalError(RuntimeError):
    """Raised by `post_delegate_internal()` when `/delegate/internal`
    responds with a non-2xx, structured `{"error": ..., "detail": ...}` body
    (see `delegate/internal_api.py`'s `_error_response()`) -- e.g.
    `no_candidates`, `chain_exhausted`, `invalid_request`. Carries both
    fields, plus the HTTP status code, so a caller can render its own
    command-specific message."""

    def __init__(self, error: str, detail: Any, *, status_code: int) -> None:
        self.error = error
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"{error}: {detail}")


def check_daemon_reachable(host: str, port: int, *, timeout: float = 5.0) -> None:
    """A bare HTTP request to the daemon's base URL -- any response (even a
    404, since neither MCP server mounts a route at `/`) proves a listener
    is actually there; a connection-level failure (`httpx.TransportError`,
    covering both `ConnectError` and a timeout) means it isn't.

    Sync (not `async def`): every current caller invokes this from
    synchronous code, before entering an `asyncio.run(...)` boundary of its
    own (`orchestrator/run.py`'s `run_task()`, `cli.py`'s `_cmd_run`) --
    matching the original `orchestrator/run.py`'s own implementation this
    replaces.
    """
    try:
        httpx.get(f"http://{host}:{port}/", timeout=timeout)
    except httpx.TransportError as exc:
        raise DaemonNotReachableError(host, port) from exc


def _auth_headers(token: str | None) -> dict[str, str]:
    """`Authorization: Bearer <token>` header for the daemon's shared-secret
    auth (`daemon_auth.py`) -- empty when `token` is `None`, so callers that
    haven't been threaded a token yet (during incremental rollout) degrade
    to the pre-auth behavior rather than sending a malformed header."""
    return {"Authorization": f"Bearer {token}"} if token else {}


async def post_json_to_internal_route(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    error_cls: type[Exception],
    timeout: float,
    token: str | None,
) -> dict[str, Any]:
    """POST `payload` (as JSON, with the daemon's shared-secret auth header)
    to `<base_url><path>` and return the decoded JSON body on success --
    the shared mechanics behind every internal route's client
    (`post_delegate_internal()` below; `graph/internal_api.py`'s
    `post_graph_internal_reindex()`), mirroring `delegate/client.py`'s
    async-client pattern applied to an internal call.

    Raises `DaemonNotReachableError` only on a genuine transport-level
    failure (a connection refusal -- the daemon isn't running/reachable at
    `base_url`). A client-side timeout does NOT raise this: a route handler
    (e.g. `graph/internal_api.py`'s reindex endpoint) can legitimately
    still be running server-side, past this call's own `timeout`, when the
    client gives up (Starlette does not cancel an in-flight `await` on
    client disconnect) -- `DaemonNotReachableError`'s "start it with `josu
    daemon start`" message would be actively wrong advice there. Every
    other failure -- a client-side timeout, a non-2xx structured
    `{"error": ..., "detail": ...}` body, or a non-JSON body from some
    other failure mode (e.g. Starlette's default error handler, which
    doesn't return JSON) -- raises `error_cls(error, detail,
    status_code=...)` instead, so callers can distinguish "daemon is down"
    from "daemon is slow/erroring but reachable." `error_cls` must accept
    `(error: str, detail: Any, *, status_code: int)`, matching
    `DelegateInternalError`'s/`GraphInternalError`'s shared shape.
    """
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            response = await client.post(path, json=payload, headers=_auth_headers(token))
    except httpx.TimeoutException as exc:
        raise error_cls(
            "request_timeout",
            f"request to {base_url}{path} exceeded this call's {timeout}s client-side "
            "timeout -- the daemon may still be reachable and the operation may still "
            "complete server-side; this does not necessarily mean the daemon is down",
            status_code=0,
        ) from exc
    except httpx.TransportError as exc:
        raise DaemonNotReachableError.from_base_url(base_url) from exc
    except httpx.RequestError as exc:
        # Mirrors `graph/gortex.py`'s `_call_tool()` fix for the same gap:
        # `TransportError` above covers connection/network failures, but
        # `httpx.RequestError` has other direct subclasses (e.g.
        # `DecodingError`) that would otherwise escape this function's
        # documented "every failure mode collapses to DaemonNotReachableError
        # or error_cls(...)" contract as a bare httpx exception. Routed
        # through `error_cls`, not `DaemonNotReachableError` -- the daemon
        # may well be reachable and mid-request when this fires, so "start
        # it with `josu daemon start`" would be just as misleading here as
        # it would be for the timeout case above.
        raise error_cls(
            "request_failed", f"request to {base_url}{path} failed: {exc}", status_code=0
        ) from exc

    try:
        data: dict[str, Any] = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raw_text = response.text[:MAX_RAW_BODY_CHARS_IN_ERROR]
        raise error_cls(
            "non_json_response",
            f"daemon responded with status {response.status_code} and a non-JSON body: "
            f"{raw_text!r}",
            status_code=response.status_code,
        ) from exc

    if response.status_code >= 400:
        raise error_cls(
            data.get("error", "error"), data.get("detail"), status_code=response.status_code
        )
    return data


async def post_delegate_internal(
    base_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 120.0,
    token: str | None = None,
) -> dict[str, Any]:
    """POST `payload` to `<base_url>` + `delegate/internal_api.py`'s
    `DELEGATE_INTERNAL_PATH` -- the delegate route's thin wrapper around
    `post_json_to_internal_route()`, used by `cli.py`'s `_cmd_delegate` and
    `proactive/watchers.py`'s commit-hook entry point.
    """
    return await post_json_to_internal_route(
        base_url,
        DELEGATE_INTERNAL_PATH,
        payload,
        error_cls=DelegateInternalError,
        timeout=timeout,
        token=token,
    )
