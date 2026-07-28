"""Tests for fallback-chain execution (U12, R32-R34, R24 revised, R26/R35).

`execute_chain` is a plain async function over already-resolved config
objects and a `DelegateQueue` -- no MCP server/session anywhere in this
file, proving the chain.py/server.py boundary (a required U12 test
scenario). Error paths use hand-written fake `DelegateClient`s, matching
`test_local_model.py`'s and `test_server.py`'s existing conventions; no
`unittest.mock`.
"""

from __future__ import annotations

import json

import pytest

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate.chain import (
    ChainExhaustedError,
    NoCandidatesError,
    execute_chain,
)
from josu.delegate.client import (
    DelegateAPIError,
    DelegateMalformedResponseError,
    DelegateRateLimitedError,
    DelegateUnreachableError,
)
from josu.delegate.queue import DelegateQueue

TASK_TYPE = "file_summarization"


def _candidate(name: str, *, local: bool = True, model: str = "test-model") -> DelegateCandidate:
    # `model` deliberately defaults to a name NOT in `CURATED_MODELS`, so
    # `preflight_check` trivially passes regardless of the test machine's
    # available RAM (see `models/curated.py`) -- tests that specifically
    # want to exercise the preflight gate use "qwen2.5-coder:7b" instead.
    return DelegateCandidate(
        name=name, endpoint="http://example.invalid", local=local, model=model
    )


def _chains_config(candidate_names: list[str]) -> ChainsConfig:
    return ChainsConfig(
        chains=[
            DelegationChain(
                task_type=TASK_TYPE, candidates=candidate_names, explicit_order=True
            )
        ],
        allow_remote=True,
    )


def _factory(mapping: dict):
    def factory(candidate):
        return mapping[candidate.name]

    return factory


class FakeGoodClient:
    def __init__(self, result="ok", caveats=""):
        self._result = result
        self._caveats = caveats
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        return json.dumps({"result": self._result, "caveats": self._caveats})


class FakeUnreachableClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise DelegateUnreachableError("candidate-unreachable")


class FakeRateLimitedClient:
    def __init__(self, retry_after="5"):
        self.calls = 0
        self._retry_after = retry_after

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise DelegateRateLimitedError("candidate-rate-limited", retry_after=self._retry_after)


class FakeAPIErrorClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise DelegateAPIError("candidate-api-error", status_code=500)


class FakeMalformedAlwaysClient:
    """Returns unparseable content on every call -- exhausts `delegate()`'s
    own R24 same-candidate retry (two calls) and then stays malformed."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        return "not json at all"


class FakeMalformedThenGoodClient:
    """Malformed on the first call, then succeeds on `delegate()`'s R24
    same-candidate retry -- the chain should never even know this happened."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        if self.calls == 1:
            return "not json"
        return json.dumps({"result": "recovered", "caveats": "retried"})


@pytest.mark.asyncio
async def test_first_candidate_unreachable_second_succeeds_advances_transparently():
    """Covers R34: unreachable/rate-limited/API-error candidates are skipped
    in favor of the next one, and the caller sees one clean success."""
    candidates = [_candidate("dead"), _candidate("good")]
    good_client = FakeGoodClient(result="42", caveats="")
    clients = {"dead": FakeUnreachableClient(), "good": good_client}

    outcome = await execute_chain(
        TASK_TYPE,
        "compute the answer",
        chains_config=_chains_config(["dead", "good"]),
        registry={c.name: c for c in candidates},
        queue=DelegateQueue(),
        client_factory=_factory(clients),
    )

    assert outcome.result == "42"
    assert good_client.calls == 1


@pytest.mark.asyncio
async def test_all_candidates_fail_raises_chain_exhausted_not_generic_error():
    """The surfaced error reflects the whole chain being exhausted, not just
    the last candidate's own failure -- distinguishable for U6's future run
    log, not a generic exception."""
    candidates = [_candidate("dead"), _candidate("rate-limited"), _candidate("api-error")]
    clients = {
        "dead": FakeUnreachableClient(),
        "rate-limited": FakeRateLimitedClient(),
        "api-error": FakeAPIErrorClient(),
    }

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["dead", "rate-limited", "api-error"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            client_factory=_factory(clients),
        )

    exc = exc_info.value
    assert exc.task_type == TASK_TYPE
    assert exc.attempted == ["dead", "rate-limited", "api-error"]
    assert isinstance(exc.last_error, DelegateAPIError)
    assert "exhausted" in str(exc).lower()


@pytest.mark.asyncio
async def test_rate_limited_retry_after_captured_before_advancing():
    """Covers the Retry-After capture scenario: the value is recorded in
    `skip_records` before the chain moves past that candidate, for later
    run-log use (U6)."""
    candidates = [_candidate("rate-limited"), _candidate("dead")]
    clients = {
        "rate-limited": FakeRateLimitedClient(retry_after="17"),
        "dead": FakeUnreachableClient(),
    }

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["rate-limited", "dead"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            client_factory=_factory(clients),
        )

    skip_records = exc_info.value.skip_records
    assert skip_records[0].candidate == "rate-limited"
    assert skip_records[0].error == "DelegateRateLimitedError"
    assert skip_records[0].retry_after == "17"


@pytest.mark.asyncio
async def test_malformed_response_triggers_same_candidate_retry_not_chain_advance():
    """A malformed response from the first candidate triggers R24's
    one-retry-same-candidate behavior (inside delegate()), not an immediate
    chain advance -- the second candidate is never even attempted."""
    candidates = [_candidate("flaky"), _candidate("good")]
    flaky_client = FakeMalformedThenGoodClient()
    good_client = FakeGoodClient()
    clients = {"flaky": flaky_client, "good": good_client}

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["flaky", "good"]),
        registry={c.name: c for c in candidates},
        queue=DelegateQueue(),
        client_factory=_factory(clients),
    )

    assert outcome.result == "recovered"
    assert flaky_client.calls == 2  # R24's one retry, same candidate
    assert good_client.calls == 0  # never advanced to the second candidate


@pytest.mark.asyncio
async def test_malformed_response_exhausting_retry_does_not_advance_chain():
    """When a candidate's R24 retry is ALSO exhausted, the resulting
    `DelegateMalformedResponseError` propagates directly out of
    `execute_chain` -- it is NOT wrapped as `ChainExhaustedError` and does
    NOT advance to the next candidate. The two retry layers (R24 vs R34)
    stay distinct in the code, never merged into one generic retry loop."""
    candidates = [_candidate("always-malformed"), _candidate("good")]
    malformed_client = FakeMalformedAlwaysClient()
    good_client = FakeGoodClient()
    clients = {"always-malformed": malformed_client, "good": good_client}

    with pytest.raises(DelegateMalformedResponseError):
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["always-malformed", "good"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            client_factory=_factory(clients),
        )

    assert malformed_client.calls == 2  # R24's retry, exhausted
    assert good_client.calls == 0  # chain did NOT advance on malformed


@pytest.mark.asyncio
async def test_local_candidate_calls_preflight_check_before_attempting():
    """Covers R26/R35: a `local=True` candidate whose curated model doesn't
    fit the (forced-tiny) available RAM is skipped via preflight before any
    client call is ever made -- proving preflight ran and gated the
    attempt, not just that the call eventually failed some other way."""
    candidates = [_candidate("undersized-local", local=True, model="qwen2.5-coder:7b")]
    local_client = FakeGoodClient()
    clients = {"undersized-local": local_client}

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["undersized-local"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            client_factory=_factory(clients),
            available_ram_gb=0.1,  # far below qwen2.5-coder:7b's 5.0 GB floor
        )

    assert local_client.calls == 0  # preflight rejected it before any attempt
    assert isinstance(exc_info.value.last_error, DelegateUnreachableError)


@pytest.mark.asyncio
async def test_remote_only_chain_never_calls_preflight_check():
    """Covers R26/R35: a remote candidate is attempted directly even under
    the same forced-tiny RAM ceiling that would fail preflight for a local
    candidate -- proving preflight is never consulted for remote candidates
    at all, not just that it happens to pass."""
    candidates = [
        _candidate("remote-only", local=False, model="qwen2.5-coder:7b"),
    ]
    remote_client = FakeGoodClient(result="remote-ok")
    clients = {"remote-only": remote_client}

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["remote-only"]),
        registry={c.name: c for c in candidates},
        queue=DelegateQueue(),
        client_factory=_factory(clients),
        available_ram_gb=0.1,
    )

    assert outcome.result == "remote-ok"
    assert remote_client.calls == 1


@pytest.mark.asyncio
async def test_task_type_with_no_matching_chain_raises_no_candidates_error():
    with pytest.raises(NoCandidatesError):
        await execute_chain(
            "architecture_decision",  # a hosted-only category, no chain configured
            "anything",
            chains_config=ChainsConfig(),
            registry={},
            queue=DelegateQueue(),
        )


@pytest.mark.asyncio
async def test_execute_chain_independently_callable_without_mcp_server_or_session():
    """Covers the chain.py/server.py boundary: `execute_chain` is a plain
    async function, callable and testable with nothing MCP-related in
    scope -- no `Server`, no `ClientSession`, no `create_connected_server_and_client_session`."""
    candidate = _candidate("solo")
    good_client = FakeGoodClient(result="solo-result")

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["solo"]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        client_factory=_factory({"solo": good_client}),
    )

    assert outcome.result == "solo-result"
