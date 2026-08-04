"""Tests for U7's quota-exhaustion detection and direct-request fallback
path (R28, R29).

`route_bounded_request`/`handle_invocation_failure` are plain functions
over already-resolved config objects, a `DelegateQueue`, and (for the
mid-run detection path) a raw `InvocationResult` -- no MCP server/session
and no real Claude Code process spawn anywhere in this file. Error paths
use hand-written fake `DelegateClient`s, matching `test_chain.py`'s/
`test_local_model.py`'s existing conventions; no `unittest.mock`.
"""

from __future__ import annotations

import json

import pytest

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate.chain import ChainExhaustedError, NoCandidatesError
from josu.delegate.queue import DelegateQueue
from josu.fallback.quota import (
    ABANDON_REASON_QUOTA_EXHAUSTION,
    HOSTED_PAUSED_NOTICE,
    ClaudeCodeFailureSignal,
    TaskNotBoundedError,
    default_quota_classifier,
    handle_invocation_failure,
    is_quota_exhaustion,
    list_abandoned_worktrees,
    mark_worktree_abandoned,
    route_bounded_request,
)
from josu.orchestrator.adapter import InvocationResult
from josu.orchestrator.worktree import Worktree

BOUNDED_TASK_TYPE = "file_summarization"
HOSTED_ONLY_TASK_TYPE = "architecture_decision"


def _candidate(name: str, *, local: bool = True, model: str = "test-model") -> DelegateCandidate:
    return DelegateCandidate(
        name=name, endpoint="http://example.invalid", local=local, model=model
    )


def _chains_config(task_type: str, candidate_names: list[str]) -> ChainsConfig:
    return ChainsConfig(
        chains=[
            DelegationChain(
                task_type=task_type, candidates=candidate_names, explicit_order=True
            )
        ],
        allow_remote=True,
    )


def _factory(mapping: dict):
    def factory(candidate):
        return mapping[candidate.name]

    return factory


def _invocation(*, returncode: int, stdout: str = "", stderr: str = "") -> InvocationResult:
    return InvocationResult(argv=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def _worktree(tmp_path, name: str = "josu-abc123") -> Worktree:
    path = tmp_path / "worktrees" / name
    path.mkdir(parents=True)
    return Worktree(path=path, branch=f"josu/{name}", repo_root=tmp_path, stash_ref=None)


class FakeGoodClient:
    """A hand-written fake standing in for the local delegate worker --
    matching `test_chain.py`'s convention, never a real process spawn."""

    def __init__(self, result="ok", caveats=""):
        self._result = result
        self._caveats = caveats
        self.calls = 0

    async def complete(self, *, model, messages, timeout):
        self.calls += 1
        return json.dumps({"result": self._result, "caveats": self._caveats})


# --- Test scenario 1: simulated quota signal routes a matching bounded
# request to the delegate worker directly, with an explicit "hosted
# paused" message (Covers R29). ----------------------------------------


@pytest.mark.asyncio
async def test_quota_signal_routes_bounded_request_directly_with_hosted_paused_message():
    quota_invocation = _invocation(
        returncode=1,
        stdout="API Error: Rate limit reached. Please try again after your 5-hour limit resets.",
    )
    assert is_quota_exhaustion(quota_invocation) is True

    candidate = _candidate("local-good")
    good_client = FakeGoodClient(result="summarized.", caveats="none")

    outcome = await route_bounded_request(
        BOUNDED_TASK_TYPE,
        "summarize this file",
        chains_config=_chains_config(BOUNDED_TASK_TYPE, ["local-good"]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        client_factory=_factory({"local-good": good_client}),
    )

    assert outcome.delegate_result.result == "summarized."
    assert outcome.message == HOSTED_PAUSED_NOTICE
    assert "paused" in outcome.message.lower()
    assert good_client.calls == 1


@pytest.mark.asyncio
async def test_quota_exhaustion_mid_run_marks_the_triggering_worktree_abandoned(tmp_path):
    """Verification scenario: a quota-exhaustion detection with a
    worktree present marks that worktree abandoned, discoverable via
    `list_abandoned_worktrees` -- the marker a future `josu cleanup` (U8)
    would surface."""
    worktree = _worktree(tmp_path)
    quota_invocation = _invocation(
        returncode=1, stderr="Usage limit reached ∙ resets 11pm"
    )

    outcome = handle_invocation_failure(quota_invocation, worktree=worktree)

    assert outcome.quota_exhausted is True
    assert outcome.abandoned_marker_path is not None
    assert outcome.abandoned_marker_path.exists()

    records = list_abandoned_worktrees(outcome.abandoned_marker_path.parent)
    assert len(records) == 1
    assert records[0].worktree_path == str(worktree.path)
    assert records[0].branch == worktree.branch
    assert records[0].reason == ABANDON_REASON_QUOTA_EXHAUSTION


# --- Test scenario 2: a non-quota failure (simulated auth error) does not
# trigger fallback mode. --------------------------------------------------


def test_auth_error_is_not_classified_as_quota_exhaustion():
    auth_failure = ClaudeCodeFailureSignal(
        returncode=1, stderr="API Error: 401 Unauthorized -- invalid api key provided"
    )
    assert default_quota_classifier(auth_failure) is False
    assert is_quota_exhaustion(auth_failure) is False


def test_network_blip_is_not_classified_as_quota_exhaustion():
    network_failure = ClaudeCodeFailureSignal(
        returncode=1, stderr="Error: connection refused while reaching api.anthropic.com"
    )
    assert is_quota_exhaustion(network_failure) is False


def test_successful_invocation_returncode_zero_is_never_quota_exhaustion():
    """Even if the text happened to mention "limit" somewhere, a clean exit
    is never classified as quota exhaustion -- quota exhaustion is
    definitionally a failed invocation."""
    clean = ClaudeCodeFailureSignal(returncode=0, stdout="rate limit info: 12% used this window")
    assert is_quota_exhaustion(clean) is False


def test_auth_error_mid_run_does_not_mark_the_worktree_abandoned(tmp_path):
    worktree = _worktree(tmp_path)
    auth_invocation = _invocation(returncode=1, stderr="Authentication error: invalid api key")

    outcome = handle_invocation_failure(auth_invocation, worktree=worktree)

    assert outcome.quota_exhausted is False
    assert outcome.abandoned_marker_path is None
    assert not (worktree.repo_root / ".josu" / "abandoned-worktrees").exists()


def test_classifier_is_injectable_not_hardcoded():
    """The classifier is a pluggable seam (per the plan's Key Technical
    Decisions -- the exact signal is deferred to implementation-time
    research): a caller can override `default_quota_classifier` entirely,
    e.g. with a refined heuristic discovered later."""

    def always_quota(signal: ClaudeCodeFailureSignal) -> bool:
        return True

    def never_quota(signal: ClaudeCodeFailureSignal) -> bool:
        return False

    ordinary_failure = ClaudeCodeFailureSignal(returncode=1, stderr="some unrelated crash")
    quota_looking_failure = ClaudeCodeFailureSignal(returncode=1, stderr="rate limit reached")

    assert is_quota_exhaustion(ordinary_failure, classifier=always_quota) is True
    assert is_quota_exhaustion(quota_looking_failure, classifier=never_quota) is False


# --- Test scenario 3: a complex non-bounded request during the outage is
# not attempted locally -- it's told to wait rather than auto-decomposed. -


@pytest.mark.asyncio
async def test_non_bounded_task_type_during_outage_is_told_to_wait_not_attempted():
    with pytest.raises(TaskNotBoundedError) as exc_info:
        await route_bounded_request(
            HOSTED_ONLY_TASK_TYPE,
            "redesign the module boundary",
            chains_config=ChainsConfig(),
            registry={},
            queue=DelegateQueue(),
        )

    assert exc_info.value.task_type == HOSTED_ONLY_TASK_TYPE
    assert "wait" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_non_bounded_task_type_never_reaches_execute_chain(tmp_path):
    """Confirms the wait-don't-attempt behavior isn't just an eventual
    empty-chain failure -- `execute_chain` (and therefore the delegate
    worker) is never even called for a non-bounded task_type, whether or
    not a chain happens to be configured for it."""
    candidate = _candidate("would-be-used")
    client = FakeGoodClient()

    with pytest.raises(TaskNotBoundedError):
        await route_bounded_request(
            HOSTED_ONLY_TASK_TYPE,
            "anything",
            chains_config=_chains_config(HOSTED_ONLY_TASK_TYPE, ["would-be-used"]),
            registry={candidate.name: candidate},
            queue=DelegateQueue(),
            client_factory=_factory({"would-be-used": client}),
        )

    assert client.calls == 0


# --- Verification: bounded request completes via the delegate worker
# alone, with no worktree or Claude Code invocation created for it -------


@pytest.mark.asyncio
async def test_bounded_request_completes_without_any_worktree_or_orchestrator_import():
    """`route_bounded_request` never touches `orchestrator/worktree.py`'s
    `create_worktree`/`remove_worktree` or `orchestrator/adapters/
    claude_code.py`'s `run` -- this test module never imports either, and
    the fallback path completes purely via `chain.py`/`queue.py`."""
    candidate = _candidate("solo")
    good_client = FakeGoodClient(result="direct-result")

    outcome = await route_bounded_request(
        BOUNDED_TASK_TYPE,
        "anything",
        chains_config=_chains_config(BOUNDED_TASK_TYPE, ["solo"]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        client_factory=_factory({"solo": good_client}),
    )

    assert outcome.delegate_result.result == "direct-result"


@pytest.mark.asyncio
async def test_chain_exhausted_propagates_unchanged_through_fallback_path():
    """`route_bounded_request` doesn't swallow or reinterpret
    `ChainExhaustedError`/`NoCandidatesError` -- they propagate exactly as
    `execute_chain` raises them."""
    with pytest.raises(NoCandidatesError):
        await route_bounded_request(
            BOUNDED_TASK_TYPE,
            "anything",
            chains_config=ChainsConfig(),  # no chain configured at all
            registry={},
            queue=DelegateQueue(),
        )


def test_mark_worktree_abandoned_is_directly_callable_and_overwrites_on_re_mark(tmp_path):
    """`mark_worktree_abandoned` doesn't require going through
    `handle_invocation_failure` -- a future caller (e.g. U8's cleanup
    command doing its own crash-recovery scan) can call it directly. Also
    covers writing to the same worktree name twice landing at one marker
    file, not two."""
    worktree = _worktree(tmp_path)

    first_path = mark_worktree_abandoned(worktree, reason="crash_recovery_test")
    second_path = mark_worktree_abandoned(worktree, reason=ABANDON_REASON_QUOTA_EXHAUSTION)

    assert first_path == second_path
    records = list_abandoned_worktrees(first_path.parent)
    assert len(records) == 1
    assert records[0].reason == ABANDON_REASON_QUOTA_EXHAUSTION


def test_list_abandoned_worktrees_on_missing_directory_returns_empty(tmp_path):
    assert list_abandoned_worktrees(tmp_path / "does-not-exist") == []


def test_default_classifier_marker_only_matches_documented_quota_phrases():
    """Sanity check on the heuristic's shape: a generic 500 or a malformed-
    response style message doesn't accidentally match."""
    generic_500 = ClaudeCodeFailureSignal(returncode=1, stderr="internal server error")
    assert default_quota_classifier(generic_500) is False

    quota_phrase = ClaudeCodeFailureSignal(returncode=1, stdout="usage limit reached, resets 3pm")
    assert default_quota_classifier(quota_phrase) is True
