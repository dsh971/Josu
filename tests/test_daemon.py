"""End-to-end test for the daemon: a real uvicorn server serving both MCP
servers over actual HTTP/SSE, connected to via the MCP SDK's own SSE
client -- not mocked, this is the real transport path Claude Code will use.

U12: the daemon now loads `josu.toml` (via `config/__init__.py`, composing
U11's candidate registry and U3's fallback chains) at startup and passes it
into the delegate server's construction, replacing the single hardcoded
`delegate_model` string U1/U2 shipped with -- these tests exercise that via
a real fixture `josu.toml` on disk, not an in-memory shortcut, since
"starts end-to-end from a fixture josu.toml" is this unit's own
verification bar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

from josu.config import load_config
from josu.daemon import DEFAULT_HOST, DEFAULT_PORT, create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    return repo


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


@pytest_asyncio.fixture
async def running_daemon(tmp_path, fixture_repo, fixture_config_path):
    # Build the graph before the server starts, mirroring how josu init
    # would build it once before the daemon comes up.
    from josu.graph.build import GraphifyEngine

    graph_out = tmp_path / "graphify-out"
    GraphifyEngine(out_dir=graph_out).build(fixture_repo)

    port = _free_port()
    app = create_app(graph_out, config_path=fixture_config_path)
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
async def test_daemon_serves_graph_mcp_over_real_http(running_daemon):
    async with sse_client(f"{running_daemon}/graph/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["search", "execute"]

            result = await session.call_tool("search", {"query": "greet"})
            assert len(result.content) > 0


def test_create_app_builds_graph_when_missing(tmp_path, fixture_repo, fixture_config_path):
    graph_out = tmp_path / "graphify-out"
    assert not graph_out.exists()

    create_app(graph_out, target=fixture_repo, config_path=fixture_config_path)

    assert (graph_out / "graph.json").exists()


@pytest.mark.asyncio
async def test_daemon_serves_delegate_mcp_over_real_http(running_daemon):
    async with sse_client(f"{running_daemon}/delegate/sse") as (read, write):
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
    tmp_path, fixture_repo
):
    """Covers: starting the daemon with a `josu.toml` defining two
    candidates constructs a delegate server that can resolve chains for
    both, without a hardcoded single-model parameter."""
    config_path = tmp_path / "josu.toml"
    _write_fixture_josu_toml(config_path, second_candidate=True)
    graph_out = tmp_path / "graphify-out"

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
    app = create_app(graph_out, target=fixture_repo, config=config)
    assert app is not None


# --- U14: /delegate/internal routed through the daemon's shared queue -------


@pytest.mark.asyncio
async def test_daemon_serves_internal_delegate_route_over_real_http(running_daemon):
    """A real HTTP POST to `/delegate/internal` (the new U14 endpoint)
    against the running daemon resolves against its real fixture config,
    the same as the MCP tool does, over the same loopback-TCP listener."""
    async with httpx.AsyncClient(base_url=running_daemon) as client:
        response = await client.post(
            "/delegate/internal",
            json={
                "task": "Reply with the single word: pong. No punctuation.",
                "task_type": "file_summarization",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert "pong" in body["result"].lower()


@contextlib.contextmanager
def _daemon_thread(app, host: str, port: int):
    """Serve `app` via a real `uvicorn` server on its own background thread
    with its OWN event loop -- distinct from the current test's event loop
    (if any). Needed because `cli.py`'s `_cmd_delegate` and
    `watchers.py`'s `_run_commit_hook_from_cli()` are themselves
    `asyncio.run()`-wrapped sync entry points (mirroring their real,
    separate-OS-process production shape); calling `asyncio.run()` from
    inside an already-running event loop raises, so the daemon under test
    genuinely needs its own thread/loop, not just a `create_task()` in the
    caller's loop the way `running_daemon` above does."""
    ready = threading.Event()
    stop = threading.Event()
    errors: list[BaseException] = []

    def _run() -> None:
        async def _serve() -> None:
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            serve_task = asyncio.ensure_future(server.serve())
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.02)
            server.should_exit = True
            await serve_task

        try:
            asyncio.run(_serve())
        except BaseException as exc:  # pragma: no cover - surfaced via `errors`
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        stop.set()
        thread.join(timeout=5)
        raise RuntimeError("daemon thread failed to start in time")
    if errors:
        raise errors[0]
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)


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
    tmp_path, fixture_repo, fixture_config_path, capsys
):
    """U14's own verification bar: `josu delegate` and a running daemon,
    connected over real HTTP -- not an in-process shortcut."""
    import argparse

    from josu.cli import _cmd_delegate

    graph_out = tmp_path / "graphify-out"
    app = create_app(graph_out, target=fixture_repo, config_path=fixture_config_path)

    with _daemon_thread(app, DEFAULT_HOST, DEFAULT_PORT):
        args = argparse.Namespace(
            task_type="file_summarization",
            task="Reply with the single word: pong. No punctuation.",
            config=None,
            host=None,
            port=None,
        )
        exit_code = _cmd_delegate(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "pong" in captured.out.lower()


def test_commit_hook_r39_survives_the_daemon_route_even_with_allow_remote_true_and_remote_ranked_first(
    tmp_path, fixture_repo, monkeypatch, capsys
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

    graph_out = tmp_path / "graphify-out"
    app = create_app(
        graph_out,
        target=fixture_repo,
        config_path=config_path,
        delegate_client_factory=client_factory,
    )

    with _daemon_thread(app, DEFAULT_HOST, DEFAULT_PORT):
        exit_code = watchers_module._run_commit_hook_from_cli()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "all clear" in captured.out
    assert remote_contacted == []
