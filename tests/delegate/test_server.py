"""Tests for the delegate-to-local MCP server (U2).

The happy path exercises a real Ollama call against the curated
qwen2.5-coder:7b model -- no mocks, matching U1's testing philosophy. The
error paths (unreachable daemon, malformed response) are exercised through
a hand-written fake implementing ollama.Client's `.chat()` shape, since
those failure modes aren't reliably reproducible against a real model on
demand.
"""

from __future__ import annotations

import json

import ollama
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from josu.delegate.local_model import delegate
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


class FakeMalformedTwiceClient:
    """Fake ollama client returning unparseable content on every call."""

    def chat(self, model, messages, format=None):
        return {"message": {"content": "not json at all"}}


class FakeMalformedThenGoodClient:
    """Fake ollama client that fails to parse once, then succeeds on retry."""

    def __init__(self):
        self.calls = 0

    def chat(self, model, messages, format=None):
        self.calls += 1
        if self.calls == 1:
            return {"message": {"content": "not json"}}
        return {"message": {"content": json.dumps({"result": "ok", "caveats": "retried"})}}


class FakeUnreachableClient:
    def chat(self, model, messages, format=None):
        raise ConnectionError("connection refused")


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
        # to anything, since that retrieval quality is covered separately by
        # test_delegate_falls_back_to_file_exploration_when_graph_has_no_match.
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


def test_delegate_retries_once_on_malformed_response_then_succeeds():
    client = FakeMalformedThenGoodClient()
    outcome = delegate("summarize", client=client, available_ram_gb=64.0)
    assert outcome.result == "ok"
    assert outcome.caveats == "retried"
    assert client.calls == 2


def test_delegate_raises_malformed_after_exhausting_retry():
    from josu.delegate.local_model import LocalModelMalformedResponseError

    with pytest.raises(LocalModelMalformedResponseError):
        delegate("summarize", client=FakeMalformedTwiceClient(), available_ram_gb=64.0)


def test_delegate_raises_unreachable_on_connection_refused():
    from josu.delegate.local_model import LocalModelUnreachableError

    with pytest.raises(LocalModelUnreachableError):
        delegate("summarize", client=FakeUnreachableClient(), available_ram_gb=64.0)


def test_delegate_unreachable_against_real_ollama_client_pointed_nowhere():
    """Same error path, but through the real ollama.Client -- no fake -- pointed
    at a host nothing is listening on, to confirm the real ConnectionError
    surfaces through our wrapper unchanged."""
    from josu.delegate.local_model import LocalModelUnreachableError

    bad_client = ollama.Client(host="http://127.0.0.1:1", timeout=2)
    with pytest.raises(LocalModelUnreachableError):
        delegate("summarize", client=bad_client, available_ram_gb=64.0)


def test_delegate_falls_back_to_file_exploration_when_graph_has_no_match(
    built_engine, fixture_repo
):
    from josu.delegate.local_model import _fallback_file_context, _graph_context

    # A query guaranteed not to match anything in the fixture graph.
    context = _graph_context(built_engine, "zzz_nonexistent_symbol_zzz")
    assert context == []

    fallback = _fallback_file_context(fixture_repo)
    assert any("helper.py" in path for path in fallback)
