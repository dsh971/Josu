"""gortex-backed implementation of the GraphEngine interface (R10, U1).

An async `httpx` client talking to a gortex daemon the user runs and
manages themselves -- josu never spawns it (see `daemon.py`, `config/
graph_engines.py`). This module only needs a base URL and, optionally, a
bearer token the user configured via `[[graph.engines]]`'s `api_key_env`.

Gortex's real dispatch primitive is `POST /v1/tools/{name}` -- "invoke any
tool by name with a JSON argument object" -- reachable directly via `httpx`
without standing up an MCP client inside the daemon. `execute(operation,
params)` maps onto this directly: any of gortex's tool names (the
`facade-v1` preset's ~21-tool compact surface, which the daemon requires
the configured target to be started with -- see `graph/gortex_process.py`'s
capability check) is a valid `operation` string. `search(query, limit)`
calls gortex's own `search` tool the same way.

`build()`/`update()` are no-ops: `index_repository`/`reindex_repository`
don't exist in gortex's current tool surface (confirmed live -- HTTP 404),
and since josu never runs `gortex track` itself either, there is nothing
left for either method to do. Tracking and reindexing are entirely the
user's own gortex's business, continuously, via its own fsnotify watcher.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from josu.graph.engine import GraphEngineUnavailableError

# `execute(operation, ...)`'s `operation` argument is LLM-controlled (it
# comes straight from a Claude Code tool call, `graph/server.py::call_tool`,
# with no validation upstream of this module) and is interpolated directly
# into a URL path segment below. Without this allowlist, a value like
# `"../../admin"` would escape gortex's intended `/v1/tools/*` dispatch
# namespace entirely (httpx normalizes `..` path segments at request-
# construction time), and a value containing `?` would inject arbitrary
# query parameters into the outbound request -- both realistic inputs given
# `operation` can originate from prompt-injected content the agent read
# (a file, a search result) rather than the developer's own typed request.
# Matches gortex's own documented tool-name shape (lowercase snake_case);
# rejecting anything else is not a functional restriction on legitimate use.
_SAFE_OPERATION_NAME = re.compile(r"^[a-zA-Z0-9_.:-]+$")

# Set safely above gortex's own documented 60s internal tool-call timeout,
# so gortex's own timeout is always the one that actually fires -- a
# shorter client timeout risks misclassifying a slow-but-working query as
# unreachable; a much longer one risks a hung call eating into the
# unrelated per-task circuit-breaker budget invisibly. See plan Key
# Technical Decisions.
DEFAULT_CLIENT_TIMEOUT_SECONDS = 70.0

# A response this large is treated as malformed rather than buffered
# without limit -- mirrors `delegate/client.py`'s `DEFAULT_MAX_RESPONSE_BYTES`
# bound, carried over to close the same unbounded-memory risk on the graph
# side (the daemon is a single shared process; an oversized response here
# has the same blast radius as an oversized delegate response).
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000

_STALENESS_CAVEAT = (
    "graph data reflects the primary repo's last indexed commit -- it may not "
    "include this task's own in-progress, uncommitted edits."
)


class GortexUnavailableError(GraphEngineUnavailableError):
    """Every gortex-communication failure (connection refused, timeout, a
    5xx response, or a 4xx from an unrecognized `execute` operation name)
    translates to this -- a `RuntimeError` subclass so `graph/server.py`'s
    `call_tool` and `delegate/local_model.py`'s `_graph_context()` catch it
    via their existing `RuntimeError` clauses without modification. `reason`
    is one of "unreachable", "timeout", "http-error", or "warming" -- see
    `GraphEngineUnavailableError`'s docstring for why this internal
    granularity exists despite the uniform external behavior.
    """

    def __init__(self, detail: str, *, reason: str) -> None:
        super().__init__(f"gortex {reason}: {detail}", reason=reason)


def _attach_caveat(result: dict) -> dict:
    """Attach the staleness caveat to a successful result, without
    clobbering an existing `caveats` key gortex's own response might carry."""
    existing = result.get("caveats")
    if existing:
        result = {**result, "caveats": f"{existing} {_STALENESS_CAVEAT}"}
    else:
        result = {**result, "caveats": _STALENESS_CAVEAT}
    return result


class GortexEngine:
    """Directory-scoped context graph backed by a user-run gortex daemon,
    reached over `POST /v1/tools/{name}`. Owns no process lifecycle of its
    own -- `base_url` must already point at a reachable, correctly-
    configured gortex instance (`daemon.py`'s job to resolve and verify)."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        timeout: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    async def _call_tool(self, name: str, params: dict) -> dict:
        """POST `params` to gortex's `/v1/tools/{name}` and return the
        decoded JSON body. Every failure mode collapses to
        `GortexUnavailableError`:

        - connection refused / DNS failure -> reason "unreachable"
        - request exceeded `self._timeout` -> reason "timeout"
        - HTTP 5xx, or 4xx from an unrecognized/invalid operation name (R12's
          `execute` accepts an arbitrary string, so a typo'd or hallucinated
          tool name is a realistic input, not just a malformed-argument
          case) -> reason "http-error"
        - response body exceeds `self._max_response_bytes` -> reason
          "http-error" (treated the same as any other malformed/oversized
          response, not buffered without limit)

        A `warming` marker in an otherwise-successful response is NOT an
        error here -- gortex returns it in-band on a 200 while its initial
        index is still building. Callers (`search`/`execute` below) treat it
        as a no-data result, with no retry.

        Raises `GortexUnavailableError` (reason "http-error") immediately,
        without any network call, if `name` doesn't match
        `_SAFE_OPERATION_NAME` -- see that pattern's own comment for why
        this validation is load-bearing, not defense-in-depth.
        """
        if not _SAFE_OPERATION_NAME.match(name):
            raise GortexUnavailableError(
                f"refusing to call gortex with unsafe operation name {name!r}",
                reason="http-error",
            )
        url = f"{self._base_url}/v1/tools/{name}"
        headers = {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=params, headers=headers) as response:
                    if response.status_code >= 400:
                        raise GortexUnavailableError(
                            f"HTTP {response.status_code} from {url}", reason="http-error"
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_response_bytes:
                            raise GortexUnavailableError(
                                f"response from {url} exceeded maximum size of "
                                f"{self._max_response_bytes} bytes",
                                reason="http-error",
                            )
        except httpx.TimeoutException as exc:
            raise GortexUnavailableError(f"request to {url} timed out", reason="timeout") from exc
        except httpx.TransportError as exc:
            raise GortexUnavailableError(f"{url} unreachable: {exc}", reason="unreachable") from exc
        except httpx.RequestError as exc:
            # Reliability-review fix: `TransportError` above covers the
            # common connection/network-failure cases, but `httpx.
            # RequestError` has other direct subclasses outside that branch
            # (e.g. `DecodingError`) that would otherwise escape this
            # method's documented "every failure mode collapses to
            # GortexUnavailableError" contract as a bare httpx exception.
            raise GortexUnavailableError(f"request to {url} failed: {exc}", reason="unreachable") from exc

        try:
            data = json.loads(bytes(body))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GortexUnavailableError(
                f"response from {url} was not valid JSON", reason="http-error"
            ) from exc
        # A 200 response is only usable if it's a JSON object -- gortex's
        # own error paths, or any unhandled edge case in its HTTP layer,
        # could in principle return a bare array/string/number/null with a
        # 200 status. Treating that as malformed (rather than letting
        # `.get()` calls below raise AttributeError) keeps every failure
        # mode inside this method's own documented GortexUnavailableError
        # contract, matching this method's own docstring.
        if not isinstance(data, dict):
            raise GortexUnavailableError(
                f"response from {url} was valid JSON but not an object "
                f"(got {type(data).__name__})",
                reason="http-error",
            )
        return data

    async def build(self, root: Path) -> None:
        """No-op -- `index_repository` doesn't exist in gortex's current
        tool surface (HTTP 404, confirmed live). Getting a repo tracked is
        the user's own `gortex track` setup step now, not something josu
        triggers via a tool call."""
        return None

    async def update(self, root: Path, changed_files: list[Path]) -> None:
        """No-op -- `reindex_repository` doesn't exist in gortex's current
        tool surface either. Reindexing is owned entirely, continuously,
        by the user's own gortex daemon's fsnotify watcher once a repo is
        tracked -- there is nothing left for josu to trigger here."""
        return None

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        result = await self._call_tool("search", {"query": query, "limit": limit})
        if result.get("warming"):
            return []
        # `or []`, not a bare `.get("results", [])` default: the default
        # only applies when the key is absent, but a Go JSON encoder (gortex
        # is a Go binary) commonly serializes a nil/empty slice as
        # `"results": null` for "zero matches" -- a present-but-null value
        # would otherwise reach the list comprehension below as `None` and
        # crash with `TypeError: 'NoneType' object is not iterable`.
        results = result.get("results") or []
        return [_attach_caveat(item) if isinstance(item, dict) else item for item in results]

    async def execute(self, operation: str, params: dict) -> dict:
        result = await self._call_tool(operation, params)
        if result.get("warming"):
            return {}
        return _attach_caveat(result)
