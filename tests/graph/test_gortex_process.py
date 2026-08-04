"""Tests for gortex subprocess lifecycle (U15): spawn, health-check,
orphan validation. No real `gortex` binary required for these -- the
health-check/reuse logic is tested against a real fixture HTTP server
standing in for gortex's `/healthz` endpoint; spawn-failure paths are
tested against a guaranteed-missing binary name.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from josu.graph.gortex_process import (
    GortexProcess,
    GortexProcessError,
    check_gortex_reachable,
    terminate_gortex,
)


@pytest.fixture
def fixture_healthz_server():
    holder: dict = {"status": 200, "body": {"status": "ok"}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
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
    not pass -- reuse feeds a trust decision (see the function's own
    docstring), so it should require at least an object shape, not just
    "any parseable JSON"."""
    host, port, holder = fixture_healthz_server
    holder["body"] = ["not", "an", "object"]
    assert check_gortex_reachable(host, port) is False


def test_check_gortex_reachable_false_for_non_200(fixture_healthz_server):
    host, port, holder = fixture_healthz_server
    holder["status"] = 500
    assert check_gortex_reachable(host, port) is False


def test_check_gortex_reachable_false_for_non_json_body(monkeypatch):
    """A response that isn't valid JSON must be treated as "not gortex" --
    reusing anything answering the port on a weaker bar (e.g. bare-GET)
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


def test_spawn_missing_binary_raises_actionable_error(tmp_path, monkeypatch):
    # Guarantee `gortex` isn't found by clearing PATH to a directory with
    # nothing in it.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    from josu.graph.gortex_process import spawn_gortex

    with pytest.raises(GortexProcessError) as exc_info:
        spawn_gortex(tmp_path, host="127.0.0.1", port=1)
    assert "install" in str(exc_info.value).lower()


def test_terminate_gortex_is_a_noop_for_reused_survivor():
    """A `GortexProcess` with `popen=None` represents a reused survivor
    this process never spawned -- terminating it must be a no-op, never
    killing a process this daemon doesn't own."""
    reused = GortexProcess(host="127.0.0.1", port=1, popen=None)
    terminate_gortex(reused)  # must not raise


def test_terminate_gortex_terminates_a_real_spawned_process():
    """A real (if trivial) subprocess this process DID spawn is actually
    terminated -- not left running."""
    proc = subprocess.Popen(["sleep", "30"])
    gortex_process = GortexProcess(host="127.0.0.1", port=1, popen=proc)
    terminate_gortex(gortex_process)
    assert proc.poll() is not None


def test_spawn_argv_never_includes_watch_flag(tmp_path, monkeypatch):
    """Covers the 'josu owns triggering' decision: the spawned subprocess's
    argv must never include `--watch`."""
    captured_argv: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured_argv.append(argv)
            self.returncode = None

        def poll(self):
            return None

        stdout = None
        stderr = None

    def fake_check_reachable(host, port, timeout=1.0):
        return True

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "josu.graph.gortex_process.check_gortex_reachable", fake_check_reachable
    )

    from josu.graph.gortex_process import spawn_gortex

    spawn_gortex(tmp_path, host="127.0.0.1", port=12345)
    assert len(captured_argv) == 1
    assert "--watch" not in captured_argv[0]
    assert "gortex" in captured_argv[0][0]
