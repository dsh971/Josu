"""Generic async OpenAI-compatible delegate client (U11).

Hand-rolled on `httpx.AsyncClient` rather than the `openai` SDK: josu's one
call shape is non-streaming chat completions in JSON mode, and the SDK's
surface (files, embeddings, assistants, batch, fine-tuning) is unused weight
that also doesn't cover the "malformed response" failure leg R24 already
needs regardless of transport. See plan Key Technical Decisions.

`DelegateClient` is the swap-boundary Protocol (mirroring `graph/engine.py`'s
`GraphEngine` idiom, not its execution model -- `GraphEngine` is
sync/build-then-query, this is async/per-call) that any concrete transport
implements. `OpenAICompatibleDelegateClient` is the one production
implementation, POSTing to `{endpoint}/chat/completions`.

The four exception types below are the taxonomy U12's fallback-chain logic
acts on: unreachable/rate-limited/API-error advance the chain to the next
candidate (R34); a malformed response instead triggers the existing
same-candidate one-retry-then-fail behavior (R24), since the candidate is
reachable and functioning, just returned garbage once. None of the four
ever include the raw `Authorization` header or resolved API key in their
string representation -- only the candidate name (and, where relevant, a
non-sensitive value like an HTTP status code or `Retry-After`) -- since some
upstream APIs echo submitted keys (masked or not) in error bodies, and the
exception types are the enforcement point that keeps that out of anything
downstream (logs, run log, error messages) touches.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

DEFAULT_MAX_RESPONSE_BYTES = 2_000_000


class DelegateError(Exception):
    """Common base for the delegate-client exception taxonomy.

    Never constructed directly -- always one of the four subclasses below.
    Subclass `__init__`s are the single place that formats `str()`/`repr()`
    output, so credential-safety only has to be verified in one spot.
    """

    def __init__(self, candidate: str, detail: str) -> None:
        self.candidate = candidate
        super().__init__(f"delegate candidate {candidate!r}: {detail}")


class DelegateUnreachableError(DelegateError):
    """The candidate's endpoint could not be reached at all (connection
    refused/timed out, DNS failure) -- distinct from a candidate that
    responded but errored (`DelegateAPIError`)."""

    def __init__(self, candidate: str, detail: str = "unreachable") -> None:
        super().__init__(candidate, detail)


class DelegateRateLimitedError(DelegateError):
    """The candidate responded HTTP 429. Carries `retry_after` (the raw
    header value, a non-sensitive number-of-seconds or HTTP-date) for later
    run-log use, never anything from the response body."""

    def __init__(self, candidate: str, retry_after: str | None = None) -> None:
        self.retry_after = retry_after
        detail = "rate limited" + (f" (retry-after: {retry_after})" if retry_after else "")
        super().__init__(candidate, detail)


class DelegateAPIError(DelegateError):
    """The candidate responded with a non-429 4xx/5xx status. Carries only
    the status code -- never the response body, which is exactly where a
    misbehaving upstream could echo a submitted credential back."""

    def __init__(self, candidate: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(candidate, f"API error (HTTP {status_code})")


class DelegateMalformedResponseError(DelegateError):
    """The candidate returned HTTP 200 but a body that isn't a parseable
    OpenAI-shaped chat-completion response, or exceeded the configured
    maximum response size."""

    def __init__(self, candidate: str, detail: str = "malformed response") -> None:
        super().__init__(candidate, detail)


class DelegateClient(Protocol):
    """Minimal surface a delegate transport implementation must provide.

    One call shape: a non-streaming, JSON-mode chat completion. Returns the
    assistant message's raw `content` string -- parsing that string into
    josu's own `{result, caveats}` shape is `local_model.py`'s job, not
    this layer's.
    """

    async def complete(
        self, *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> str: ...


class OpenAICompatibleDelegateClient:
    """Async client for any OpenAI-API-compatible `/chat/completions` endpoint.

    Owns no long-lived connection -- each `complete()` call opens and closes
    its own `httpx.AsyncClient`, keeping this class free of a connection
    lifecycle callers would otherwise have to manage (there is no daemon
    shutdown hook wired up yet for that; see U11 scope notes).
    """

    def __init__(
        self,
        endpoint: str,
        *,
        name: str,
        api_key: str | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._name = name
        self._api_key = api_key
        self._max_response_bytes = max_response_bytes

    async def complete(
        self, *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> str:
        url = f"{self._endpoint}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "stream": False,
        }

        try:
            async with httpx.AsyncClient() as http:
                async with http.stream(
                    "POST", url, json=payload, headers=headers, timeout=timeout
                ) as response:
                    if response.status_code == 429:
                        raise DelegateRateLimitedError(
                            self._name, retry_after=response.headers.get("Retry-After")
                        )
                    if response.status_code >= 400:
                        raise DelegateAPIError(self._name, status_code=response.status_code)

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_response_bytes:
                            raise DelegateMalformedResponseError(
                                self._name,
                                f"response exceeded maximum size of "
                                f"{self._max_response_bytes} bytes",
                            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException) as exc:
            raise DelegateUnreachableError(self._name) from exc

        try:
            data = json.loads(bytes(body))
            return str(data["choices"][0]["message"]["content"])
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise DelegateMalformedResponseError(
                self._name, "response body is not a parseable chat-completion shape"
            ) from exc
        except RecursionError as exc:
            # A deeply (but not necessarily large-byte-wise) nested response
            # body -- e.g. thousands of levels of nested arrays/objects --
            # blows CPython's recursion limit inside `json.loads`'s
            # recursive-descent parser before ever raising `JSONDecodeError`.
            # Treated as exactly the same "malformed response" outcome as
            # any other unparseable body, not an uncaught crash that bypasses
            # R24's same-candidate retry / R34's chain-advance handling.
            raise DelegateMalformedResponseError(
                self._name, "response body is too deeply nested to parse"
            ) from exc
