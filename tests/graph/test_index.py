"""Tests for the surviving reindex path: `reindex_on_save()` (U10, R14).

The commit-triggered and merge-triggered reindex functions this file used
to cover were retired as part of the gortex integration rework (R4): a
configured gortex's own continuous watcher is the sole reindex trigger
once a repo is tracked, so josu no longer drives reindexing on either
event itself. `reindex_on_save()` remains -- it already has zero
production callers (a separate, pre-existing gap this change doesn't
touch) -- and is still tested here against a real running daemon backed by
a fixture HTTP server standing in for gortex, matching this repo's
integration-first testing convention.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
import uvicorn

from josu.config import JosuConfig
from josu.config.chains import ChainsConfig
from josu.config.delegate import DelegateConfig
from josu.config.orchestrator import OrchestratorConfig
from josu.daemon import create_app
from josu.daemon_auth import resolve_daemon_token
from josu.graph.gortex_process import GortexProcess
from josu.graph.index import reindex_on_save
from tests.conftest import free_port as _free_port


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo seeded with two source files, mirroring
    `tests/orchestrator/conftest.py`'s `git_repo` fixture (not reused
    directly -- this unit's file scope is `index.py` + `test_index.py`
    only, so the fixture is defined locally instead of introducing a new
    shared `tests/graph/conftest.py`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    (repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)
    return repo


@pytest.fixture
def fake_gortex_server():
    """A real HTTP server standing in for gortex, recording every POST it
    receives -- since the internal reindex route no longer exists on the
    daemon, these tests exercise `reindex_on_save()`'s graceful-degrade
    path (`ReindexResult.engine_error`) rather than a successful reindex."""
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
            calls.append({"path": self.path, "body": body})
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield GortexProcess(host="127.0.0.1", port=httpd.server_port), calls

    httpd.shutdown()
    thread.join()


@pytest.fixture
def config(tmp_path):
    return JosuConfig(
        path=tmp_path / "josu.toml",
        delegate=DelegateConfig(),
        chains=ChainsConfig(),
        orchestrator=OrchestratorConfig(),
    )


@pytest_asyncio.fixture
async def running_daemon(tmp_path, git_repo, config, fake_gortex_server):
    gortex_process, calls = fake_gortex_server
    port = _free_port()
    app = create_app(target=git_repo, config=config, gortex_process=gortex_process)
    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    token = resolve_daemon_token(config.path)
    yield f"http://127.0.0.1:{port}", token, calls
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_save_event_degrades_gracefully_since_the_internal_route_is_gone(
    running_daemon, git_repo
):
    """`reindex_on_save()` still computes the right bounded change set (the
    one saved file) and still calls `post_graph_internal_reindex()` -- but
    since the daemon no longer serves `/graph/internal/reindex` at all
    (U5's dead-route removal), the call reports via `ReindexResult.
    engine_error` rather than succeeding. This proves the graceful-degrade
    path, not a successful reindex -- there is no successful path left for
    this function today."""
    base_url, token, calls = running_daemon

    (git_repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n\n\n"
        "def saved_addition():\n    pass\n",
        encoding="utf-8",
    )

    result = await reindex_on_save(git_repo, git_repo / "main.py", base_url=base_url, token=token)

    assert result.reindexed_files == [str((git_repo / "main.py").resolve())]
    assert result.engine_error is not None
    # The route no longer exists -- nothing reaches gortex's own fixture
    # server's /v1/tools/* surface at all.
    assert calls == []


@pytest.mark.asyncio
async def test_reindex_on_save_degrades_gracefully_when_daemon_unreachable(git_repo):
    """R13-style posture: a reindex trigger whose daemon call fails must
    report the failure via `ReindexResult.engine_error`, never raise and
    break the triggering save event."""
    result = await reindex_on_save(
        git_repo, git_repo / "helper.py", base_url="http://127.0.0.1:1", token="irrelevant"
    )

    assert result.engine_error is not None
    assert result.reindexed_files == [str((git_repo / "helper.py").resolve())]
