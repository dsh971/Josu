"""Tests for gortex connection-target liveness checking (U2) and the
version/tool-surface compatibility guard (U3). No real `gortex` binary or
spawn/terminate lifecycle here anymore -- josu is a pure client; the
health-check and capability-probe logic are tested against a real fixture
HTTP server standing in for gortex's `/healthz`/`/v1/tools/search`
endpoints. The version check is tested against a stubbed `subprocess.run`
-- see `check_gortex_version_compatible()`'s own docstring for why no
HTTP-based alternative exists.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from josu.graph.gortex_process import (
    GortexProcess,
    check_gortex_reachable,
    check_gortex_tool_surface_capable,
    check_gortex_version_compatible,
)


@pytest.fixture
def fixture_healthz_server():
    holder: dict = {"status": 200, "body": {"status": "ok"}, "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            holder["headers"] = dict(self.headers)
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(holder["body"]).encode("utf-8")
            self.send_response(holder["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield "127.0.0.1", httpd.server_port, holder

    httpd.shutdown()
    thread.join()


def test_check_gortex_reachable_true_for_valid_json_200(fixture_healthz_server):
    host, port, _holder = fixture_healthz_server
    assert check_gortex_reachable(host, port) is True


def test_check_gortex_reachable_false_for_non_object_json_body(fixture_healthz_server):
    """A bare JSON scalar/array (e.g. `true`, `[]`) with a 200 status must
    not pass -- this feeds a "usable as the graph engine" trust decision
    (see the function's own docstring), so it should require at least an
    object shape, not just "any parseable JSON"."""
    host, port, holder = fixture_healthz_server
    holder["body"] = ["not", "an", "object"]
    assert check_gortex_reachable(host, port) is False


def test_check_gortex_reachable_false_for_non_200(fixture_healthz_server):
    host, port, holder = fixture_healthz_server
    holder["status"] = 500
    assert check_gortex_reachable(host, port) is False


def test_check_gortex_reachable_false_for_non_json_body(monkeypatch):
    """A response that isn't valid JSON must be treated as "not gortex" --
    accepting anything answering the port on a weaker bar (e.g. bare-GET)
    would be too permissive for a decision that routes real graph queries
    to it (see module docstring)."""

    class FakeResponse:
        status_code = 200

        def json(self):
            raise json.JSONDecodeError("not json", "doc", 0)

    monkeypatch.setattr(
        "josu.graph.gortex_process.httpx.get", lambda *a, **k: FakeResponse()
    )
    assert check_gortex_reachable("127.0.0.1", 12345) is False


def test_check_gortex_reachable_false_when_nothing_listening():
    assert check_gortex_reachable("127.0.0.1", 1) is False


def test_check_gortex_reachable_sends_bearer_token_when_configured(fixture_healthz_server):
    """A gortex started with `--http-auth-token` may also protect
    `/healthz` -- the reachability probe must send the same credential
    the capability probe already does, or a correctly-configured
    authenticated target gets misreported as unreachable."""
    host, port, holder = fixture_healthz_server
    assert check_gortex_reachable(host, port, auth_token="secret-token-value") is True
    assert holder["headers"].get("Authorization") == "Bearer secret-token-value"


def test_check_gortex_reachable_false_for_invalid_url_host():
    """A bracket-less IPv6 host (e.g. the literal `::1`, which
    `config/graph_engines.py`'s own `_LOOPBACK_HOSTS` lists as a valid
    loopback value) must degrade to `False`, not raise `httpx.InvalidURL`
    -- an uncaught exception here would crash `josu daemon start`
    entirely instead of degrading to no graph engine."""
    assert check_gortex_reachable("::1", 7411, timeout=1.0) is False


def test_gortex_process_base_url():
    process = GortexProcess(host="127.0.0.1", port=7411)
    assert process.base_url == "http://127.0.0.1:7411"


# --- check_gortex_version_compatible() -------------------------------------


def _fake_run(stdout: str):
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    return _run


def test_version_compatible_within_range(monkeypatch):
    monkeypatch.setattr(
        "josu.graph.gortex_process.subprocess.run",
        _fake_run("gortex v0.62.0+99d745c\n  commit: 99d745c\n"),
    )
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert detail == ""


def test_version_below_minimum_is_incompatible(monkeypatch):
    monkeypatch.setattr(
        "josu.graph.gortex_process.subprocess.run", _fake_run("gortex v0.30.0+abcdef\n")
    )
    compatible, detail = check_gortex_version_compatible()
    assert compatible is False
    assert "0.30.0" in detail
    assert "upgrade" in detail.lower()


def test_version_above_ceiling_warns_but_compatible(monkeypatch):
    monkeypatch.setattr(
        "josu.graph.gortex_process.subprocess.run", _fake_run("gortex v9.9.9+ffffff\n")
    )
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert "9.9.9" in detail


def test_version_check_skips_gracefully_when_binary_missing(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _raise)
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert detail == ""


def test_version_check_skips_gracefully_on_timeout(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gortex", timeout=5.0)

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _raise)
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert detail == ""


def test_version_check_skips_gracefully_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(
        "josu.graph.gortex_process.subprocess.run", _fake_run("not a version string at all\n")
    )
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert detail == ""


def test_version_check_skips_gracefully_on_permission_error(monkeypatch):
    """A `gortex` on PATH that exists but isn't executable (e.g. wrong
    file mode) raises `PermissionError`, a distinct `OSError` subtype
    from `FileNotFoundError` -- must degrade the same way, not crash
    `josu daemon start`."""

    def _raise(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _raise)
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert detail == ""


def test_version_check_skips_gracefully_on_non_utf8_output(monkeypatch):
    """A `gortex` on PATH emitting non-UTF-8 bytes (corrupt binary, a
    locale-encoded wrapper script) raises `UnicodeDecodeError` under
    `text=True`'s default strict decoding -- must degrade, not crash."""

    def _raise(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _raise)
    compatible, detail = check_gortex_version_compatible()
    assert compatible is True
    assert detail == ""


def test_version_check_skipped_entirely_for_non_loopback_host(monkeypatch):
    """A local `gortex` binary says nothing about a remote target's
    version -- the check must not even shell out for a non-loopback
    host, let alone let a stale local binary disable a healthy remote
    target."""
    calls = []

    def _run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="gortex v0.30.0\n", stderr="")

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _run)
    compatible, detail = check_gortex_version_compatible("gortex.internal.example.com")
    assert compatible is True
    assert detail == ""
    assert calls == []


def test_version_check_runs_subprocess_with_restricted_env(monkeypatch):
    """The `gortex version` subprocess must not inherit the daemon's full
    ambient environment (which carries resolved delegate API credentials)
    -- only PATH/HOME should be passed."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("SOME_SECRET_API_KEY", "should-not-be-passed-through")

    captured_env = {}

    def _run(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="gortex v0.62.0\n", stderr="")

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _run)
    check_gortex_version_compatible()

    assert captured_env == {"PATH": "/usr/bin", "HOME": "/home/test"}
    assert "SOME_SECRET_API_KEY" not in captured_env


# --- check_gortex_tool_surface_capable() ------------------------------------


@pytest.fixture
def fixture_tools_server():
    holder: dict = {"status": 200, "body": {"results": []}, "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            holder["headers"] = dict(self.headers)
            payload = json.dumps(holder["body"]).encode("utf-8")
            self.send_response(holder["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield "127.0.0.1", httpd.server_port, holder

    httpd.shutdown()
    thread.join()


def test_tool_surface_capable_for_successful_search_response(fixture_tools_server):
    host, port, _holder = fixture_tools_server
    capable, detail = check_gortex_tool_surface_capable(host, port)
    assert capable is True
    assert detail == ""


def test_tool_surface_not_capable_for_tool_blocked_by_mode(fixture_tools_server):
    host, port, holder = fixture_tools_server
    holder["body"] = {
        "content": [
            {
                "type": "text",
                "text": (
                    '{"error_code":"tool_blocked_by_mode","message":'
                    '"\\"search\\" belongs to the \\"facade-v1\\" MCP surface"}'
                ),
            }
        ],
        "isError": True,
    }
    capable, detail = check_gortex_tool_surface_capable(host, port)
    assert capable is False
    assert "--tools facade-v1" in detail


def test_tool_surface_not_capable_when_unreachable():
    capable, detail = check_gortex_tool_surface_capable("127.0.0.1", 1)
    assert capable is False
    assert "could not reach" in detail


def test_tool_surface_capability_probe_sends_bearer_token_when_configured(fixture_tools_server):
    host, port, holder = fixture_tools_server
    check_gortex_tool_surface_capable(host, port, auth_token="secret-token-value")
    assert holder["headers"].get("Authorization") == "Bearer secret-token-value"


def test_tool_surface_not_capable_when_content_is_not_a_list(fixture_tools_server):
    """A malformed error body where `content` is present but not a list
    (e.g. a bare dict) must degrade to `capable=False`, not raise
    `KeyError`/`TypeError` out of a function documented to never raise."""
    host, port, holder = fixture_tools_server
    holder["body"] = {"isError": True, "content": {"text": "unexpected shape"}}
    capable, detail = check_gortex_tool_surface_capable(host, port)
    assert capable is False
    assert "unexpected shape" in detail or "returned an error" in detail


def test_tool_surface_not_capable_when_content_first_item_is_not_a_dict(fixture_tools_server):
    host, port, holder = fixture_tools_server
    holder["body"] = {"isError": True, "content": ["not-a-dict"]}
    capable, detail = check_gortex_tool_surface_capable(host, port)
    assert capable is False


def test_tool_surface_not_capable_when_content_is_empty_list(fixture_tools_server):
    host, port, holder = fixture_tools_server
    holder["body"] = {"isError": True, "content": []}
    capable, detail = check_gortex_tool_surface_capable(host, port)
    assert capable is False
