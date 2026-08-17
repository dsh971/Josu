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
from contextlib import asynccontextmanager
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
        def do_GET(self):
            # `check_gortex_reachable()`'s `/healthz` probe -- a real gortex
            # answers GET here; without this, any test that goes through
            # the real `_resolve_graph_engine_target()` path (as opposed to
            # the `gortex_process=` test seam, which never calls it) would
            # get a 501 from BaseHTTPRequestHandler's default and silently
            # degrade to "unreachable" regardless of what it meant to test.
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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

    yield GortexProcess(host="127.0.0.1", port=httpd.server_port)

    httpd.shutdown()
    thread.join()


def _force_no_local_gortex_binary(monkeypatch) -> None:
    """Make `check_gortex_version_compatible()` take its "binary absent"
    degrade path regardless of whether this machine happens to have a
    real `gortex` on PATH -- several tests below target a loopback host,
    so without this their pass/fail would depend on whatever `gortex`
    version is actually installed locally (confirmed: this dev machine
    has v0.62.0, exactly at the compatibility floor, silently masking
    what should be a deterministic, environment-independent test)."""

    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _raise_not_found)


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


@asynccontextmanager
async def _running_app(app):
    """Start a real uvicorn server for `app` and yield its base URL,
    tearing the server down on exit -- the shared plumbing behind both
    the `running_daemon` fixture (built from the shared fixtures below)
    and `_run_app_and_search` (drives one MCP call against a custom,
    per-test app/config)."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest_asyncio.fixture
async def running_daemon(tmp_path, fixture_repo, fixture_config_path, fake_gortex_server):
    app = create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )
    async with _running_app(app) as base_url:
        yield base_url


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
    """`create_app()` blocks on the configured target's `/healthz`
    liveness only (satisfied here by the fixture server always answering),
    never on a full index completing -- there is no `engine.build()` call
    at startup at all for `GortexEngine` (build/update are no-ops now;
    tracking is the user's own `gortex track` setup step)."""
    app = create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )
    assert app is not None


def test_create_app_prints_no_warning_for_a_correctly_permissioned_config(
    tmp_path, fixture_repo, fixture_config_path, fake_gortex_server, capsys
):
    """A `josu.toml` with correct (0o600) permissions and no other
    warnings produces no warning output."""
    create_app(
        target=fixture_repo, config_path=fixture_config_path, gortex_process=fake_gortex_server
    )
    assert "warning:" not in capsys.readouterr().out


def test_create_app_prints_permission_warning_for_a_group_world_readable_config(
    tmp_path, fixture_repo, fake_gortex_server, capsys
):
    """`config.warnings` (e.g. the group/world-readable permission
    warning) previously had no code path reading it anywhere -- this
    confirms it now actually reaches stdout at daemon startup, not just
    that `create_app()`'s return value is unaffected."""
    config_path = tmp_path / "josu.toml"
    _write_fixture_josu_toml(config_path)
    os.chmod(config_path, 0o644)

    create_app(target=fixture_repo, config_path=config_path, gortex_process=fake_gortex_server)

    out = capsys.readouterr().out
    assert "josu daemon: warning:" in out
    assert "group/world-accessible" in out


def test_create_app_resolves_a_configured_and_reachable_target_from_config(
    fixture_repo, fixture_config_path, fake_gortex_server, monkeypatch
):
    """The real (non-test-seam) path: a `[[graph.engines]]` entry pointing
    at a reachable target resolves and constructs cleanly, with no
    `gortex_process=` override. Verified via a real MCP `search` call
    returning the fixture server's canned result -- `app is not None`
    alone would pass even if resolution silently failed and the engine
    ended up unset, which is exactly what happened here before the
    `fake_gortex_server` fixture grew a `/healthz` handler."""
    _force_no_local_gortex_binary(monkeypatch)
    text = fixture_config_path.read_text(encoding="utf-8")
    text += f"""
[[graph.engines]]
name = "gortex"
host = "{fake_gortex_server.host}"
port = {fake_gortex_server.port}
"""
    fixture_config_path.write_text(text, encoding="utf-8")

    app = create_app(target=fixture_repo, config_path=fixture_config_path)
    assert app is not None
    token = resolve_daemon_token(fixture_config_path)
    result_text = asyncio.run(_run_app_and_search(app, token, "greet"))
    assert "helper.py::greet" in result_text


def test_create_app_starts_with_no_engine_when_configured_target_is_unreachable(
    fixture_repo, fixture_config_path
):
    """An unreachable configured target degrades to no graph engine --
    `create_app()` must still succeed (R1), not raise -- and a real MCP
    caller sees the "unreachable" reason, not a silently-working engine."""
    text = fixture_config_path.read_text(encoding="utf-8")
    text += """
[[graph.engines]]
name = "gortex"
host = "127.0.0.1"
port = 1
"""
    fixture_config_path.write_text(text, encoding="utf-8")

    app = create_app(target=fixture_repo, config_path=fixture_config_path)
    assert app is not None
    token = resolve_daemon_token(fixture_config_path)
    result_text = asyncio.run(_run_app_and_search(app, token))
    assert "not reachable" in result_text


def test_create_app_starts_with_no_engine_when_nothing_is_configured(
    fixture_repo, fixture_config_path
):
    """No `[[graph.engines]]` section at all is the valid, expected
    "no engine configured" state (R7) -- `create_app()` still succeeds,
    and a real MCP caller gets the "unconfigured" reason."""
    app = create_app(target=fixture_repo, config_path=fixture_config_path)
    assert app is not None
    token = resolve_daemon_token(fixture_config_path)
    result_text = asyncio.run(_run_app_and_search(app, token))
    assert "no graph-engine target is configured" in result_text


async def _search_via_mcp(base_url: str, token: str, query: str = "anything"):
    async with sse_client(f"{base_url}/graph/sse?token={token}") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool("search", {"query": query})


async def _run_app_and_search(app, token: str, query: str = "anything"):
    """Drive one real MCP `search` call over SSE against `app` and return
    the resulting text -- proving actual engine-availability behavior
    end-to-end (not just that `create_app()` didn't raise)."""
    async with _running_app(app) as base_url:
        result = await _search_via_mcp(base_url, token, query)
        return result.content[0].text


def test_create_app_starts_with_no_engine_when_version_incompatible(
    fixture_repo, fixture_config_path, fake_gortex_server, monkeypatch
):
    """A configured, reachable target whose local `gortex` binary reports
    a version below the compatibility floor degrades to no graph engine --
    `create_app()` succeeds (R1), and the specific "version-incompatible"
    reason (not the generic "unconfigured" fallback) reaches a real MCP
    caller, proving both the degrade-branch wiring and the reason-
    threading fix work end-to-end."""
    text = fixture_config_path.read_text(encoding="utf-8")
    text += f"""
[[graph.engines]]
name = "gortex"
host = "{fake_gortex_server.host}"
port = {fake_gortex_server.port}
"""
    fixture_config_path.write_text(text, encoding="utf-8")

    def _fake_run(*args, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="gortex v0.10.0+deadbeef\n", stderr=""
        )

    monkeypatch.setattr("josu.graph.gortex_process.subprocess.run", _fake_run)

    app = create_app(target=fixture_repo, config_path=fixture_config_path)
    assert app is not None
    token = resolve_daemon_token(fixture_config_path)
    result_text = asyncio.run(_run_app_and_search(app, token))
    assert "incompatible version" in result_text


def test_create_app_starts_with_no_engine_when_tool_surface_incapable(
    fixture_repo, fixture_config_path, monkeypatch
):
    """A configured, reachable target that responds to the capability
    probe with `tool_blocked_by_mode` (started with the wrong `--tools`
    preset) degrades to no graph engine -- and the "incapable" reason
    reaches a real MCP caller, not the generic fallback."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": '{"error_code":"tool_blocked_by_mode"}',
                    }
                ],
            }
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
    try:
        text = fixture_config_path.read_text(encoding="utf-8")
        text += f"""
[[graph.engines]]
name = "gortex"
host = "127.0.0.1"
port = {httpd.server_port}
"""
        fixture_config_path.write_text(text, encoding="utf-8")
        _force_no_local_gortex_binary(monkeypatch)

        app = create_app(target=fixture_repo, config_path=fixture_config_path)
        assert app is not None
        token = resolve_daemon_token(fixture_config_path)
        result_text = asyncio.run(_run_app_and_search(app, token))
        assert "tool preset" in result_text
    finally:
        httpd.shutdown()
        thread.join()


def test_create_app_resolves_auth_token_from_api_key_env_end_to_end(
    fixture_repo, fixture_config_path, monkeypatch
):
    """The full chain, end-to-end through the real (non-test-seam) config-
    loading path: a `[[graph.engines]]` entry's `api_key_env` names an
    environment variable -> `_resolve_graph_engine_target()` resolves it
    via `os.environ.get()` -> `GortexEngine` sends it as `Authorization:
    Bearer <token>` on both the `/healthz` reachability probe and the real
    `/v1/tools/search` call. Each link is unit-tested elsewhere (config
    parsing in test_graph_engines.py, header-sending given a token in
    test_gortex.py) but nothing previously proved the daemon actually
    wires them together starting from a real TOML file and a real env
    var."""
    monkeypatch.setenv("JOSU_TEST_GORTEX_TOKEN", "end-to-end-secret-value")
    _force_no_local_gortex_binary(monkeypatch)

    seen_headers: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen_headers["healthz"] = dict(self.headers)
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            seen_headers[self.path] = dict(self.headers)
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
    try:
        text = fixture_config_path.read_text(encoding="utf-8")
        text += f"""
[[graph.engines]]
name = "gortex"
host = "127.0.0.1"
port = {httpd.server_port}
api_key_env = "JOSU_TEST_GORTEX_TOKEN"
"""
        fixture_config_path.write_text(text, encoding="utf-8")

        app = create_app(target=fixture_repo, config_path=fixture_config_path)
        assert app is not None

        # The reachability probe already happened synchronously inside
        # create_app() -- assert it carried the token too (this is the
        # fix from the earlier skeptical review: check_gortex_reachable()
        # used to be called before auth_token was even resolved).
        assert (
            seen_headers.get("healthz", {}).get("Authorization")
            == "Bearer end-to-end-secret-value"
        )

        token = resolve_daemon_token(fixture_config_path)
        result_text = asyncio.run(_run_app_and_search(app, token, "greet"))
        assert "helper.py::greet" in result_text
        assert (
            seen_headers.get("/v1/tools/search", {}).get("Authorization")
            == "Bearer end-to-end-secret-value"
        )
    finally:
        httpd.shutdown()
        thread.join()


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


def _write_dead_candidate_josu_toml(path, *, failure_threshold=1, cooldown_seconds=300.0) -> None:
    """A single candidate pointed at `127.0.0.1:1` -- a port that refuses
    the connection near-instantly on any dev/CI machine, giving a fast,
    deterministic `DelegateUnreachableError` with no real network hang.
    `model` deliberately isn't in `CURATED_MODELS`, so `preflight_check`
    trivially passes and the failure genuinely comes from the connection
    attempt, matching `test_chain.py`'s `_candidate()` convention."""
    path.write_text(
        f"""
[delegate]
failure_threshold = {failure_threshold}
cooldown_seconds = {cooldown_seconds}

[[delegate.candidates]]
name = "dead-candidate"
endpoint = "http://127.0.0.1:1/v1"
local = true
model = "test-dead-model"

[[delegation.chains]]
task_type = "file_summarization"
candidates = ["dead-candidate"]
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


@pytest.mark.asyncio
async def test_cooldown_store_shared_between_mcp_tool_and_internal_route(
    tmp_path, fixture_repo, fake_gortex_server
):
    """U4 (feat/delegate-candidate-circuit-breaker plan): a candidate
    tripped into cooldown via the MCP tool path is skipped -- without a
    second connection attempt -- via the internal HTTP route, proving
    `daemon.py`'s `create_app()` actually shares ONE `CandidateCooldownStore`
    between `build_delegate_server()` and `build_delegate_internal_route()`,
    not two independently constructed ones. Complements U3's unit-level
    coverage of the same requirement (R6) at the daemon-wiring level."""
    config_path = tmp_path / "josu.toml"
    _write_dead_candidate_josu_toml(config_path)
    token = resolve_daemon_token(config_path)

    port = _free_port()
    app = create_app(target=fixture_repo, config_path=config_path, gortex_process=fake_gortex_server)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    base_url = f"http://127.0.0.1:{port}"

    try:
        # First attempt, via the MCP tool: the only candidate is dead, so
        # this trips cooldown (failure_threshold=1) and reports chain
        # exhaustion.
        async with sse_client(f"{base_url}/delegate/sse?token={token}") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "delegate_to_local",
                    {"task": "anything", "task_type": "file_summarization"},
                )
        assert "exhausted" in result.content[0].text.lower()

        # Second attempt, via the internal HTTP route: if the cooldown store
        # were NOT actually shared, this would independently attempt
        # "dead-candidate" again (and eventually also fail via its own
        # first failure). Instead, it must already see the candidate as
        # cooled down -- also a 502 chain-exhausted response, but now
        # without a fresh connection attempt being the cause.
        async with httpx.AsyncClient(base_url=base_url) as client:
            response = await client.post(
                "/delegate/internal",
                json={"task": "anything", "task_type": "file_summarization"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 502
        body = response.json()
        assert body["error"] == "chain_exhausted"
        assert "CandidateCooldownError" in body["detail"] or "cooldown" in body["detail"].lower()
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_daemon_no_longer_serves_an_internal_graph_reindex_route(
    running_daemon, daemon_token, fixture_repo
):
    """The `/graph/internal/reindex` route was retired (gortex integration
    rework, U5) -- its only production callers (josu's own commit/merge
    reindex triggers) were retired alongside it (R4), and its handler
    called a now-no-op `engine.update()`. Confirms the route is genuinely
    gone, not silently left wired but broken."""
    token = daemon_token
    changed_file = str(fixture_repo / "helper.py")
    async with httpx.AsyncClient(base_url=running_daemon) as client:
        response = await client.post(
            "/graph/internal/reindex",
            json={"root": str(fixture_repo), "changed_files": [changed_file]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 404


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


def test_cmd_delegate_prints_no_config_warning_even_when_misconfigured(
    tmp_path, fixture_repo, fake_gortex_server, capsys
):
    """feat/cli-ease-of-use plan (U4): `_cmd_delegate` deliberately never
    loads config in-process (it only resolves `config_path` to find the
    daemon's shared-secret token file, per `_cmd_delegate`'s own docstring)
    -- so it must never print a `config.warnings` entry itself, even
    against a misconfigured (group/world-readable) `josu.toml`, unlike
    `_cmd_run`/`create_app()`. The daemon's own startup warning (from
    `create_app()`, sharing this test's stdout since the daemon runs
    in-process on a background thread) is drained before the delegate call
    so only `_cmd_delegate`'s own output is asserted against."""
    import argparse

    from josu.cli import _cmd_delegate

    config_path = tmp_path / "josu.toml"
    _write_fixture_josu_toml(config_path)
    os.chmod(config_path, 0o644)

    app = create_app(
        target=fixture_repo, config_path=config_path, gortex_process=fake_gortex_server
    )
    capsys.readouterr()  # drain create_app()'s own startup warning print

    with _daemon_thread(app, DEFAULT_HOST, DEFAULT_PORT):
        args = argparse.Namespace(
            task_type="file_summarization",
            task="Reply with the single word: pong. No punctuation.",
            config=str(config_path),
            host=None,
            port=None,
        )
        exit_code = _cmd_delegate(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "warning:" not in captured.out


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
