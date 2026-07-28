"""Tests for the delegate-to-local MCP server (U2, U11).

The happy path exercises a real Ollama call (through the new generic
async client, wired to Ollama's OpenAI-compatible endpoint) end to end via
the MCP surface -- no mocks, matching U1's testing philosophy. Error-path
tests exercise the full `call_tool` -> `queue.run()` -> `delegate()` ->
`client.py` async path via a hand-written fake `DelegateClient`, proving
U11 kept the existing single-candidate tool fully working on the new
fully-async path with no remaining synchronous bridging.

`delegate()`-level behavior (retry semantics, graph fallback, etc.) is
covered independently in `test_local_model.py`; this module only covers the
MCP transport and tool-schema surface.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from josu.delegate.client import DelegateRateLimitedError, DelegateUnreachableError
from josu.delegate.server import build_server
from josu.graph.build import GraphifyEngine


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    return repo


@pytest.fixture
def built_engine(tmp_path, fixture_repo):
    engine = GraphifyEngine(out_dir=tmp_path / "graphify-out")
    engine.build(fixture_repo)
    return engine


class FakeGoodClient:
    """Fake `DelegateClient` returning a well-formed response immediately."""

    def __init__(self, result="ok", caveats=""):
        self._result = result
        self._caveats = caveats
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        return json.dumps({"result": self._result, "caveats": self._caveats})


class FakeUnreachableClient:
    async def complete(self, *, model, messages, timeout):
        raise DelegateUnreachableError("fake-candidate")


class FakeRateLimitedClient:
    async def complete(self, *, model, messages, timeout):
        raise DelegateRateLimitedError("fake-candidate", retry_after="12")


@pytest.mark.asyncio
async def test_list_tools_returns_exactly_one():
    server = build_server()
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        assert [t.name for t in result.tools] == ["delegate_to_local"]


@pytest.mark.asyncio
async def test_delegate_bounded_task_returns_result_and_caveats(built_engine, fixture_repo):
    server = build_server(graph_engine=built_engine)
    async with create_connected_server_and_client_session(server) as client:
        # Self-contained task -- doesn't depend on graph/file context resolving
        # to anything specific.
        result = await client.call_tool(
            "delegate_to_local",
            {
                "task": (
                    "Write a one-sentence docstring for a Python function named "
                    "'greet' that takes a name argument and returns a greeting "
                    "string like 'Hello, <name>!'."
                )
            },
        )
        payload = json.loads(result.content[0].text)
        assert payload["result"]
        assert "caveats" in payload


@pytest.mark.asyncio
async def test_delegate_tool_round_trips_on_fully_async_path_with_fake_client():
    """The full stack -- MCP `call_tool` -> `queue.run()` -> `delegate()` ->
    `client.py` -- round-trips through a fake `DelegateClient`, proving no
    synchronous bridging remains anywhere on the path (U11's verification
    goal)."""
    fake_client = FakeGoodClient(result="42", caveats="none")
    server = build_server(client=fake_client)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("delegate_to_local", {"task": "compute the answer"})
        payload = json.loads(result.content[0].text)
        assert payload["result"] == "42"
        assert payload["caveats"] == "none"
    assert fake_client.calls == 1


@pytest.mark.asyncio
async def test_delegate_tool_surfaces_unreachable_error_as_error_content():
    server = build_server(client=FakeUnreachableClient())
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("delegate_to_local", {"task": "anything"})
        text = result.content[0].text
        assert "error" in text.lower()
        assert "unreachable" in text.lower()


@pytest.mark.asyncio
async def test_delegate_tool_surfaces_rate_limited_error_as_error_content():
    server = build_server(client=FakeRateLimitedClient())
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("delegate_to_local", {"task": "anything"})
        text = result.content[0].text
        assert "error" in text.lower()
