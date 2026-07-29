"""Tests for U9's proactive checks (R15-R17, R39).

`run_proactive_check`/`DebouncedSaveWatcher`/`install_commit_hook` are
plain functions/classes over already-resolved config objects, an injectable
`Scheduler` seam, and real (but throwaway, `tmp_path`-scoped) git repos for
the hook-installation tests -- no MCP server/session, no real delegate
network call, and no real Claude Code process spawn anywhere in this file.
Error/decision paths use hand-written fakes for the two true I/O
boundaries this module crosses (the delegate client, matching
`test_chain.py`'s/`test_quota.py`'s existing convention, and the Claude
Code invocation boundary via a fake `ClaudeCodeInvoker`); no
`unittest.mock`.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from josu.config.chains import (
    PROACTIVE_CHECK_TASK_TYPE,
    ChainsConfig,
    DelegationChain,
)
from josu.config.delegate import DelegateCandidate
from josu.delegate.queue import DelegateQueue
from josu.proactive.watchers import (
    DebouncedSaveWatcher,
    HookInstallationError,
    ProactiveCheckOutcome,
    _local_only_execution_inputs,
    default_severity_classifier,
    install_commit_hook,
    query_on_demand,
    run_proactive_check,
)

# --- Shared fixtures/fakes, matching test_chain.py's/test_quota.py's
# conventions -----------------------------------------------------------


def _candidate(name: str, *, local: bool = True, model: str = "test-model") -> DelegateCandidate:
    # `model` deliberately not in `CURATED_MODELS` so `preflight_check`
    # trivially passes regardless of the test machine's RAM -- matching
    # `test_chain.py`'s convention.
    return DelegateCandidate(name=name, endpoint="http://example.invalid", local=local, model=model)


def _chains_config(candidate_names: list[str], *, allow_remote: bool = False) -> ChainsConfig:
    return ChainsConfig(
        chains=[
            DelegationChain(
                task_type=PROACTIVE_CHECK_TASK_TYPE,
                candidates=candidate_names,
                explicit_order=True,
            )
        ],
        allow_remote=allow_remote,
    )


def _factory(mapping: dict):
    def factory(candidate):
        return mapping[candidate.name]

    return factory


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


class FakeClaudeCodeInvoker:
    """Hand-written fake standing in for the Claude Code invocation
    boundary (`ClaudeCodeInvoker`) -- never spawns a real `claude`
    process. Records every finding it was invoked with."""

    def __init__(self, fake_result: str = "claude-code-ran"):
        self.calls = 0
        self.last_finding = None
        self._fake_result = fake_result

    def __call__(self, finding):
        self.calls += 1
        self.last_finding = finding
        return self._fake_result


class FakeScheduler:
    """Hand-written stand-in for `DebouncedSaveWatcher`'s `Scheduler` seam
    -- records every `(delay, callback)` it's asked to schedule and lets a
    test fire (or confirm the absence of) one explicitly, instead of
    depending on real wall-clock timers."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, Callable[[], None]]] = []
        self.cancelled: list[Callable[[], None]] = []

    def __call__(self, delay: float, callback: Callable[[], None]) -> Callable[[], None]:
        self.scheduled.append((delay, callback))

        def cancel() -> None:
            self.cancelled.append(callback)

        return cancel

    def fire_latest(self) -> None:
        """Simulate the debounce window elapsing for the most recently
        scheduled callback -- exactly what a real timer would do once
        nothing cancelled it first."""
        _, callback = self.scheduled[-1]
        callback()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


# --- Test scenario: a debounced save event triggers a local-only check --


def test_debounced_save_event_triggers_local_only_check():
    candidate = _candidate("local-only")
    client = FakeGoodClient(result="looks fine", caveats="")
    chains_config = _chains_config([candidate.name])
    registry = {candidate.name: candidate}

    fired_outcomes: list[ProactiveCheckOutcome] = []

    def on_fire() -> None:
        outcome = asyncio.run(
            run_proactive_check(
                "check saved file",
                chains_config=chains_config,
                registry=registry,
                queue=DelegateQueue(),
                client_factory=_factory({candidate.name: client}),
            )
        )
        fired_outcomes.append(outcome)

    scheduler = FakeScheduler()
    watcher = DebouncedSaveWatcher(0.5, on_fire, scheduler=scheduler)

    watcher.on_save()
    assert watcher.pending is True
    assert client.calls == 0  # still debouncing -- nothing fired yet
    assert fired_outcomes == []

    scheduler.fire_latest()  # simulate the debounce window elapsing

    assert watcher.pending is False
    assert client.calls == 1
    assert len(fired_outcomes) == 1
    outcome = fired_outcomes[0]
    assert outcome.skipped_no_local_candidate is False
    assert outcome.delegate_result.result == "looks fine"


def test_rapid_consecutive_saves_collapse_into_a_single_scheduled_check():
    """Each `on_save()` cancels the previous pending timer and schedules a
    fresh one -- rapid saves never fire more than one check."""
    scheduler = FakeScheduler()
    fire_count = 0

    def on_fire() -> None:
        nonlocal fire_count
        fire_count += 1

    watcher = DebouncedSaveWatcher(0.5, on_fire, scheduler=scheduler)

    watcher.on_save()
    watcher.on_save()
    watcher.on_save()

    assert len(scheduler.scheduled) == 3
    assert len(scheduler.cancelled) == 2  # the first two were cancelled by the next save

    scheduler.fire_latest()  # only the last one is still live
    assert fire_count == 1


# --- Test scenario: a commit-triggered check with no serious finding
# never invokes Claude Code -----------------------------------------------


@pytest.mark.asyncio
async def test_commit_triggered_check_with_no_serious_finding_never_invokes_claude_code():
    candidate = _candidate("local-good")
    client = FakeGoodClient(result="all clear, no issues found", caveats="none")
    invoker = FakeClaudeCodeInvoker()

    outcome = await run_proactive_check(
        "post-commit re-evaluation",
        chains_config=_chains_config([candidate.name]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        client_factory=_factory({candidate.name: client}),
        claude_code_invoker=invoker,
    )

    assert outcome.skipped_no_local_candidate is False
    assert outcome.serious is False
    assert outcome.claude_code_result is None
    assert invoker.calls == 0


# --- Test scenario: a serious finding invokes the full Claude Code loop -


@pytest.mark.asyncio
async def test_serious_finding_invokes_full_claude_code_loop():
    candidate = _candidate("local-good")
    client = FakeGoodClient(
        result="found a hardcoded API key committed in config.py",
        caveats="this looks like a leaked credential -- a security vulnerability",
    )
    invoker = FakeClaudeCodeInvoker(fake_result="claude-code-ran")

    outcome = await run_proactive_check(
        "post-commit re-evaluation",
        chains_config=_chains_config([candidate.name]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        client_factory=_factory({candidate.name: client}),
        claude_code_invoker=invoker,
    )

    assert outcome.serious is True
    assert invoker.calls == 1
    assert invoker.last_finding is outcome.delegate_result
    assert outcome.claude_code_result == "claude-code-ran"


def test_default_severity_classifier_is_a_documented_keyword_heuristic():
    """Sanity check on the heuristic's shape -- an innocuous finding isn't
    accidentally flagged serious, and a security-flavored one is."""
    from josu.delegate.local_model import DelegateResult

    innocuous = DelegateResult(result="renamed a variable for clarity", caveats="", model="m")
    assert default_severity_classifier(innocuous) is False

    serious = DelegateResult(
        result="unchanged", caveats="potential data loss on this code path", model="m"
    )
    assert default_severity_classifier(serious) is True


# --- Test scenario: no event and no on-demand query means no scan
# (Covers R17) -------------------------------------------------------------


def test_no_event_and_no_on_demand_query_means_no_scan():
    """Literally nothing happens with the passage of time alone: a watcher
    is constructed, real wall-clock time passes, `on_save()` is never
    called, and `query_on_demand` is never invoked -- no timer is ever
    scheduled and the check function is never called."""
    scheduler = FakeScheduler()
    fire_calls: list[int] = []
    DebouncedSaveWatcher(0.01, lambda: fire_calls.append(1), scheduler=scheduler)

    time.sleep(0.05)

    assert scheduler.scheduled == []
    assert fire_calls == []


@pytest.mark.asyncio
async def test_query_on_demand_is_the_same_function_and_independently_callable():
    """R16: `query_on_demand` bypasses the commit/save event triggers
    entirely -- it's directly callable any time, with no watcher or hook
    state involved."""
    assert query_on_demand is run_proactive_check

    candidate = _candidate("local-good")
    client = FakeGoodClient(result="queried directly")

    outcome = await query_on_demand(
        "developer-initiated query",
        chains_config=_chains_config([candidate.name]),
        registry={candidate.name: candidate},
        queue=DelegateQueue(),
        client_factory=_factory({candidate.name: client}),
    )

    assert outcome.delegate_result.result == "queried directly"


# --- Test scenario: a save event with no local candidate available is
# skipped rather than falling to remote (Covers R39) -----------------------


@pytest.mark.asyncio
async def test_save_event_with_only_remote_candidate_configured_is_skipped_not_called():
    """Fixture chain config with ONLY a remote candidate configured for
    `proactive_check` -- even with `allow_remote=True` set globally, the
    check must be skipped for this event, and the remote candidate must
    never be touched."""
    remote_candidate = _candidate("remote-only", local=False)
    remote_client = FakeGoodClient(result="should never be seen")

    chains_config = _chains_config(["remote-only"], allow_remote=True)

    outcome = await run_proactive_check(
        "debounced save re-evaluation",
        chains_config=chains_config,
        registry={"remote-only": remote_candidate},
        queue=DelegateQueue(),
        client_factory=_factory({"remote-only": remote_client}),
    )

    assert outcome.skipped_no_local_candidate is True
    assert outcome.delegate_result is None
    assert outcome.serious is False
    assert remote_client.calls == 0  # never touched


def test_local_only_projection_excludes_remote_even_with_allow_remote_true():
    """Unit-level proof of the R39 by-construction guarantee: even when
    the source config lists a remote candidate FIRST and sets
    `allow_remote = true`, the projected registry/config `execute_chain()`
    actually sees contains only the local candidate."""
    local_candidate = _candidate("local-c", local=True)
    remote_candidate = _candidate("remote-c", local=False)
    chains_config = _chains_config(["remote-c", "local-c"], allow_remote=True)
    registry = {"local-c": local_candidate, "remote-c": remote_candidate}

    projected_config, projected_registry = _local_only_execution_inputs(chains_config, registry)

    assert projected_config is not None
    assert projected_config.allow_remote is False
    assert set(projected_registry) == {"local-c"}


def test_local_only_projection_returns_none_when_no_local_candidate_exists():
    remote_candidate = _candidate("remote-only", local=False)
    chains_config = _chains_config(["remote-only"], allow_remote=True)

    projected_config, projected_registry = _local_only_execution_inputs(
        chains_config, {"remote-only": remote_candidate}
    )

    assert projected_config is None
    assert projected_registry == {}


# --- Test scenario: `josu init` on a repo with an existing post-commit
# hook chains to it ---------------------------------------------------------


def test_install_commit_hook_chains_to_existing_post_commit_hook(tmp_path):
    repo = _init_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    existing_hook.write_text("#!/bin/sh\necho 'husky ran'\n", encoding="utf-8")
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IEXEC)

    result = install_commit_hook(repo, python_executable="/usr/bin/python3")

    assert result.chained_existing is True
    assert result.already_installed is False

    content = existing_hook.read_text(encoding="utf-8")
    assert "echo 'husky ran'" in content  # original preserved, not overwritten
    assert "josu.proactive.watchers" in content  # josu's block appended
    assert content.index("echo 'husky ran'") < content.index("josu.proactive.watchers")
    assert os.access(existing_hook, os.X_OK)


def test_install_commit_hook_writes_fresh_hook_when_none_exists(tmp_path):
    repo = _init_repo(tmp_path)

    result = install_commit_hook(repo, python_executable="/usr/bin/python3")

    assert result.chained_existing is False
    assert result.already_installed is False
    content = result.hook_path.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh")
    assert "josu.proactive.watchers" in content
    assert os.access(result.hook_path, os.X_OK)


def test_install_commit_hook_is_idempotent_on_repeated_run(tmp_path):
    repo = _init_repo(tmp_path)

    first = install_commit_hook(repo, python_executable="/usr/bin/python3")
    second = install_commit_hook(repo, python_executable="/usr/bin/python3")

    assert first.chained_existing is False
    assert second.already_installed is True
    content = first.hook_path.read_text(encoding="utf-8")
    assert content.count("josu.proactive.watchers") == 1  # not duplicated


def test_install_commit_hook_aborts_on_unsafe_existing_hook(tmp_path):
    """An existing hook containing a bare `exec` (which would silently
    swallow anything josu appends after it) can't be safely chained to --
    installation aborts with a clear error and leaves the hook untouched."""
    repo = _init_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    original_content = "#!/bin/sh\nexec somecommand --replace-this-process\n"
    existing_hook.write_text(original_content, encoding="utf-8")
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(HookInstallationError):
        install_commit_hook(repo, python_executable="/usr/bin/python3")

    assert existing_hook.read_text(encoding="utf-8") == original_content  # untouched


def test_install_commit_hook_aborts_on_hook_with_no_recognized_shebang(tmp_path):
    repo = _init_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    existing_hook.write_text("some-custom-binary-wrapper --run\n", encoding="utf-8")
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(HookInstallationError):
        install_commit_hook(repo, python_executable="/usr/bin/python3")


def test_install_commit_hook_requires_a_git_repository(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    with pytest.raises(HookInstallationError):
        install_commit_hook(not_a_repo)
