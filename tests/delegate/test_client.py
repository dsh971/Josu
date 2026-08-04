"""Tests for the generic OpenAI-compatible delegate client (U11).

The happy path and every HTTP-level error path run against a real,
locally-bound `http.server` fixture -- not mocks -- since the whole point of
this client is genuine HTTP behavior (status codes, headers, streaming).
Only the credential-in-message check and the malformed-content-after-retry
interaction with `local_model.py` need a hand-written fake, since those
aren't cleanly reproducible as one HTTP response shape.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from josu.delegate.client import (
    DelegateAPIError,
    DelegateMalformedResponseError,
    DelegateRateLimitedError,
    DelegateUnreachableError,
    OpenAICompatibleDelegateClient,
)


@pytest.fixture
def fixture_server():
    """A real HTTP server on a free port. Tests configure its single POST
    response via `set_respond(fn)`, where `fn(handler)` writes the response
    using the stdlib `BaseHTTPRequestHandler` API."""
    holder: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            holder["respond"](self)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # keep test output quiet

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def set_respond(fn):
        holder["respond"] = fn

    yield f"http://127.0.0.1:{httpd.server_port}", set_respond

    httpd.shutdown()
    thread.join()


def _write_json(handler: BaseHTTPRequestHandler, status: int, body: dict, headers=None):
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(payload)


@pytest.mark.asyncio
async def test_live_fixture_server_returns_parsed_content(fixture_server):
    base_url, set_respond = fixture_server
    expected_content = json.dumps({"result": "ok", "caveats": ""})

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(
            handler,
            200,
            {"choices": [{"message": {"content": expected_content}}]},
        )

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture")
    content = await client.complete(
        model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
    )
    assert content == expected_content


@pytest.mark.asyncio
async def test_connection_refused_raises_unreachable():
    # Nothing is listening on this port -- connection refused immediately.
    client = OpenAICompatibleDelegateClient("http://127.0.0.1:1", name="dead-candidate")
    with pytest.raises(DelegateUnreachableError):
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=2
        )


@pytest.mark.asyncio
async def test_429_raises_rate_limited_with_retry_after(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 429, {"error": "rate limited"}, headers={"Retry-After": "17"})

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture")
    with pytest.raises(DelegateRateLimitedError) as exc_info:
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )
    assert exc_info.value.retry_after == "17"


@pytest.mark.asyncio
async def test_500_raises_api_error_distinct_from_rate_limit_and_malformed(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 500, {"error": "boom"})

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture")
    with pytest.raises(DelegateAPIError) as exc_info:
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_200_unparseable_body_raises_malformed(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        body = b"not json at all"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture")
    with pytest.raises(DelegateMalformedResponseError):
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )


@pytest.mark.asyncio
async def test_200_missing_choices_shape_raises_malformed(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 200, {"unexpected": "shape"})

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture")
    with pytest.raises(DelegateMalformedResponseError):
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )


@pytest.mark.asyncio
async def test_deeply_nested_response_body_raises_malformed_not_recursion_error(fixture_server):
    """Covers the P1 fix: a small but deeply nested (2000+ levels) JSON
    response body used to raise an uncaught `RecursionError` from inside
    `json.loads` -- past both R24's same-candidate retry and R34's
    chain-advance taxonomy. It must now surface as the same
    `DelegateMalformedResponseError` any other unparseable body produces."""
    base_url, set_respond = fixture_server
    deeply_nested = ("[" * 3000) + ("]" * 3000)

    def respond(handler: BaseHTTPRequestHandler):
        body = deeply_nested.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture")
    with pytest.raises(DelegateMalformedResponseError):
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )


@pytest.mark.asyncio
async def test_oversized_response_treated_as_malformed_not_fully_buffered(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        # Larger than the client's configured max -- must be rejected before
        # the whole body is buffered/parsed.
        oversized = json.dumps({"padding": "x" * 10_000}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(oversized)))
        handler.end_headers()
        handler.wfile.write(oversized)

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture", max_response_bytes=100)
    with pytest.raises(DelegateMalformedResponseError):
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )


def test_exception_str_never_contains_api_key_or_authorization_header():
    api_key = "sk-super-secret-value-should-never-leak"
    unreachable = DelegateUnreachableError("my-candidate")
    api_error = DelegateAPIError("my-candidate", status_code=401)
    rate_limited = DelegateRateLimitedError("my-candidate", retry_after="30")
    malformed = DelegateMalformedResponseError("my-candidate")

    for exc in (unreachable, api_error, rate_limited, malformed):
        text = f"{exc!s} {exc!r}"
        assert api_key not in text
        assert "Authorization" not in text
        assert "Bearer" not in text
        assert "my-candidate" in text


@pytest.mark.asyncio
async def test_api_key_never_appears_in_error_when_upstream_echoes_it(fixture_server):
    """Even if an upstream API's error body echoes the submitted key back
    (the real-world failure mode security review flagged), the client never
    reads that body for a 4xx/5xx response, so it can't leak into the
    exception message."""
    base_url, set_respond = fixture_server
    api_key = "sk-should-never-appear-anywhere"

    def respond(handler: BaseHTTPRequestHandler):
        # Simulate a misbehaving upstream echoing the submitted key in an
        # error body.
        auth = handler.headers.get("Authorization", "")
        assert api_key in auth  # sanity: the key really was sent
        _write_json(handler, 401, {"error": f"invalid key: {auth}"})

    set_respond(respond)
    client = OpenAICompatibleDelegateClient(base_url, name="fixture", api_key=api_key)
    with pytest.raises(DelegateAPIError) as exc_info:
        await client.complete(
            model="test-model", messages=[{"role": "user", "content": "hi"}], timeout=5
        )
    assert api_key not in str(exc_info.value)
    assert api_key not in repr(exc_info.value)
