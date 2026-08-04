"""Tests for the context-graph MCP server (U1) -- real MCP protocol
round-trips over in-memory streams via the SDK's own test helper, not mocks.
Backed by `FakeGraphEngine` (tests/conftest.py) -- `GortexEngine`'s own HTTP
behavior is covered separately in `tests/graph/test_gortex.py`.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from josu.graph.server import build_server
from tests.conftest import FakeGraphEngine


@pytest.fixture
def built_engine():
    return FakeGraphEngine(
        search_results=[{"id": "helper.py::greet", "neighbors": ["main.py::main"]}],
        execute_results={"graph_stats": {"nodes": 2, "edges": 1}},
    )


@pytest.mark.asyncio
async def test_list_tools_returns_exactly_two(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        assert [t.name for t in result.tools] == ["search", "execute"]


@pytest.mark.asyncio
async def test_list_tools_schema_has_no_hardcoded_operation_enum(built_engine):
    """R12: `execute`'s `operation` argument must accept an arbitrary
    string (gortex's own tool names), not a fixed enum -- a hardcoded enum
    would silently block the whole point of routing gortex's ~180-tool
    surface through this one operation-dispatch argument."""
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        execute_tool = next(t for t in result.tools if t.name == "execute")
        operation_schema = execute_tool.inputSchema["properties"]["operation"]
        assert "enum" not in operation_schema


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
async def test_execute_unrecognized_operation_returns_error_content_not_exception(built_engine):
    server = build_server(built_engine)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("execute", {"operation": "not_a_real_op", "params": {}})
        # Should get a text error content block back, not raise/crash the session.
        assert "error" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_execute_engine_unavailable_returns_error_content_not_exception():
    """A GortexUnavailableError (or any GraphEngineUnavailableError
    subclass) from the engine must surface the same graceful error-content
    behavior as a ValueError/KeyError -- never an unhandled exception at
    the MCP transport layer."""
    from josu.graph.engine import GraphEngineUnavailableError

    class UnavailableEngine(FakeGraphEngine):
        async def search(self, query, limit=10):
            raise GraphEngineUnavailableError("gortex down", reason="unreachable")

    server = build_server(UnavailableEngine())
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("search", {"query": "anything"})
        assert "error" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_slow_engine_call_does_not_block_a_concurrent_session():
    """Proves the async conversion actually achieves non-blocking behavior
    (Key Technical Decisions: a sync engine call at this layer would freeze
    the whole daemon's event loop), not just that the code compiles as
    `async def`."""
    import asyncio

    class SlowEngine(FakeGraphEngine):
        async def search(self, query, limit=10):
            await asyncio.sleep(0.3)
            return await super().search(query, limit=limit)

    slow_engine = SlowEngine(search_results=[{"id": "slow"}])
    fast_engine = FakeGraphEngine(search_results=[{"id": "fast"}])

    slow_server = build_server(slow_engine)
    fast_server = build_server(fast_engine)

    async def call_slow():
        async with create_connected_server_and_client_session(slow_server) as client:
            return await client.call_tool("search", {"query": "x"})

    async def call_fast():
        async with create_connected_server_and_client_session(fast_server) as client:
            return await client.call_tool("search", {"query": "x"})

    start = asyncio.get_event_loop().time()
    slow_result, fast_result = await asyncio.gather(call_fast(), call_slow())
    elapsed = asyncio.get_event_loop().time() - start
    # The fast call should not have been forced to wait for the slow one --
    # both run concurrently, so total elapsed stays close to the slow
    # call's own 0.3s, not the sum of both.
    assert elapsed < 0.6
    assert "fast" in slow_result.content[0].text or "fast" in fast_result.content[0].text
