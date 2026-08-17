"""Tests for fallback-chain execution (U12, R32-R34, R24 revised, R26/R35).

`execute_chain` is a plain async function over already-resolved config
objects and a `DelegateQueue` -- no MCP server/session anywhere in this
file, proving the chain.py/server.py boundary (a required U12 test
scenario). Error paths use hand-written fake `DelegateClient`s, matching
`test_local_model.py`'s and `test_server.py`'s existing conventions; no
`unittest.mock`.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate.chain import (
    CandidateCooldownError,
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
from josu.delegate.cooldown import CandidateCooldownStore
from josu.delegate.queue import DelegateQueue

TASK_TYPE = "file_summarization"


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _fresh_cooldown_store(
    *, failure_threshold: int = 3, cooldown_seconds: float = 30.0, clock=None
) -> CandidateCooldownStore:
    """A freshly constructed store -- every candidate starts healthy, so
    tests unrelated to cooldown behavior are unaffected by threading this
    through their `execute_chain()` calls."""
    return CandidateCooldownStore(
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
        clock=clock or _FakeClock(),
    )


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
        cooldown_store=_fresh_cooldown_store(),
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
            cooldown_store=_fresh_cooldown_store(),
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
            cooldown_store=_fresh_cooldown_store(),
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
        cooldown_store=_fresh_cooldown_store(),
        client_factory=_factory(clients),
    )

    assert outcome.result == "recovered"
    assert flaky_client.calls == 2  # R24's one retry, same candidate
    assert good_client.calls == 0  # never advanced to the second candidate


@pytest.mark.asyncio
async def test_malformed_response_exhausting_retry_advances_to_next_candidate():
    """When a candidate's R24 same-candidate retry is ALSO exhausted, the
    chain advances to the next candidate -- exactly as it already does for
    DelegateUnreachableError/DelegateRateLimitedError/DelegateAPIError --
    rather than letting `DelegateMalformedResponseError` fail the whole
    delegation. This matches the plan's flowchart (`C -->|malformed
    response| E[retry same candidate once]`, `E -->|still malformed|
    D[advance to next candidate]`): retry-then-fail is R24's job, entirely
    inside `delegate()`; what happens AFTER that retry is exhausted is R34's
    job, handled here."""
    candidates = [_candidate("always-malformed"), _candidate("good")]
    malformed_client = FakeMalformedAlwaysClient()
    good_client = FakeGoodClient(result="42")
    clients = {"always-malformed": malformed_client, "good": good_client}

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["always-malformed", "good"]),
        registry={c.name: c for c in candidates},
        queue=DelegateQueue(),
        cooldown_store=_fresh_cooldown_store(),
        client_factory=_factory(clients),
    )

    assert outcome.result == "42"
    assert malformed_client.calls == 2  # R24's retry, exhausted, same candidate
    assert good_client.calls == 1  # chain advanced to the second candidate


@pytest.mark.asyncio
async def test_all_candidates_malformed_exhausted_raises_chain_exhausted():
    """When EVERY candidate in the chain is malformed-exhausted (each having
    already run R24's own same-candidate retry inside `delegate()`), the
    result is `ChainExhaustedError` -- the same chain-exhausted surface used
    for the unreachable/rate-limited/API-error combination, not a bare
    `DelegateMalformedResponseError` propagating out of `execute_chain`."""
    candidates = [_candidate("always-malformed-1"), _candidate("always-malformed-2")]
    client_1 = FakeMalformedAlwaysClient()
    client_2 = FakeMalformedAlwaysClient()
    clients = {"always-malformed-1": client_1, "always-malformed-2": client_2}

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["always-malformed-1", "always-malformed-2"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            cooldown_store=_fresh_cooldown_store(),
            client_factory=_factory(clients),
        )

    exc = exc_info.value
    assert exc.task_type == TASK_TYPE
    assert exc.attempted == ["always-malformed-1", "always-malformed-2"]
    assert isinstance(exc.last_error, DelegateMalformedResponseError)
    assert client_1.calls == 2  # R24's retry, exhausted
    assert client_2.calls == 2  # R24's retry, exhausted
    assert [r.error for r in exc.skip_records] == [
        "DelegateMalformedResponseError",
        "DelegateMalformedResponseError",
    ]


@pytest.mark.asyncio
async def test_chain_exhausted_via_mixed_malformed_and_unreachable_failures():
    """A chain-exhausted outcome can arise from a mix of failure modes --
    one candidate malformed-exhausted, another unreachable -- confirming
    the two advance paths (R24-then-R34 vs R34 directly) feed the same
    chain-exhaustion surface rather than being handled inconsistently."""
    candidates = [_candidate("always-malformed"), _candidate("dead")]
    malformed_client = FakeMalformedAlwaysClient()
    dead_client = FakeUnreachableClient()
    clients = {"always-malformed": malformed_client, "dead": dead_client}

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["always-malformed", "dead"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            cooldown_store=_fresh_cooldown_store(),
            client_factory=_factory(clients),
        )

    exc = exc_info.value
    assert exc.attempted == ["always-malformed", "dead"]
    assert isinstance(exc.last_error, DelegateUnreachableError)
    assert [r.error for r in exc.skip_records] == [
        "DelegateMalformedResponseError",
        "DelegateUnreachableError",
    ]


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
            cooldown_store=_fresh_cooldown_store(),
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
        cooldown_store=_fresh_cooldown_store(),
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
            cooldown_store=_fresh_cooldown_store(),
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
        cooldown_store=_fresh_cooldown_store(),
        client_factory=_factory({"solo": good_client}),
    )

    assert outcome.result == "solo-result"


# --- Per-candidate cooldown (feat/delegate-candidate-circuit-breaker plan) --


class FakeHangingClient:
    """Never returns and never raises -- simulates a candidate that hangs
    (a network partition, a wedged upstream) rather than failing fast.
    Deliberately ignores its own `timeout` argument, since the whole point
    of KTD3's inner `asyncio.wait_for()` wrapping `delegate()` is to bound
    a hang the client's OWN timeout handling didn't catch."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled by the inner wait_for before this")


class FakeCancellingClient:
    """Raises `asyncio.CancelledError` directly from `complete()` --
    functionally equivalent, for `_attempt()`'s own exception handling, to
    what the code-review-fixed scenario delivers: the OUTER
    `queue.run_chain()` timeout cancelling `_attempt()` from outside
    (e.g. because a real `delegate()` call's own cancellation cleanup took
    longer than the inner wait_for's margin). Raising it directly here is
    a deterministic way to exercise `_attempt()`'s except clause without
    needing to race real wall-clock timing."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_candidate_with_no_prior_failures_is_attempted_normally():
    """Covers R1: a candidate a fresh cooldown store has never seen is
    attempted exactly as if cooldown didn't exist."""
    candidate = _candidate("solo")
    good_client = FakeGoodClient(result="ok")

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["solo"]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        cooldown_store=_fresh_cooldown_store(),
        client_factory=_factory({"solo": good_client}),
    )

    assert outcome.result == "ok"
    assert good_client.calls == 1


@pytest.mark.asyncio
async def test_successful_call_resets_previously_nonzero_failure_count():
    """Covers R3: a successful `execute_chain()` call resets a candidate's
    consecutive-failure count, verified by confirming a fresh round of
    failures (fewer than the threshold) doesn't immediately trip cooldown."""
    candidate = _candidate("flaky-then-good")
    store = _fresh_cooldown_store(failure_threshold=3)
    store.record_failure("flaky-then-good")
    store.record_failure("flaky-then-good")

    good_client = FakeGoodClient(result="recovered")
    await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["flaky-then-good"]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        cooldown_store=store,
        client_factory=_factory({"flaky-then-good": good_client}),
    )

    assert store.is_in_cooldown("flaky-then-good") is False
    store.record_failure("flaky-then-good")
    store.record_failure("flaky-then-good")
    assert store.is_in_cooldown("flaky-then-good") is False  # count restarted from 0


@pytest.mark.asyncio
async def test_candidate_in_cooldown_is_skipped_without_delegate_call():
    """Covers R1, R4: a candidate already in cooldown is skipped without
    `delegate()` ever being invoked, and the chain advances to the next
    candidate."""
    candidates = [_candidate("cooling-down"), _candidate("good")]
    store = _fresh_cooldown_store(failure_threshold=2)
    store.record_failure("cooling-down")
    store.record_failure("cooling-down")
    assert store.is_in_cooldown("cooling-down") is True

    never_called_client = FakeGoodClient()
    good_client = FakeGoodClient(result="42")

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["cooling-down", "good"]),
        registry={c.name: c for c in candidates},
        queue=DelegateQueue(),
        cooldown_store=store,
        client_factory=_factory({"cooling-down": never_called_client, "good": good_client}),
    )

    assert outcome.result == "42"
    assert never_called_client.calls == 0
    assert good_client.calls == 1


@pytest.mark.asyncio
async def test_candidate_attempted_again_after_cooldown_expires():
    """Covers R2: once the cooldown window elapses, the candidate is
    attempted again automatically -- no manual reset."""
    candidate = _candidate("recovering")
    clock = _FakeClock()
    store = _fresh_cooldown_store(failure_threshold=1, cooldown_seconds=30, clock=clock)
    store.record_failure("recovering")
    assert store.is_in_cooldown("recovering") is True

    clock.now += 30
    good_client = FakeGoodClient(result="back")

    outcome = await execute_chain(
        TASK_TYPE,
        "anything",
        chains_config=_chains_config(["recovering"]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        cooldown_store=store,
        client_factory=_factory({"recovering": good_client}),
    )

    assert outcome.result == "back"
    assert good_client.calls == 1


@pytest.mark.asyncio
async def test_candidate_that_hangs_past_timeout_records_failure():
    """Covers R1, R4, R7 (KTD3): a candidate whose `delegate()` call hangs
    past `timeout` still calls `record_failure()` and produces a
    `SkipRecord` -- proving the inner `asyncio.wait_for()` wrapping just
    `delegate()`, not only `queue.run_chain()`'s outer one, is what makes a
    hang observable to the cooldown store. Without KTD3's fix, a hang would
    only ever be caught by `run_chain()`'s own outer `wait_for`, whose
    `TimeoutError` never reaches `_attempt()`'s own exception handling."""
    candidate = _candidate("hanging")
    store = _fresh_cooldown_store(failure_threshold=1)
    hanging_client = FakeHangingClient()

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["hanging"]),
            registry={candidate.name: candidate},
            queue=DelegateQueue(),
            cooldown_store=store,
            client_factory=_factory({"hanging": hanging_client}),
            timeout=0.05,
        )

    assert isinstance(exc_info.value.last_error, TimeoutError)
    assert exc_info.value.skip_records[0].candidate == "hanging"
    assert store.is_in_cooldown("hanging") is True


@pytest.mark.asyncio
async def test_cancelled_error_during_attempt_still_records_failure():
    """Code-review regression test: a `CancelledError` reaching `_attempt()`
    (standing in for the OUTER `queue.run_chain()` timeout cancelling
    `_attempt()` from outside, e.g. because `delegate()`'s own cancellation
    cleanup took longer than the inner wait_for's margin) still calls
    `record_failure()` before propagating -- without this, a candidate that
    hangs badly enough that even its own cancellation is slow would never
    trip cooldown, no matter how generous the outer safety margin is. The
    `CancelledError` itself must still propagate unchanged, never swallowed."""
    candidate = _candidate("cancels-mid-flight")
    store = _fresh_cooldown_store(failure_threshold=1)
    cancelling_client = FakeCancellingClient()

    with pytest.raises(asyncio.CancelledError):
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["cancels-mid-flight"]),
            registry={candidate.name: candidate},
            queue=DelegateQueue(),
            cooldown_store=store,
            client_factory=_factory({"cancels-mid-flight": cancelling_client}),
        )

    assert cancelling_client.calls == 1
    assert store.is_in_cooldown("cancels-mid-flight") is True


@pytest.mark.asyncio
async def test_cooldown_state_shared_across_different_chains_for_same_candidate_name():
    """Covers R6: cooldown state is a property of the candidate name, not
    the chain/task_type that resolved it -- a candidate tripped via one
    chain (standing in for the proactive-check chain) is skipped via a
    different chain (standing in for a task-delegation chain) when both
    share the same store instance."""
    store = _fresh_cooldown_store(failure_threshold=1)
    store.record_failure("shared-candidate")

    candidate = _candidate("shared-candidate")
    other_chains_config = ChainsConfig(
        chains=[
            DelegationChain(
                task_type="directory_summarization",
                candidates=["shared-candidate"],
                explicit_order=True,
            )
        ],
        allow_remote=True,
    )
    never_called_client = FakeGoodClient()

    with pytest.raises(ChainExhaustedError):
        await execute_chain(
            "directory_summarization",
            "anything",
            chains_config=other_chains_config,
            registry={candidate.name: candidate},
            queue=DelegateQueue(),
            cooldown_store=store,
            client_factory=_factory({"shared-candidate": never_called_client}),
        )

    assert never_called_client.calls == 0


@pytest.mark.asyncio
async def test_every_candidate_cooled_down_raises_chain_exhausted_not_no_candidates():
    """Covers R1, R7, AE2 (KTD1): when every candidate in the resolved
    chain is currently in cooldown, `ChainExhaustedError` fires -- not
    `NoCandidatesError` -- with one `SkipRecord` per candidate, each
    reporting `CandidateCooldownError`. This is the scenario a naive
    implementation (filtering cooldown candidates out inside
    `resolve_chain()` instead of inside `execute_chain()`'s attempt loop)
    would get wrong."""
    candidates = [_candidate("cold-1"), _candidate("cold-2")]
    store = _fresh_cooldown_store(failure_threshold=1)
    store.record_failure("cold-1")
    store.record_failure("cold-2")

    never_called = {"cold-1": FakeGoodClient(), "cold-2": FakeGoodClient()}

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["cold-1", "cold-2"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            cooldown_store=store,
            client_factory=_factory(never_called),
        )

    exc = exc_info.value
    assert isinstance(exc.last_error, CandidateCooldownError)
    assert [r.error for r in exc.skip_records] == [
        "CandidateCooldownError",
        "CandidateCooldownError",
    ]
    assert never_called["cold-1"].calls == 0
    assert never_called["cold-2"].calls == 0


@pytest.mark.asyncio
async def test_mixed_chain_cooldown_skip_and_real_failure_distinguishable():
    """Covers R7, AE1: a chain with one cooldown-skipped candidate and one
    actually-attempted-and-failed candidate produces `skip_records`
    distinguishing the two by `error` -- not both reporting the same
    generic reason."""
    candidates = [_candidate("cold"), _candidate("genuinely-dead")]
    store = _fresh_cooldown_store(failure_threshold=1)
    store.record_failure("cold")

    clients = {"cold": FakeGoodClient(), "genuinely-dead": FakeUnreachableClient()}

    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["cold", "genuinely-dead"]),
            registry={c.name: c for c in candidates},
            queue=DelegateQueue(),
            cooldown_store=store,
            client_factory=_factory(clients),
        )

    assert [r.error for r in exc_info.value.skip_records] == [
        "CandidateCooldownError",
        "DelegateUnreachableError",
    ]
    assert clients["cold"].calls == 0


@pytest.mark.asyncio
async def test_n_consecutive_failures_trip_candidate_into_cooldown_mid_test():
    """Covers AE3: N consecutive qualifying failures across separate
    `execute_chain()` calls trip a candidate into cooldown, observable via
    a subsequent call skipping it entirely."""
    candidate = _candidate("degrading")
    store = _fresh_cooldown_store(failure_threshold=2)
    dead_client = FakeUnreachableClient()

    for _ in range(2):
        with pytest.raises(ChainExhaustedError):
            await execute_chain(
                TASK_TYPE,
                "anything",
                chains_config=_chains_config(["degrading"]),
                registry={candidate.name: candidate},
                queue=DelegateQueue(),
                cooldown_store=store,
                client_factory=_factory({"degrading": dead_client}),
            )

    assert dead_client.calls == 2
    assert store.is_in_cooldown("degrading") is True

    never_called = FakeGoodClient()
    with pytest.raises(ChainExhaustedError) as exc_info:
        await execute_chain(
            TASK_TYPE,
            "anything",
            chains_config=_chains_config(["degrading"]),
            registry={candidate.name: candidate},
            queue=DelegateQueue(),
            cooldown_store=store,
            client_factory=_factory({"degrading": never_called}),
        )

    assert isinstance(exc_info.value.last_error, CandidateCooldownError)
    assert never_called.calls == 0
