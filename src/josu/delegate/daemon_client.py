"""Shared daemon-client helper (U13/U14 simplify pass).

`cli.py`'s `_cmd_delegate`/`_cmd_run`, `orchestrator/run.py`'s `run_task()`,
and `proactive/watchers.py`'s commit-hook entry point each independently
hand-rolled an `httpx` client + `except httpx.TransportError` block, with
slightly different "daemon not reachable" message text depending on which
file you happened to be reading. This module is the one place that logic
lives now:

- `DaemonNotReachableError` -- one canonical exception/message, raised on a
  transport-level failure talking to the daemon. Previously duplicated as
  `orchestrator/run.py`'s own class of the same name; that module now
  imports this one instead of defining its own.
- `check_daemon_reachable()` -- a bare HTTP GET against the daemon's base
  URL used as a pure reachability probe (any response, even a 404, proves a
  listener is there). Sync, since every current caller
  (`orchestrator/run.py`'s `run_task()`, `cli.py`'s `_cmd_run`) calls it
  from synchronous code, before any `asyncio.run()` boundary.
- `post_delegate_internal()` -- the actual POST to `delegate/internal_api.py`'s
  `/delegate/internal` route, used by `cli.py`'s `_cmd_delegate` and
  `proactive/watchers.py`'s commit-hook entry point. Async, since both
  callers already run inside `asyncio.run(...)`. Raises
  `DaemonNotReachableError` on a transport failure, or `DelegateInternalError`
  on a non-2xx structured error response from the route; returns the parsed
  JSON body on success.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from josu.delegate.internal_api import DELEGATE_INTERNAL_PATH


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


async def post_delegate_internal(
    base_url: str, payload: dict[str, Any], *, timeout: float = 120.0
) -> dict[str, Any]:
    """POST `payload` to `<base_url>` + `delegate/internal_api.py`'s
    `DELEGATE_INTERNAL_PATH` -- the shared implementation behind `cli.py`'s
    `_cmd_delegate` and `proactive/watchers.py`'s commit-hook entry point,
    both one-shot `httpx` clients of the daemon's internal delegate route,
    mirroring `delegate/client.py`'s async-client pattern applied to an
    internal call.

    Raises `DaemonNotReachableError` on a transport-level failure (the
    daemon isn't running/reachable at `base_url`), or `DelegateInternalError`
    on a non-2xx structured error response. Returns the parsed JSON body on
    success (2xx).
    """
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            response = await client.post(DELEGATE_INTERNAL_PATH, json=payload)
    except httpx.TransportError as exc:
        raise DaemonNotReachableError.from_base_url(base_url) from exc

    data: dict[str, Any] = response.json()
    if response.status_code >= 400:
        raise DelegateInternalError(
            data.get("error", "error"), data.get("detail"), status_code=response.status_code
        )
    return data
