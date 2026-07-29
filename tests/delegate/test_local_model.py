"""Tests for `delegate()` itself (U2, U11) -- independent of the MCP
transport, which `test_server.py` covers separately.

The happy path exercises a real Ollama instance through the new generic
`OpenAICompatibleDelegateClient` pointed at Ollama's own OpenAI-compatible
`/v1` endpoint (no mocks) -- proving Ollama is now just one configured
candidate, not a special-cased code path. Error paths (unreachable,
malformed, rate-limited, API-error) are exercised through hand-written fakes
implementing `DelegateClient`'s `async def complete(...)` shape, since those
failure modes aren't reliably reproducible against a real model on demand.
"""

from __future__ import annotations

import json

import pytest

from josu.delegate.client import (
    DelegateAPIError,
    DelegateMalformedResponseError,
    DelegateRateLimitedError,
    DelegateUnreachableError,
    OpenAICompatibleDelegateClient,
)
from josu.delegate.local_model import delegate
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
    """Fake `DelegateClient` returning unparseable content on every call."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        return "not json at all"


class FakeMalformedThenGoodClient:
    """Fake `DelegateClient` that returns unparseable content once, then
    succeeds on the same-candidate retry (R24)."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        if self.calls == 1:
            return "not json"
        return json.dumps({"result": "ok", "caveats": "retried"})


class FakeEnvelopeMalformedThenGoodClient:
    """Fake `DelegateClient` whose first call raises
    `DelegateMalformedResponseError` directly (an envelope-level failure,
    e.g. an unparseable HTTP body) then succeeds -- proving the retry-once
    path covers client-level malformation, not just content-level."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        if self.calls == 1:
            raise DelegateMalformedResponseError("fake", "bad envelope")
        return json.dumps({"result": "ok", "caveats": ""})


class FakeDeeplyNestedContentClient:
    """Fake `DelegateClient` whose `content` (the model's own {result,
    caveats} envelope, parsed by `local_model.py`'s own `_parse_response()`)
    is deeply nested JSON -- distinct from `test_client.py`'s coverage of
    the OUTER HTTP response body being deeply nested; this covers the
    `json.loads(content)` call inside `_parse_response()` itself hitting
    the same `RecursionError` failure mode."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        return ("[" * 3000) + ("]" * 3000)


class FakeUnreachableClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise DelegateUnreachableError("fake")


class FakeRateLimitedClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise DelegateRateLimitedError("fake", retry_after="5")


class FakeAPIErrorClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise DelegateAPIError("fake", status_code=500)


@pytest.mark.asyncio
async def test_delegate_retries_once_on_malformed_content_then_succeeds():
    client = FakeMalformedThenGoodClient()
    outcome = await delegate("summarize", client=client)
    assert outcome.result == "ok"
    assert outcome.caveats == "retried"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_delegate_raises_malformed_after_exhausting_content_retry():
    client = FakeMalformedTwiceClient()
    with pytest.raises(DelegateMalformedResponseError):
        await delegate("summarize", client=client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_delegate_raises_malformed_after_recursion_error_parsing_content_not_uncaught_crash():
    """Covers the P1 fix: deeply nested JSON in the model's own {result,
    caveats} `content` string used to raise an uncaught `RecursionError`
    from `_parse_response()`'s `json.loads(content)` call -- crashing past
    both the R24 same-candidate retry and the R34 chain-advance taxonomy.
    It must now be treated as an ordinary malformed response: retried once
    (same as a `json.JSONDecodeError`/`KeyError`), then raised as
    `DelegateMalformedResponseError`."""
    client = FakeDeeplyNestedContentClient()
    with pytest.raises(DelegateMalformedResponseError):
        await delegate("summarize", client=client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_delegate_retries_once_on_envelope_malformed_then_succeeds():
    client = FakeEnvelopeMalformedThenGoodClient()
    outcome = await delegate("summarize", client=client)
    assert outcome.result == "ok"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_delegate_raises_unreachable_immediately_without_retry():
    client = FakeUnreachableClient()
    with pytest.raises(DelegateUnreachableError):
        await delegate("summarize", client=client)
    # Unreachable/rate-limited/API-error feed U12's chain-advance logic --
    # they must NOT trigger local_model.py's same-candidate retry.
    assert client.calls == 1


@pytest.mark.asyncio
async def test_delegate_raises_rate_limited_immediately_without_retry():
    client = FakeRateLimitedClient()
    with pytest.raises(DelegateRateLimitedError) as exc_info:
        await delegate("summarize", client=client)
    assert client.calls == 1
    assert exc_info.value.retry_after == "5"


@pytest.mark.asyncio
async def test_delegate_raises_api_error_immediately_without_retry():
    client = FakeAPIErrorClient()
    with pytest.raises(DelegateAPIError):
        await delegate("summarize", client=client)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_delegate_unreachable_against_real_client_pointed_nowhere():
    """Same error path, but through the real `OpenAICompatibleDelegateClient`
    -- no fake -- pointed at a host nothing is listening on, to confirm a
    genuine connection failure surfaces through our wrapper unchanged."""
    bad_client = OpenAICompatibleDelegateClient("http://127.0.0.1:1", name="nowhere")
    with pytest.raises(DelegateUnreachableError):
        await delegate("summarize", client=bad_client, timeout=2)


@pytest.mark.asyncio
async def test_delegate_against_real_ollama_as_plain_openai_compatible_candidate():
    """Ollama, pointed at via its own OpenAI-compatible `/v1` endpoint and
    the generic client -- not the `ollama` SDK -- round-trips correctly,
    proving it's just one configured candidate now, not a special case."""
    client = OpenAICompatibleDelegateClient("http://localhost:11434/v1", name="ollama")
    outcome = await delegate(
        "Reply with the single word: pong. No punctuation.",
        model="qwen2.5-coder:7b",
        client=client,
    )
    assert outcome.result
    assert isinstance(outcome.caveats, str)


def test_delegate_falls_back_to_file_exploration_when_graph_has_no_match(
    built_engine, fixture_repo
):
    from josu.delegate.local_model import _fallback_file_context, _graph_context

    # A query guaranteed not to match anything in the fixture graph.
    context = _graph_context(built_engine, "zzz_nonexistent_symbol_zzz")
    assert context == []

    fallback = _fallback_file_context(fixture_repo)
    assert any("helper.py" in path for path in fallback)
