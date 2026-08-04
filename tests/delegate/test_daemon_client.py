"""Tests for `delegate/daemon_client.py`'s shared daemon-HTTP helpers (U13/U14
simplify pass).

`httpx.MockTransport` stands in for the real network here -- these tests care
about `post_delegate_internal()`'s OWN response-handling logic (transport
failure -> `DaemonNotReachableError`, non-2xx structured body ->
`DelegateInternalError`, non-JSON body -> `DelegateInternalError`, P2 fix),
not a real daemon round trip (already covered end-to-end by
`tests/test_daemon.py`).
"""

from __future__ import annotations

import json

import httpx
import pytest

from josu.delegate.daemon_client import (
    DaemonNotReachableError,
    DelegateInternalError,
    post_delegate_internal,
)

# Captured BEFORE any monkeypatching below replaces `httpx.AsyncClient` --
# each fake client below must build a REAL `httpx.AsyncClient` (wired to a
# `MockTransport`), not recursively call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_transport(monkeypatch, handler) -> None:
    """Monkeypatch `daemon_client.py`'s `httpx.AsyncClient(base_url=...,
    timeout=...)` construction to route through a `MockTransport` wrapping
    `handler`, instead of a real network connection."""

    def _fake_async_client(*, base_url, timeout):
        return _REAL_ASYNC_CLIENT(
            base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr("josu.delegate.daemon_client.httpx.AsyncClient", _fake_async_client)


@pytest.mark.asyncio
async def test_post_delegate_internal_returns_parsed_json_on_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "ok", "caveats": "", "model": "m"})

    _patch_transport(monkeypatch, handler)

    data = await post_delegate_internal("http://daemon.invalid", {"task": "x", "task_type": "t"})
    assert data == {"result": "ok", "caveats": "", "model": "m"}


@pytest.mark.asyncio
async def test_post_delegate_internal_transport_failure_raises_daemon_not_reachable():
    """No mock transport needed here -- a genuinely unbound loopback port
    produces a real `httpx.ConnectError`, exercising the actual transport
    failure path rather than a simulated one."""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    _host, unused_port = probe.getsockname()
    probe.close()

    with pytest.raises(DaemonNotReachableError):
        await post_delegate_internal(f"http://127.0.0.1:{unused_port}", {"task": "x"})


@pytest.mark.asyncio
async def test_post_delegate_internal_structured_error_response_raises_delegate_internal_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "no_candidates", "detail": "nothing configured"})

    _patch_transport(monkeypatch, handler)

    with pytest.raises(DelegateInternalError) as exc_info:
        await post_delegate_internal("http://daemon.invalid", {"task": "x", "task_type": "t"})

    assert exc_info.value.error == "no_candidates"
    assert exc_info.value.detail == "nothing configured"
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_post_delegate_internal_non_json_error_response_is_a_clean_delegate_internal_error(monkeypatch):
    """P2 fix: a non-JSON 500 response (e.g. an unhandled exception inside
    `execute_chain()` that isn't a `DelegateError` subclass, hitting
    Starlette's default error handler, which returns plain text/HTML, not
    JSON) must surface as a clean `DelegateInternalError`, not an uncaught
    `json.JSONDecodeError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, content=b"Internal Server Error", headers={"content-type": "text/plain"}
        )

    _patch_transport(monkeypatch, handler)

    with pytest.raises(DelegateInternalError) as exc_info:
        await post_delegate_internal("http://daemon.invalid", {"task": "x", "task_type": "t"})

    assert exc_info.value.status_code == 500
    assert "Internal Server Error" in str(exc_info.value)


@pytest.mark.asyncio
async def test_post_delegate_internal_non_json_response_never_raises_json_decode_error(monkeypatch):
    """A stronger form of the P2 proof: the raw stdlib `json.JSONDecodeError`
    itself must never escape `post_delegate_internal()` -- only this
    module's own `DelegateInternalError`/`DaemonNotReachableError` types
    are allowed to reach a caller."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    _patch_transport(monkeypatch, handler)

    try:
        await post_delegate_internal("http://daemon.invalid", {"task": "x", "task_type": "t"})
    except json.JSONDecodeError:
        pytest.fail("a raw json.JSONDecodeError escaped post_delegate_internal()")
    except DelegateInternalError:
        pass  # expected
