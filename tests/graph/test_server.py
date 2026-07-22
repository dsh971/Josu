"""Tests for the context-graph MCP server (U1) -- real MCP protocol
round-trips over in-memory streams via the SDK's own test helper, not mocks.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from josu.graph.build import GraphifyEngine
from josu.graph.server import build_server


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    (repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def built_engine(tmp_path, fixture_repo):
    engine = GraphifyEngine(out_dir=tmp_path / "graphify-out")
    engine.build(fixture_repo)
    return engine


@pytest.mark.asyncio
async def test_list_tools_returns_exactly_two(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        assert [t.name for t in result.tools] == ["search", "execute"]


@pytest.mark.asyncio
async def test_list_tools_schema_is_fixed_regardless_of_graph_size(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        # Schema shape stays the same two tools even though the underlying
        # graph has many nodes -- the whole point of R7/R12.
        assert len(result.tools) == 2


@pytest.mark.asyncio
async def test_search_tool_call_returns_matching_node(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("search", {"query": "greet"})
        payload = json.loads(result.content[0].text)
        assert len(payload) > 0
        assert any("greet" in str(node).lower() for node in payload)


@pytest.mark.asyncio
async def test_execute_graph_stats(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("execute", {"operation": "graph_stats", "params": {}})
        payload = json.loads(result.content[0].text)
        assert payload["nodes"] > 0
        assert payload["edges"] > 0


@pytest.mark.asyncio
async def test_execute_malformed_operation_returns_error_content_not_exception(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("execute", {"operation": "not_a_real_op", "params": {}})
        # Should get a text error content block back, not raise/crash the session.
        assert "error" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_execute_get_node_unknown_id_returns_error_content(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "execute", {"operation": "get_node", "params": {"node_id": "nope"}}
        )
        assert "error" in result.content[0].text.lower()
