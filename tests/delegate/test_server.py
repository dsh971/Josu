"""Tests for the delegate-to-local MCP server (U2, U11, U12).

U12 makes `call_tool` thin: resolve arguments -> `chain.execute_chain(...)`
-> map outcome to MCP content blocks. This file now covers only the
MCP-transport/tool-schema surface (list_tools shape, one real round trip,
one fake-client round trip, and exception-to-content-block mapping) --
the actual retry/chain-advance decision tree is covered independently,
without any MCP harness, in `test_chain.py`.

The happy path exercises a real Ollama call through a chain resolving to
one local candidate -- no mocks, matching U1's testing philosophy.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate.client import DelegateRateLimitedError, DelegateUnreachableError
from josu.delegate.server import build_server
from tests.conftest import FakeGraphEngine

TASK_TYPE = "file_summarization"


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    return repo


@pytest.fixture
def built_engine():
    return FakeGraphEngine()


def _single_candidate_chain(
    name: str,
    *,
    local: bool = True,
    model: str = "test-model",
    endpoint: str = "http://example.invalid",
):
    candidate = DelegateCandidate(
        name=name, endpoint=endpoint, local=local, model=model
    )
    chains_config = ChainsConfig(
        chains=[DelegationChain(task_type=TASK_TYPE, candidates=[name])],
        allow_remote=True,
    )
    return chains_config, {name: candidate}


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
async def test_list_tools_schema_requires_task_type_alongside_task():
    """U12: the tool schema gains `task_type`, naming which fallback chain
    (U3) to resolve, alongside the existing `task`/`scope`."""
    server = build_server()
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        schema = result.tools[0].inputSchema
        assert "task_type" in schema["properties"]
        assert "task" in schema["properties"]
        assert "scope" in schema["properties"]
        assert set(schema["required"]) == {"task", "task_type"}


@pytest.mark.asyncio
async def test_tool_description_includes_chain_exhausted_fallback_instruction():
    """U3: the tool description tells Claude Code to perform the sub-task
    itself on a chain-exhausted error, rather than retrying the same
    delegation or leaving it incomplete."""
    server = build_server()
    async with create_connected_server_and_client_session(server) as client:
        result = await client.list_tools()
        description = result.tools[0].description
        assert "chain-exhausted" in description
        assert "perform the sub-task yourself" in description


@pytest.mark.asyncio
async def test_delegate_bounded_task_returns_result_and_caveats(built_engine, fixture_repo):
    """Real end-to-end round trip: MCP call_tool -> chain.execute_chain ->
    queue.run_chain -> delegate() -> a real Ollama-backed local candidate."""
    chains_config, registry = _single_candidate_chain(
        "local-ollama",
        local=True,
        model="qwen2.5-coder:7b",
        endpoint="http://localhost:11434/v1",
    )
    server = build_server(graph_engine=built_engine, chains_config=chains_config, registry=registry)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "delegate_to_local",
            {
                "task": (
                    "Write a one-sentence docstring for a Python function named "
                    "'greet' that takes a name argument and returns a greeting "
                    "string like 'Hello, <name>!'."
                ),
                "task_type": TASK_TYPE,
            },
        )
        payload = json.loads(result.content[0].text)
        assert payload["result"]
        assert "caveats" in payload


@pytest.mark.asyncio
async def test_delegate_tool_round_trips_on_fully_async_path_with_fake_client():
    """The full stack -- MCP call_tool -> chain.execute_chain ->
    queue.run_chain -> delegate() -> client.py -- round-trips through a
    fake `DelegateClient`, with no synchronous bridging anywhere."""
    fake_client = FakeGoodClient(result="42", caveats="none")
    chains_config, registry = _single_candidate_chain("fake-candidate")
    server = build_server(
        chains_config=chains_config,
        registry=registry,
        client_factory=lambda candidate: fake_client,
    )
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "delegate_to_local", {"task": "compute the answer", "task_type": TASK_TYPE}
        )
        payload = json.loads(result.content[0].text)
        assert payload["result"] == "42"
        assert payload["caveats"] == "none"
    assert fake_client.calls == 1


@pytest.mark.asyncio
async def test_delegate_tool_surfaces_chain_exhausted_error_as_error_content():
    """A single-candidate chain whose only candidate is unreachable
    exhausts immediately -- surfaced as a distinguishable chain-exhausted
    error, not a bare `DelegateUnreachableError` message."""
    chains_config, registry = _single_candidate_chain("dead-candidate")
    server = build_server(
        chains_config=chains_config,
        registry=registry,
        client_factory=lambda candidate: FakeUnreachableClient(),
    )
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "delegate_to_local", {"task": "anything", "task_type": TASK_TYPE}
        )
        text = result.content[0].text
        assert "error" in text.lower()
        assert "chain exhausted" in text.lower()


@pytest.mark.asyncio
async def test_delegate_tool_surfaces_rate_limited_chain_exhaustion_as_error_content():
    chains_config, registry = _single_candidate_chain("rate-limited-candidate")
    server = build_server(
        chains_config=chains_config,
        registry=registry,
        client_factory=lambda candidate: FakeRateLimitedClient(),
    )
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "delegate_to_local", {"task": "anything", "task_type": TASK_TYPE}
        )
        text = result.content[0].text
        assert "error" in text.lower()
        assert "chain exhausted" in text.lower()


@pytest.mark.asyncio
async def test_delegate_tool_surfaces_no_candidates_error_for_unconfigured_task_type():
    """A `task_type` with no matching chain (e.g. a hosted-only category, or
    simply a typo) surfaces a distinguishable "no candidates" error rather
    than a chain-exhausted one -- no attempt was ever made."""
    server = build_server(chains_config=ChainsConfig(), registry={})
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "delegate_to_local", {"task": "anything", "task_type": "architecture_decision"}
        )
        text = result.content[0].text
        assert "error" in text.lower()
        assert "no delegate candidates" in text.lower()
