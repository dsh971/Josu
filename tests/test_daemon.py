"""End-to-end test for the daemon: a real uvicorn server serving the graph
MCP server over actual HTTP/SSE, connected to via the MCP SDK's own SSE
client -- not mocked, this is the real transport path Claude Code will use.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

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


@pytest_asyncio.fixture
async def running_daemon(tmp_path, fixture_repo):
    # Build the graph before the server starts, mirroring how josu init
    # would build it once before the daemon comes up.
    from josu.graph.build import GraphifyEngine

    graph_out = tmp_path / "graphify-out"
    GraphifyEngine(out_dir=graph_out).build(fixture_repo)

    port = _free_port()
    app = create_app(graph_out)
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


def test_create_app_builds_graph_when_missing(tmp_path, fixture_repo):
    graph_out = tmp_path / "graphify-out"
    assert not graph_out.exists()

    create_app(graph_out, target=fixture_repo)

    assert (graph_out / "graph.json").exists()
