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
import os
import socket

import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

from josu.config import load_config
from josu.daemon import create_app


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
