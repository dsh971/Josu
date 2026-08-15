"""Tests for GortexEngine (U1, U4) -- against a real, locally-bound HTTP
server fixture standing in for gortex's own `/v1/tools/{name}` endpoint,
mirroring `tests/delegate/test_client.py`'s fixture-server pattern. No
mocking of httpx internals.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from josu.graph.gortex import GortexEngine, GortexUnavailableError


@pytest.fixture
def fixture_server():
    holder: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            holder["respond"](self)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def set_respond(fn):
        holder["respond"] = fn

    yield f"http://127.0.0.1:{httpd.server_port}", set_respond

    httpd.shutdown()
    thread.join()


def _write_json(handler: BaseHTTPRequestHandler, status: int, body: dict):
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _write_json_raw(handler: BaseHTTPRequestHandler, status: int, payload: bytes):
    """Like `_write_json`, but for a body that's already valid JSON bytes
    not shaped like a Python dict (e.g. a bare array) -- `_write_json`'s
    `body: dict` parameter can't express that."""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


@pytest.mark.asyncio
async def test_search_returns_scoped_results(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        assert handler.path == "/v1/tools/search"
        _write_json(handler, 200, {"results": [{"id": "helper.py::greet"}]})

    set_respond(respond)
    engine = GortexEngine(base_url)
    results = await engine.search("greet", limit=5)
    assert len(results) == 1
    assert results[0]["id"] == "helper.py::greet"
    # Staleness caveat attached to every successful result (Key Technical
    # Decisions).
    assert "last indexed commit" in results[0]["caveats"]


@pytest.mark.asyncio
async def test_execute_dispatches_to_named_operation(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        assert handler.path == "/v1/tools/get_dependents"
        _write_json(handler, 200, {"callers": ["a.py::foo"]})

    set_respond(respond)
    engine = GortexEngine(base_url)
    result = await engine.execute("get_dependents", {"id": "b.py::bar"})
    assert result["callers"] == ["a.py::foo"]
    assert "last indexed commit" in result["caveats"]


@pytest.mark.asyncio
async def test_connection_refused_raises_unreachable():
    engine = GortexEngine("http://127.0.0.1:1")
    with pytest.raises(GortexUnavailableError) as exc_info:
        await engine.search("anything")
    assert exc_info.value.reason == "unreachable"


@pytest.mark.asyncio
async def test_timeout_raises_with_timeout_reason(fixture_server):
    import time

    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        time.sleep(0.5)
        _write_json(handler, 200, {"results": []})

    set_respond(respond)
    engine = GortexEngine(base_url, timeout=0.05)
    with pytest.raises(GortexUnavailableError) as exc_info:
        await engine.search("anything")
    assert exc_info.value.reason == "timeout"


@pytest.mark.asyncio
async def test_5xx_raises_with_http_error_reason(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 500, {"error": "boom"})

    set_respond(respond)
    engine = GortexEngine(base_url)
    with pytest.raises(GortexUnavailableError) as exc_info:
        await engine.execute("search", {})
    assert exc_info.value.reason == "http-error"


@pytest.mark.asyncio
async def test_unrecognized_operation_4xx_raises_with_http_error_reason(fixture_server):
    """R12's `execute` accepts an arbitrary operation string -- a typo'd or
    hallucinated tool name is a realistic input, not just a malformed
    argument to a known operation. Must funnel into the same
    GortexUnavailableError path as a 5xx, not propagate as a bare
    httpx.HTTPStatusError."""
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 404, {"error": "unknown tool"})

    set_respond(respond)
    engine = GortexEngine(base_url)
    with pytest.raises(GortexUnavailableError) as exc_info:
        await engine.execute("not_a_real_operation", {})
    assert exc_info.value.reason == "http-error"


@pytest.mark.asyncio
async def test_warming_marker_treated_as_no_data_not_error(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 200, {"warming": True})

    set_respond(respond)
    engine = GortexEngine(base_url)
    assert await engine.search("anything") == []
    assert await engine.execute("get_dependents", {}) == {}


@pytest.mark.asyncio
async def test_oversized_response_treated_as_unavailable_not_fully_buffered(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 200, {"padding": "x" * 10_000})

    set_respond(respond)
    engine = GortexEngine(base_url, max_response_bytes=100)
    with pytest.raises(GortexUnavailableError) as exc_info:
        await engine.search("anything")
    assert exc_info.value.reason == "http-error"


@pytest.mark.asyncio
async def test_build_is_a_no_op_and_makes_no_http_call(fixture_server, tmp_path: Path):
    """`index_repository` returns HTTP 404 against gortex's real, current
    tool surface -- build() no longer calls it at all; tracking is the
    user's own `gortex track` setup step now."""
    base_url, set_respond = fixture_server
    calls = []

    def respond(handler: BaseHTTPRequestHandler):
        calls.append(handler.path)
        _write_json(handler, 200, {"status": "ok"})

    set_respond(respond)
    engine = GortexEngine(base_url)
    assert await engine.build(tmp_path) is None
    assert calls == []


@pytest.mark.asyncio
async def test_search_results_null_treated_as_empty_not_a_crash(fixture_server):
    """A Go JSON encoder (gortex is a Go binary) commonly serializes a
    nil/empty slice as `"results": null`, not `"results": []`, for a
    zero-match search -- must degrade gracefully like any other no-data
    result, not raise TypeError from iterating None."""
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json(handler, 200, {"results": None})

    set_respond(respond)
    engine = GortexEngine(base_url)
    assert await engine.search("anything") == []


@pytest.mark.asyncio
async def test_non_object_top_level_json_raises_unavailable_not_attributeerror(fixture_server):
    """A 200 response whose body is valid JSON but not an object (e.g. a
    bare array, string, or null) must still funnel into
    GortexUnavailableError, matching this module's own documented 'every
    failure mode collapses to GortexUnavailableError' contract -- not an
    unhandled AttributeError from `.get()` on a non-dict."""
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        _write_json_raw(handler, 200, b"[1, 2, 3]")

    set_respond(respond)
    engine = GortexEngine(base_url)
    with pytest.raises(GortexUnavailableError) as exc_info:
        await engine.search("anything")
    assert exc_info.value.reason == "http-error"


@pytest.mark.asyncio
async def test_execute_rejects_unsafe_operation_name_without_a_network_call(fixture_server):
    """`operation` is LLM-controlled (comes straight from a Claude Code tool
    call with no upstream validation) and gets interpolated into a URL path
    segment -- a value like '../../admin' or 'foo?x=1' must be rejected
    before ever building a request, not sent to gortex verbatim."""
    base_url, set_respond = fixture_server
    called = []

    def respond(handler: BaseHTTPRequestHandler):
        called.append(handler.path)
        _write_json(handler, 200, {"status": "ok"})

    set_respond(respond)
    engine = GortexEngine(base_url)
    for unsafe in ("../../etc/passwd", "foo?admin=1", "foo/../bar", "foo bar", "foo\nbar"):
        with pytest.raises(GortexUnavailableError):
            await engine.execute(unsafe, {})
    assert called == []


@pytest.mark.asyncio
async def test_execute_allows_safe_operation_names(fixture_server):
    base_url, set_respond = fixture_server

    def respond(handler: BaseHTTPRequestHandler):
        assert handler.path == "/v1/tools/get_dependents.v2"
        _write_json(handler, 200, {"status": "ok"})

    set_respond(respond)
    engine = GortexEngine(base_url)
    await engine.execute("get_dependents.v2", {})


@pytest.mark.asyncio
async def test_update_is_a_no_op_and_makes_no_http_call(fixture_server, tmp_path: Path):
    """`reindex_repository` also returns HTTP 404 against gortex's real,
    current tool surface -- reindexing is owned entirely by the user's own
    gortex daemon's fsnotify watcher now."""
    base_url, set_respond = fixture_server
    calls = []

    def respond(handler: BaseHTTPRequestHandler):
        calls.append(handler.path)
        _write_json(handler, 200, {"status": "ok"})

    set_respond(respond)
    engine = GortexEngine(base_url)
    changed = [tmp_path / "a.py", tmp_path / "b.py"]
    assert await engine.update(tmp_path, changed) is None
    assert calls == []


@pytest.mark.asyncio
async def test_auth_token_sent_as_bearer_header_when_configured(fixture_server):
    base_url, set_respond = fixture_server
    received_auth = []

    def respond(handler: BaseHTTPRequestHandler):
        received_auth.append(handler.headers.get("Authorization"))
        _write_json(handler, 200, {"results": []})

    set_respond(respond)
    engine = GortexEngine(base_url, auth_token="secret-token-value")
    await engine.search("anything")
    assert received_auth == ["Bearer secret-token-value"]


@pytest.mark.asyncio
async def test_no_authorization_header_sent_when_auth_token_not_configured(fixture_server):
    base_url, set_respond = fixture_server
    received_auth = []

    def respond(handler: BaseHTTPRequestHandler):
        received_auth.append(handler.headers.get("Authorization"))
        _write_json(handler, 200, {"results": []})

    set_respond(respond)
    engine = GortexEngine(base_url)
    await engine.search("anything")
    assert received_auth == [None]
