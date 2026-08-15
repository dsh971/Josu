"""Orchestrator main loop -- `josu run <task>` (U13).

Composes U4/U5/U6/U10's already-tested functions into one end-to-end task
run. Introduces no new orchestration logic of its own beyond the composition
itself and the run-record bookkeeping (`RunRecord.record_outcome()`,
`observability/runlog.py`).

Composition order is load-bearing, not stylistic (see the plan's revised
U13 Approach, and `merge.py`'s own module docstring, which is the authority
this follows):

1. `worktree.create_worktree()` -- an isolated worktree, seeded from
   `repo_root`'s current state.
2. `merge.snapshot_repo()` -- called IMMEDIATELY after, before anything else
   touches `repo_root`. This is the "before" baseline R21's divergence check
   compares against. If this were captured any later (e.g. after the
   adapter runs), a concurrent edit made *during* the adapter run would be
   silently baked into the baseline and never detected as diverged -- see
   `merge.py`'s module docstring and `test_run.py`'s
   `test_concurrent_edit_made_while_the_adapter_is_running_is_caught_as_diverged`,
   which exercises exactly this timing.
3. The per-worktree MCP manifest -- generated (`mcp_manifest.write_manifest()`)
   then validated (`mcp_manifest.validate_manifest()`) before the adapter
   ever sees it.
4. The circuit-breaker-wrapped adapter invocation
   (`circuit_breaker.run_under_circuit_breaker()` wrapping the resolved
   adapter's `run()`, e.g. `adapters.claude_code.run()`).
5. On success, the diff is surfaced for developer review
   (`merge.diff_for_review()`) and the merge is gated on the caller-supplied
   `approve()` callback's answer (`merge.merge()`). The worktree is removed
   (`worktree.remove_worktree()`) immediately after a successful merge, or
   right after a developer-rejected diff is determined -- both are terminal
   "nothing more to do with this worktree" states, per the plan's
   "auto-delete on merge/rejection only" requirement. A circuit-breaker
   timeout or any other unexpected exception deliberately does NOT remove
   the worktree -- U8's crash-recovery/cleanup path (`josu cleanup`) is
   what surfaces and disposes of those, never a silent discard here.

josu's own merge-triggered reindex call was retired (gortex integration
rework, R4): a configured gortex's own continuous watcher is the sole
reindex trigger once a repo is tracked, so there is no reindex step here
anymore -- `run_task()` ends at the merge/rejection worktree cleanup
described above.

A `RunRecord` (U6) is created up front and saved via `observability.runlog.
save_run()` in a `finally` block, so success, a developer-rejected diff, a
circuit-breaker timeout, a merge-conflict-abort, or any other unexpected
exception all produce a saved record -- never just the happy path.

Requires the daemon already running (the adapter's MCP manifest points at
it) -- checked FIRST, before any worktree/git work, so an unreachable daemon
fails with a clear, actionable message rather than leaving a worktree behind
for no reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from josu.config import JosuConfig
from josu.config.orchestrator import OrchestratorAdapterConfig, usable_adapters
from josu.daemon import DEFAULT_HOST, DEFAULT_PORT
from josu.daemon_auth import resolve_daemon_token
from josu.delegate.daemon_client import DaemonNotReachableError, check_daemon_reachable
from josu.observability.runlog import (
    RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT,
    RUN_OUTCOME_DIVERGED,
    RUN_OUTCOME_ERROR,
    RUN_OUTCOME_MERGED,
    RUN_OUTCOME_REJECTED,
    CircuitBreakerEvent,
    RunRecord,
    default_runlog_dir,
    new_run_id,
    save_run,
)
from josu.orchestrator.adapters import claude_code
from josu.orchestrator.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerTimeoutError,
    run_under_circuit_breaker,
)
from josu.orchestrator.merge import (
    DivergedWorkingTreeError,
    MergeResult,
    RepoSnapshot,
    diff_for_review,
    merge,
    snapshot_repo,
)
from josu.orchestrator.mcp_manifest import MCPManifest, validate_manifest, write_manifest
from josu.orchestrator.worktree import (
    Worktree,
    create_worktree,
    default_worktrees_dir,
    remove_worktree,
)

# The adapter dispatch table: `adapter_name` -> the module-level `run()`
# callable that actually invokes that CLI. Only Claude Code exists today
# (see plan Risks & Dependencies: "the config-driven orchestrator adapter
# mechanism is validated against exactly one real CLI in v1") -- a second
# adapter registers itself here alongside its own `[[orchestrator.adapters]]`
# TOML entry name once it clears the MCP-approval gate. Accepts both the
# underscore and hyphen spellings so `adapter_name="claude_code"` (this
# module's own default/plan-literal spelling) and `OrchestratorAdapterConfig.
# name == "claude-code"` (`adapters/claude_code.py`'s `ADAPTER_NAME`) both
# resolve to the same entry without a caller having to know which spelling
# the config file happens to use.
_ADAPTER_RUN_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "claude_code": claude_code.run,
    "claude-code": claude_code.run,
}


class NoUsableAdapterError(RuntimeError):
    """Raised when no usable (parsed AND `mcp_approval_verified`-attested)
    `[[orchestrator.adapters]]` entry matches `adapter_name`, or when
    `adapter_name` names an adapter this module has no dispatch function
    for."""

    def __init__(self, adapter_name: str) -> None:
        self.adapter_name = adapter_name
        super().__init__(
            f"no usable orchestrator adapter named {adapter_name!r} configured -- add an "
            "[[orchestrator.adapters]] entry to josu.toml with mcp_approval_verified.verified "
            "= true, or check the name against what's configured"
        )


@dataclass
class RunTaskResult:
    """Everything `josu run`'s CLI subcommand (or any other caller) needs to
    report a run's conclusion -- the saved `RunRecord` itself, plus the
    richer in-process objects (`Worktree`, `MergeResult`) `RunRecord`'s own
    sanitized/summarized fields don't carry (see `runlog.py`'s module
    docstring on why records never carry raw payloads)."""

    run_id: str
    record: RunRecord
    outcome: str
    worktree: Worktree | None = None
    diff: str | None = None
    merge_result: MergeResult | None = None
    error: Exception | None = None


def _resolve_adapter_config(config: JosuConfig, adapter_name: str) -> OrchestratorAdapterConfig:
    """Find the usable (R37-attested) `[[orchestrator.adapters]]` entry
    matching `adapter_name`, accepting either the underscore or hyphen
    spelling (see `_ADAPTER_RUN_FUNCTIONS`'s docstring)."""
    normalized = adapter_name.replace("_", "-")
    usable, _warnings = usable_adapters(config.orchestrator)
    for adapter in usable:
        if adapter.name in (adapter_name, normalized):
            return adapter
    raise NoUsableAdapterError(adapter_name)


def run_task(
    task: str,
    *,
    config: JosuConfig,
    repo_root: Path,
    approve: Callable[[str], bool],
    worktrees_dir: Path | None = None,
    manifests_dir: Path | None = None,
    runlog_dir: Path | None = None,
    adapter_name: str = "claude_code",
    adapter_run: Callable[..., Any] | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    task_description: str | None = None,
) -> RunTaskResult:
    """Run `task` against `repo_root` end-to-end: worktree -> snapshot ->
    MCP manifest -> circuit-breaker-wrapped adapter invocation -> diff
    review/merge -> reindex. See module docstring for why this exact order
    matters.

    `approve` is called with the surfaced diff text and must return `True`
    to merge, `False` to reject -- kept as a caller-supplied callback so this
    function has no I/O of its own (the CLI's `y/n` prompt lives in
    `cli.py`, not here); a test can pass a canned `lambda diff: True/False`.

    `adapter_run` is a test seam: when given, it's called directly instead
    of resolving one from `_ADAPTER_RUN_FUNCTIONS` via `adapter_name` --
    lets a test substitute a hand-written fake at the adapter-invocation
    boundary (matching `tests/orchestrator/test_claude_code.py`'s
    fake-executable convention, applied at the boundary THIS module's own
    tests care about) without spawning a real `claude` subprocess. The
    matching `OrchestratorAdapterConfig` entry (`adapter_name`) is still
    resolved from `config` either way, since `adapter_run` callables share
    the same `(adapter, *, worktree, manifest, config_path, task, timeout)`
    signature `adapters.claude_code.run()` has.

    Raises `DaemonNotReachableError`/`NoUsableAdapterError` directly (no
    `RunRecord` is created or saved for either) -- both are checked before
    any worktree/git work happens, so there's nothing yet to record.
    """
    check_daemon_reachable(host, port)
    daemon_token = resolve_daemon_token(config.path)
    adapter = _resolve_adapter_config(config, adapter_name)
    run_fn = adapter_run if adapter_run is not None else _ADAPTER_RUN_FUNCTIONS.get(
        adapter_name, _ADAPTER_RUN_FUNCTIONS.get(adapter_name.replace("_", "-"))
    )
    if run_fn is None:
        raise NoUsableAdapterError(adapter_name)

    resolved_worktrees_dir = worktrees_dir or default_worktrees_dir(repo_root)
    resolved_runlog_dir = runlog_dir or default_runlog_dir(repo_root)

    record = RunRecord(run_id=new_run_id(), task_description=task_description or task)

    worktree: Worktree | None = None
    snapshot: RepoSnapshot | None = None
    manifest: MCPManifest | None = None
    diff: str | None = None
    merge_result: MergeResult | None = None
    outcome = RUN_OUTCOME_ERROR
    error: Exception | None = None

    try:
        # P3 fix (U13/U14 Tier 2 review): steps 1-3 (worktree creation, the
        # R21 snapshot, MCP manifest generation+validation) used to run with
        # NO timeout coverage at all -- only step 4 (the adapter invocation)
        # was wrapped by the circuit breaker. `worktree.py`'s/`merge.py`'s
        # own git subprocess calls had no `timeout=` passed to
        # `subprocess.run` whatsoever, so a git lock-contention hang during
        # worktree creation could hang the WHOLE `josu run` invocation
        # indefinitely, well before the adapter step's own circuit-breaker
        # wrapping ever got a chance to matter.
        #
        # Fix: create ONE `CircuitBreaker` up front and wrap BOTH this
        # prep sequence and the step-4 adapter call (below) with it, rather
        # than giving steps 1-3 their own separate, smaller timeout.
        # `run_under_circuit_breaker()`'s `breaker.start()` is idempotent,
        # so the two calls share one continuous wall-clock budget: prep
        # time is deliberately counted AGAINST the same budget the adapter
        # gets, not on top of it -- a slow/hanging worktree/snapshot/
        # manifest step now eats into the adapter's own effective timeout
        # exactly the way a slow adapter step already would, rather than
        # silently getting its own separate allowance. This was chosen over
        # a second, independent timeout around steps 1-3 because
        # `config.wall_clock_timeout_seconds` is documented (R23) as THE
        # single whole-run budget -- introducing a second, separate knob
        # here would fragment that single-budget contract for no clear
        # benefit, and every existing test configures a generous enough
        # budget that real (non-hanging) worktree/snapshot/manifest prep
        # time is negligible against it.
        breaker = CircuitBreaker(config.wall_clock_timeout_seconds)
        try:

            def _prepare_worktree_snapshot_and_manifest(remaining: float) -> None:
                nonlocal worktree, snapshot, manifest
                # Step 1: an isolated worktree, seeded from repo_root's
                # current state. `timeout=remaining` bounds each of its own
                # (up to three) git subprocess calls -- see
                # `worktree.py`'s `create_worktree()`/`_run_git()` docstrings.
                worktree = create_worktree(
                    repo_root,
                    resolved_worktrees_dir,
                    task_description=task_description or task,
                    timeout=remaining,
                )

                # Step 2 -- MUST happen immediately after worktree creation,
                # before anything else runs. See module docstring.
                snapshot = snapshot_repo(
                    worktree.repo_root, stash_ref=worktree.stash_ref, timeout=remaining
                )

                # Step 3: the per-worktree MCP manifest, generated then
                # validated -- pure file I/O, no subprocess/timeout
                # plumbing needed.
                manifest = write_manifest(
                    worktree.path, host, port, daemon_token, manifests_dir=manifests_dir
                )
                validate_manifest(manifest)

            run_under_circuit_breaker(breaker, _prepare_worktree_snapshot_and_manifest)

            # Step 4: the circuit-breaker-wrapped adapter invocation, reusing
            # the SAME `breaker` (and therefore the budget steps 1-3 above
            # already started consuming).
            #
            # The return value (a `ClaudeCodeRunResult`-shaped object) is
            # deliberately not captured here -- its only role in this
            # composition is proving the invocation completed without
            # raising; the diff surfaced to the developer comes from the
            # dedicated `diff_for_review()` call in step 5 below (per the
            # plan's Approach), not from re-reading this result's own
            # `.diff` field.
            run_under_circuit_breaker(
                breaker,
                lambda remaining: run_fn(
                    adapter,
                    worktree=worktree,
                    manifest=manifest,
                    config_path=config.path,
                    task=task,
                    timeout=remaining,
                ),
            )
        except CircuitBreakerTimeoutError as exc:
            record.record_circuit_breaker_event(CircuitBreakerEvent.from_timeout_error(exc))
            outcome = RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT
            error = exc
            # The worktree is deliberately left in place, NOT removed here --
            # U8's crash-recovery/cleanup path (`josu cleanup`) is what
            # surfaces and disposes of it, never a silent discard on this
            # path.
            return RunTaskResult(
                run_id=record.run_id,
                record=record,
                outcome=outcome,
                worktree=worktree,
                error=error,
            )
        # Step 5: surface the diff for developer review, gated on explicit
        # approval before any write to repo_root.
        diff = diff_for_review(worktree)
        approved = approve(diff)

        try:
            merge_result = merge(worktree, snapshot, approved=approved)
        except DivergedWorkingTreeError as exc:
            outcome = RUN_OUTCOME_DIVERGED
            error = exc
            return RunTaskResult(
                run_id=record.run_id,
                record=record,
                outcome=outcome,
                worktree=worktree,
                diff=diff,
                error=error,
            )

        if not merge_result.merged:
            outcome = RUN_OUTCOME_REJECTED
            # Terminal state (developer rejected the diff) -- auto-delete
            # the worktree now, matching the plan's "auto-delete on
            # merge/rejection only" requirement (see module docstring).
            # `force=True`: the worktree carries the adapter's rejected
            # (never merged) edits, uncommitted -- `git worktree remove`
            # refuses a dirty worktree without `--force`.
            remove_worktree(worktree, force=True)
            return RunTaskResult(
                run_id=record.run_id,
                record=record,
                outcome=outcome,
                worktree=worktree,
                diff=diff,
                merge_result=merge_result,
            )

        outcome = RUN_OUTCOME_MERGED
        # Terminal state (merged) -- auto-delete the worktree now, matching
        # the plan's "auto-delete on merge/rejection only" requirement (see
        # module docstring). `force=True`: the worktree always carries
        # uncommitted changes at this point (the adapter's own edits) --
        # that's expected, not a sign anything is wrong, since its diff has
        # already been applied to `repo_root` by `merge()` above; `git
        # worktree remove` refuses a dirty worktree without `--force`.
        remove_worktree(worktree, force=True)
        return RunTaskResult(
            run_id=record.run_id,
            record=record,
            outcome=outcome,
            worktree=worktree,
            diff=diff,
            merge_result=merge_result,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failure still must save a record
        outcome = RUN_OUTCOME_ERROR
        error = exc
        raise
    finally:
        # P1 fix: `RUN_OUTCOME_ERROR` previously saved with no diagnostic
        # detail at all -- populate `error_class`/`error_message` from the
        # exception this block's own `except` clause caught, so `josu log
        # <run_id>` has something to show beyond a bare `outcome: error`.
        # Only set for the actual error outcome -- the other outcomes
        # (`DIVERGED`, `CIRCUIT_BREAKER_TIMEOUT`) already have their own
        # dedicated fields (`diverged_paths`, `circuit_breaker_events`) for
        # this same "what happened" detail.
        is_error_outcome = outcome == RUN_OUTCOME_ERROR and error is not None
        record.record_outcome(
            outcome,
            worktree_path=str(worktree.path) if worktree is not None else None,
            diverged_paths=list(getattr(error, "diverged_paths", None) or []),
            error_class=type(error).__name__ if is_error_outcome else None,
            error_message=str(error) if is_error_outcome else None,
        )
        save_run(record, resolved_runlog_dir)
