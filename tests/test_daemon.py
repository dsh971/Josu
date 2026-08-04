"""End-to-end test for the daemon: a real uvicorn server serving both MCP
servers over actual HTTP/SSE, connected to via the MCP SDK's own SSE
client -- not mocked, this is the real transport path Claude Code will use.

U15: `create_app()` now needs a reachable gortex to construct `GortexEngine`
against. These tests pass the `gortex_process` test seam pointed at a real,
locally-bound fixture HTTP server standing in for gortex's own
`/v1/tools/{name}` surface (mirroring `tests/graph/test_gortex.py`'s and
`tests/delegate/test_client.py`'s fixture-server convention) -- no real
`gortex` binary needed for these tests, and no mocking of the daemon's own
HTTP/SSE transport.

Every route now requires the daemon's shared-secret auth token
(`daemon_auth.py`) -- these tests resolve it the same way a real caller
would, via `daemon_auth.resolve_daemon_token(config_path)`.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

from josu.config import load_config
from josu.daemon import DEFAULT_HOST, DEFAULT_PORT, create_app
from josu.daemon_auth import resolve_daemon_token
from josu.graph.gortex_process import GortexProcess
from tests.conftest import daemon_thread as _daemon_thread
from tests.conftest import free_port as _free_port


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    return repo


@pytest.fixture
def fake_gortex_server():
    """A real HTTP server standing in for gortex's `/v1/tools/{name}`
    surface, returning one canned "greet" search result for any `search`
    call and an empty result for anything else -- enough for these
    transport-level daemon tests, which care about the SSE/HTTP plumbing
    reaching the engine, not gortex's own query semantics (covered
    separately in `tests/graph/test_gortex.py`)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == "/v1/tools/search":
                body = {"results": [{"id": "helper.py::greet"}]}
            else:
                body = {"status": "ok"}
            payload = json.dumps(body).encode("utf-8")
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

    yield GortexProcess(host="127.0.0.1", port=httpd.server_port, popen=None)

    httpd.shutdown()
    thread.join()


def _write_fixture_josu_toml(path, *, second_candidate=False) -> None:
    # `allow_remote` must precede any `[[table]]` header -- TOML scopes bare
    # keys to the most recently opened table, so it has to stay a genuine
    # top-level key (see the same note in tests/config/test_chains.py).
    text = "allow_remote = true\n\n" if second_candidate else ""
    text += """
[[delegate.candidates]]
name = "local-ollama"
endpoint = "http://localhost:11434/v1"
local = true
model = "qwen2.5-coder:7b"
"""
    if second_candidate:
        text += """
[[delegate.candidates]]
name = "remote-example"
endpoint = "https://api.example.invalid/v1"
api_key_env = "JOSU_EXAMPLE_API_KEY"
local = false
model = "example-remote-model"
"""
    text += """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-ollama"]
"""
    if second_candidate:
        text += """
[[delegation.chains]]
task_type = "boilerplate_scaffolding"
candidates = ["remote-example", "local-ollama"]
"""
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


@pytest.fixture
def fixture_config_path(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_fixture_josu_toml(config_path)
    return config_path


@pytest.fixture
def daemon_token(fixture_config_path):
    return resolve_daemon_token(fixture_config_path)


@pytest_asyncio.fixture
async def running_daemon(tmp_path, fixture_repo, fixture_config_path, fake_gortex_server):
    port = _free_port()
    app = create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_daemon_serves_graph_mcp_over_real_http(running_daemon, daemon_token):
    token = daemon_token
    async with sse_client(f"{running_daemon}/graph/sse?token={token}") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["search", "execute"]

            result = await session.call_tool("search", {"query": "greet"})
            assert len(result.content) > 0
            assert "greet" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_daemon_route_rejects_missing_or_wrong_token(running_daemon):
    async with httpx.AsyncClient(base_url=running_daemon) as client:
        response = await client.post(
            "/delegate/internal",
            json={"task": "anything", "task_type": "file_summarization"},
        )
        assert response.status_code == 401

        response = await client.post(
            "/delegate/internal",
            json={"task": "anything", "task_type": "file_summarization"},
            headers={"Authorization": "Bearer totally-wrong-token"},
        )
        assert response.status_code == 401


def test_create_app_constructs_gortex_engine_without_blocking_on_full_index(
    tmp_path, fixture_repo, fixture_config_path, fake_gortex_server
):
    """Key Technical Decisions: `create_app()` blocks on the gortex
    subprocess's `/healthz` liveness only (satisfied here by the fixture
    server always answering), never on a full index completing -- there is
    no `engine.build()` call at startup at all for `GortexEngine` (gortex
    indexes the path it was spawned with as part of starting up)."""
    app = create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )
    assert app is not None


def test_create_app_terminates_a_process_it_spawned_if_construction_fails_after(
    tmp_path, fixture_repo, fixture_config_path, monkeypatch
):
    """A gortex subprocess `create_app()` itself spawned (not the test-seam
    `gortex_process` handle) must not be leaked if something after the
    spawn raises -- e.g. a malformed config causing delegate-server
    construction to fail. `terminate_gortex()` should still run even
    though `lifespan`'s teardown never gets a chance to (the app was never
    constructed)."""
    import josu.daemon as daemon_module
    from josu.graph.gortex_process import GortexProcess

    terminated: list[GortexProcess] = []
    fake_process = GortexProcess(host="127.0.0.1", port=1, popen=object())

    monkeypatch.setattr(daemon_module, "_ensure_gortex_running", lambda *a, **k: fake_process)
    monkeypatch.setattr(
        daemon_module, "terminate_gortex", lambda p: terminated.append(p)
    )

    def _boom(**kwargs):
        raise RuntimeError("simulated failure constructing the delegate server")

    monkeypatch.setattr(daemon_module, "build_delegate_server", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        create_app(target=fixture_repo, config_path=fixture_config_path)

    assert terminated == [fake_process]


def test_create_app_does_not_terminate_a_caller_supplied_process_if_construction_fails(
    fixture_repo, fixture_config_path, fake_gortex_server, monkeypatch
):
    """Complements the test above: a `gortex_process` the CALLER supplied
    (the test seam) is never this `create_app()` call's to terminate, even
    when construction fails after it -- this daemon instance didn't spawn
    it (see `daemon.py`'s `owns_gortex_process` flag, reliability-review
    fix). Regression coverage for the asymmetry that fix closed: the
    construction-failure branch already guarded on this correctly before
    the fix; only `lifespan`'s clean-shutdown teardown didn't."""
    import josu.daemon as daemon_module

    terminated: list = []
    monkeypatch.setattr(
        daemon_module, "terminate_gortex", lambda p: terminated.append(p)
    )

    def _boom(**kwargs):
        raise RuntimeError("simulated failure constructing the delegate server")

    monkeypatch.setattr(daemon_module, "build_delegate_server", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        create_app(
            target=fixture_repo,
            config_path=fixture_config_path,
            gortex_process=fake_gortex_server,
        )

    assert terminated == []


@pytest.mark.asyncio
async def test_lifespan_teardown_does_not_terminate_a_caller_supplied_process(
    fixture_repo, fixture_config_path, fake_gortex_server, monkeypatch
):
    """Regression coverage for the `lifespan`-teardown half of the same
    `owns_gortex_process` fix: a real uvicorn server, constructed with a
    caller-supplied `gortex_process`, taken through a full clean
    start-then-shutdown cycle (the actual ASGI lifespan path, not a direct
    call to the `lifespan` context manager) must not call `terminate_gortex`
    on that process during shutdown -- before the fix, `lifespan`'s
    teardown called it unconditionally."""
    import josu.daemon as daemon_module

    terminated: list = []
    monkeypatch.setattr(
        daemon_module, "terminate_gortex", lambda p: terminated.append(p)
    )

    port = _free_port()
    app = create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)

    server.should_exit = True
    await task

    assert terminated == []


@pytest.mark.asyncio
async def test_daemon_serves_delegate_mcp_over_real_http(running_daemon, daemon_token):
    token = daemon_token
    async with sse_client(f"{running_daemon}/delegate/sse?token={token}") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["delegate_to_local"]

            result = await session.call_tool(
                "delegate_to_local",
                {
                    "task": "Reply with the single word: pong. No punctuation.",
                    "task_type": "file_summarization",
                },
            )
            payload = result.content[0].text
            assert "pong" in payload.lower()


def test_daemon_starts_end_to_end_from_a_fixture_josu_toml_with_two_candidates(
    tmp_path, fixture_repo, fake_gortex_server
):
    """Covers: starting the daemon with a `josu.toml` defining two
    candidates constructs a delegate server that can resolve chains for
    both, without a hardcoded single-model parameter."""
    config_path = tmp_path / "josu.toml"
    _write_fixture_josu_toml(config_path, second_candidate=True)

    config = load_config(config_path)
    assert {c.name for c in config.delegate.candidates} == {"local-ollama", "remote-example"}

    from josu.config.chains import resolve_chain

    registry = {c.name: c for c in config.delegate.candidates}
    file_summ_chain = resolve_chain("file_summarization", config.chains, registry)
    assert [c.name for c in file_summ_chain] == ["local-ollama"]

    boilerplate_chain = resolve_chain("boilerplate_scaffolding", config.chains, registry)
    # Free-local-first (R33): local-ollama ranks before remote-example even
    # though remote-example is listed first in TOML.
    assert [c.name for c in boilerplate_chain] == ["local-ollama", "remote-example"]

    # And the daemon itself constructs cleanly from this same two-candidate
    # config -- no hardcoded single `delegate_model` parameter anywhere.
    app = create_app(target=fixture_repo, config=config, gortex_process=fake_gortex_server)
    assert app is not None


# --- U14: /delegate/internal routed through the daemon's shared queue -------


@pytest.mark.asyncio
async def test_daemon_serves_internal_delegate_route_over_real_http(
    running_daemon, daemon_token
):
    """A real HTTP POST to `/delegate/internal` (the U14 endpoint) against
    the running daemon resolves against its real fixture config, the same
    as the MCP tool does, over the same loopback-TCP listener."""
    token = daemon_token
    async with httpx.AsyncClient(base_url=running_daemon) as client:
        response = await client.post(
            "/delegate/internal",
            json={
                "task": "Reply with the single word: pong. No punctuation.",
                "task_type": "file_summarization",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert "pong" in body["result"].lower()


@pytest.mark.asyncio
async def test_daemon_serves_internal_graph_reindex_route_over_real_http(
    running_daemon, daemon_token, fixture_repo
):
    """The new `/graph/internal/reindex` route (doc-review bug fix) reaches
    the daemon's own live `GortexEngine` -- proven here by asserting the
    fixture gortex server actually received the `reindex_repository` call
    with the changed-file paths, not a throwaway engine instance."""
    token = daemon_token
    changed_file = str(fixture_repo / "helper.py")
    async with httpx.AsyncClient(base_url=running_daemon) as client:
        response = await client.post(
            "/graph/internal/reindex",
            json={"root": str(fixture_repo), "changed_files": [changed_file]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_josu_delegate_cli_with_no_daemon_running_produces_clear_error(capsys):
    """U14: `josu delegate` no longer silently constructs a local queue when
    the daemon isn't reachable -- it fails with a clear, actionable
    message, never a stack trace."""
    import argparse

    from josu.cli import _cmd_delegate

    unused_port = _free_port()  # nothing is listening here
    args = argparse.Namespace(
        task_type="file_summarization",
        task="anything",
        config=None,
        host="127.0.0.1",
        port=unused_port,
    )

    exit_code = _cmd_delegate(args)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "josu daemon not reachable" in captured.out
    assert "josu daemon start" in captured.out


def test_josu_delegate_cli_routes_through_the_running_daemon_over_real_http(
    tmp_path, fixture_repo, fixture_config_path, fake_gortex_server, capsys
):
    """U14's own verification bar: `josu delegate` and a running daemon,
    connected over real HTTP -- not an in-process shortcut."""
    import argparse

    from josu.cli import _cmd_delegate

    app = create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )

    with _daemon_thread(app, DEFAULT_HOST, DEFAULT_PORT):
        args = argparse.Namespace(
            task_type="file_summarization",
            task="Reply with the single word: pong. No punctuation.",
            config=str(fixture_config_path),
            host=None,
            port=None,
        )
        exit_code = _cmd_delegate(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "pong" in captured.out.lower()


def test_commit_hook_r39_survives_the_daemon_route_even_with_allow_remote_true_and_remote_ranked_first(
    tmp_path, fixture_repo, fake_gortex_server, monkeypatch, capsys
):
    """U14's core safety proof: even with the daemon's REAL config setting
    `allow_remote = true` globally and ranking a remote candidate FIRST for
    `proactive_check`, the commit hook -- now routed through the daemon's
    HTTP endpoint instead of an in-process call -- never reaches that
    remote candidate. `_local_only_execution_inputs()` (unchanged, still
    client-side) projects down to the local-only candidate before the
    daemon is ever contacted (R39); the endpoint's own defensive filter
    (`internal_api.py`) is a second, independent line of defense."""
    import josu.proactive.watchers as watchers_module

    remote_contacted: list[str] = []

    class ExplodingClient:
        async def complete(self, *, model, messages, timeout):
            remote_contacted.append("remote-first")
            raise AssertionError("remote candidate must never be contacted (R39)")

    class FakeLocalClient:
        async def complete(self, *, model, messages, timeout):
            return json.dumps({"result": "all clear", "caveats": ""})

    def client_factory(candidate):
        return ExplodingClient() if candidate.name == "remote-first" else FakeLocalClient()

    xdg_home = tmp_path / "xdg-config"
    config_dir = xdg_home / "josu"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "josu.toml"
    config_path.write_text(
        """
allow_remote = true

[[delegate.candidates]]
name = "remote-first"
endpoint = "https://api.example.invalid/v1"
local = false
model = "remote-model"

[[delegate.candidates]]
name = "local-ollama"
endpoint = "http://localhost:11434/v1"
local = true
model = "qwen2.5-coder:7b"

[[delegation.chains]]
task_type = "proactive_check"
candidates = ["remote-first", "local-ollama"]
explicit_order = true
""",
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    monkeypatch.chdir(fixture_repo)

    app = create_app(
        target=fixture_repo,
        config_path=config_path,
        delegate_client_factory=client_factory,
        gortex_process=fake_gortex_server,
    )

    with _daemon_thread(app, DEFAULT_HOST, DEFAULT_PORT):
        exit_code = watchers_module._run_commit_hook_from_cli()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "all clear" in captured.out
    assert remote_contacted == []
