"""josu CLI entry point.

Subcommands are added incrementally as their owning implementation unit
lands (see docs/plans/2026-07-21-001-feat-hybrid-local-hosted-coding-agent-plan.md).
`daemon` ships with U1 since daemon.py has to be reachable somehow; `init`
lands with U9 (`proactive/watchers.py`'s `install_commit_hook()` -- chains
to an existing `post-commit` hook rather than overwriting it); `run` and
`models` land with their respective (not-yet-built) units.
`delegate` lands with U7 -- a developer-initiated escape hatch for routing
a bounded task directly to the local delegate worker (`fallback/quota.py`,
`delegate/chain.py`'s `execute_chain()`) when Claude Code is quota/rate-
limit exhausted, bypassing the hosted orchestrator entirely. `cleanup`
lands with U8 -- crash recovery and disk-usage bounding for orchestrator
worktrees (`orchestrator/worktree.py`'s crash-orphan detection,
`fallback/quota.py`'s abandonment-marker mechanism reused rather than
duplicated).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from josu.config import resolve_config_path
from josu.config.chains import DELEGABLE_TASK_TYPES
from josu.daemon import DEFAULT_HOST, DEFAULT_PORT


# --- U8: crash recovery and cleanup -----------------------------------------
#
# `scan_for_crash_orphaned_worktrees()` is the one place that knows about
# BOTH `orchestrator/worktree.py`'s read-only orphan detection AND
# `fallback/quota.py`'s abandonment-marker mechanism -- neither of those
# two modules imports the other (quota.py already imports `Worktree` from
# worktree.py, so the reverse would be circular), so the actual
# cross-referencing has to happen here.


def _scan_and_collect_abandoned_worktrees(
    repo_root: Path,
    worktrees_dir: Path,
    *,
    abandoned_dir: Path | None = None,
) -> tuple[list[str], list]:
    """Shared implementation behind `scan_for_crash_orphaned_worktrees()`
    and `abandoned_worktree_report()`: read `abandoned_dir`'s marker files
    ONCE, cross-reference against `git worktree list --porcelain` to find
    new crash orphans, mark any found, and return both the newly-marked
    names and the full current record list -- so a caller that needs both
    (`abandoned_worktree_report`) never has to re-glob/re-parse the same
    marker directory a second time to get what it already read here.

    When nothing new was marked, the records read at the top are still the
    complete, up-to-date set (nothing on disk changed), so they're reused
    as-is. Only when new orphans WERE marked (disk state changed) is a
    second read performed, to pick up exactly those new markers.
    """
    from josu.fallback.quota import (
        ABANDON_REASON_CRASH_ORPHANED,
        default_abandoned_worktrees_dir,
        list_abandoned_worktrees,
        mark_worktree_abandoned,
    )
    from josu.orchestrator.worktree import find_orphaned_worktrees

    resolved_abandoned_dir = abandoned_dir or default_abandoned_worktrees_dir(repo_root)
    existing_records = list_abandoned_worktrees(resolved_abandoned_dir)
    known_names = {Path(record.worktree_path).name for record in existing_records}

    orphans = find_orphaned_worktrees(
        repo_root, worktrees_dir, known_abandoned_names=known_names
    )
    for worktree in orphans:
        mark_worktree_abandoned(
            worktree,
            reason=ABANDON_REASON_CRASH_ORPHANED,
            abandoned_dir=resolved_abandoned_dir,
        )

    newly_orphaned_names = [worktree.path.name for worktree in orphans]
    if not orphans:
        return newly_orphaned_names, existing_records
    return newly_orphaned_names, list_abandoned_worktrees(resolved_abandoned_dir)


def scan_for_crash_orphaned_worktrees(
    repo_root: Path,
    worktrees_dir: Path,
    *,
    abandoned_dir: Path | None = None,
) -> list[str]:
    """The startup half of U8: cross-reference `git worktree list
    --porcelain` (via `orchestrator/worktree.py`'s `find_orphaned_
    worktrees()`) against `fallback/quota.py`'s abandonment-marker state,
    and mark every newly-found orphan abandoned via that SAME mechanism
    (`mark_worktree_abandoned()`, `reason=ABANDON_REASON_CRASH_ORPHANED`)
    -- reused, not duplicated, so `josu cleanup` sees one unified list
    regardless of whether a worktree was abandoned due to quota exhaustion
    (U7) or a crash (U8).

    Never resumes, merges, or removes anything -- an orphan is only ever
    surfaced (marked abandoned, discoverable via `josu cleanup`), matching
    the plan's explicit "no automatic background sweep" and "surfaced ...
    not silently resumed" requirements. Meant to be called once, at
    daemon/CLI startup (`_cmd_daemon_start`, `_cmd_cleanup` below) -- there
    is no standing watcher.

    Returns the worktree directory names newly marked abandoned by this
    call (empty if nothing new was found).
    """
    names, _records = _scan_and_collect_abandoned_worktrees(
        repo_root, worktrees_dir, abandoned_dir=abandoned_dir
    )
    return names


def abandoned_worktree_report(
    repo_root: Path,
    *,
    worktrees_dir: Path | None = None,
    abandoned_dir: Path | None = None,
) -> list:
    """Run U8's crash-recovery scan, then return every currently-known
    abandoned worktree record -- both U7's quota-exhaustion abandonments
    and U8's crash orphans, indistinguishable in shape (only `.reason`
    differs), exactly what `josu cleanup` lists.

    Reuses `_scan_and_collect_abandoned_worktrees()`'s already-loaded
    records instead of re-globbing/re-parsing `abandoned_dir` a second time
    after the scan already read it once.
    """
    from josu.orchestrator.worktree import default_worktrees_dir

    resolved_worktrees_dir = worktrees_dir or default_worktrees_dir(repo_root)

    _names, records = _scan_and_collect_abandoned_worktrees(
        repo_root, resolved_worktrees_dir, abandoned_dir=abandoned_dir
    )
    return records


def _format_age(age: timedelta) -> str:
    """A short, human-readable rendering of `age` for `josu cleanup`'s
    listing -- e.g. `"3m12s"`, `"5h2m"`, `"2d1h"`. A negative age (a clock
    skew edge case, never expected in practice) floors to `"0s"` rather
    than printing a confusing negative duration."""
    total_seconds = max(int(age.total_seconds()), 0)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def render_abandoned_record(record) -> str:
    """One abandoned worktree's record rendered as a single detail line for
    `josu cleanup` -- its directory name, why it was abandoned, how long
    ago that was detected, its branch, its task description (only if one
    was available -- U8's crash orphans never have one, see
    `orchestrator/worktree.py`'s `Worktree.task_description` docstring),
    and its full path -- enough for a developer to decide whether to
    remove it."""
    detected_at = datetime.fromisoformat(record.detected_at)
    age = datetime.now(timezone.utc) - detected_at

    parts = [
        Path(record.worktree_path).name,
        f"reason={record.reason}",
        f"age={_format_age(age)}",
        f"branch={record.branch}",
    ]
    if record.task_description:
        parts.append(f"task={record.task_description!r}")
    parts.append(f"path={record.worktree_path}")
    return " ".join(parts)


def remove_abandoned_worktree(
    record,
    *,
    abandoned_dir: Path,
    force: bool = False,
) -> None:
    """The developer-gated removal `josu cleanup --remove NAME` performs on
    one abandoned worktree record: remove it cleanly via `orchestrator/
    worktree.py`'s `remove_worktree()` (i.e. `git worktree remove`, never a
    raw `rm -rf`), then clear its marker file so it stops showing up in
    future `josu cleanup` listings.

    If `remove_worktree()` fails (`WorktreeError` -- e.g. the path was
    already removed by hand, or git reports uncommitted changes and
    `force` wasn't passed), the exception propagates and the marker is
    left in place, so the record isn't silently lost.
    """
    from josu.orchestrator.worktree import Worktree, remove_worktree

    worktree = Worktree(
        path=Path(record.worktree_path),
        branch=record.branch,
        repo_root=Path(record.repo_root),
        stash_ref=None,
    )
    remove_worktree(worktree, force=force)

    marker_path = abandoned_dir / f"{Path(record.worktree_path).name}.json"
    marker_path.unlink(missing_ok=True)


def _cmd_daemon_start(args: argparse.Namespace) -> int:
    from josu.daemon import run
    from josu.orchestrator.worktree import default_worktrees_dir

    target = Path(args.target) if args.target else Path.cwd()
    config_path = Path(args.config) if args.config else resolve_config_path()

    # U8: cross-reference git's own worktree bookkeeping against
    # abandonment-marker state once, at startup, before serving anything --
    # a worktree left behind by a prior crash is surfaced here, never
    # silently resumed or discarded.
    newly_orphaned = scan_for_crash_orphaned_worktrees(target, default_worktrees_dir(target))
    if newly_orphaned:
        print(
            f"Detected {len(newly_orphaned)} worktree(s) orphaned by a prior crash "
            f"(no matching completed/rejected/abandoned record): "
            f"{', '.join(sorted(newly_orphaned))}. Not resuming automatically -- "
            "run `josu cleanup` to review."
        )

    print(
        f"Starting josu daemon on {args.host}:{args.port} "
        f"(target: {target}, config: {config_path})"
    )
    # josu never spawns gortex -- `run()` only connects to whatever
    # `[[graph.engines]]` target is configured, degrading to no graph
    # engine (never raising) when it's absent, unreachable, or
    # incompatible. See daemon.py's `_resolve_graph_engine_target()`.
    run(host=args.host, port=args.port, target=target, config_path=config_path)
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """U9's `josu init`: install the `post-commit` hook that drives
    commit-triggered proactive checks (R15), via `proactive/watchers.py`'s
    `install_commit_hook()`. Detects an existing hook (Husky, `pre-commit`,
    or a hand-written script) and chains to it rather than overwriting it;
    aborts with a clear warning instead of clobbering existing tooling if
    that can't be done safely.
    """
    from josu.proactive.watchers import HookInstallationError, install_commit_hook

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()

    try:
        result = install_commit_hook(repo_root)
    except HookInstallationError as exc:
        print(f"josu init: {exc}")
        return 1

    if result.already_installed:
        print(f"josu init: post-commit hook already installed at {result.hook_path}")
    elif result.chained_existing:
        print(f"josu init: chained to existing post-commit hook at {result.hook_path}")
    else:
        print(f"josu init: installed post-commit hook at {result.hook_path}")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    from josu.observability.runlog import (
        RunNotFoundError,
        default_runlog_dir,
        latest_run,
        load_run,
        render_run,
    )

    runlog_dir = Path(args.runlog_dir) if args.runlog_dir else default_runlog_dir(Path.cwd())

    if args.run_id:
        try:
            record = load_run(args.run_id, runlog_dir)
        except RunNotFoundError as exc:
            print(str(exc))
            return 1
    else:
        # `latest_run()` returns the id AND the already-parsed record
        # together, so the no-run-id path doesn't call `load_run()` again
        # and parse the same file a second time.
        found = latest_run(runlog_dir)
        if found is None:
            print(f"No run log entries found under {runlog_dir}")
            return 1
        _, record = found

    print(render_run(record))
    return 0


def _cmd_delegate(args: argparse.Namespace) -> int:
    """U7's direct-request escape hatch: route a bounded `task_type` straight
    to the local delegate worker. Meant for a developer to invoke by hand
    once they've noticed (via `josu log`, or Claude Code simply refusing to
    run) that Claude Code is quota/rate-limit exhausted.

    U14: this no longer constructs its own `DelegateQueue`/calls
    `delegate/chain.py`'s `execute_chain()` directly -- that was a SECOND,
    unshared queue living in this CLI process, independent of the daemon's
    own, defeating the "one shared queue serializes every delegate call"
    invariant. Instead this is now an `httpx` client of the daemon's
    `/delegate/internal` endpoint (`delegate/internal_api.py`), via the
    shared `delegate/daemon_client.py` helper (simplify pass after U13/U14:
    this used to hand-roll its own `httpx.AsyncClient` +
    `except httpx.TransportError` block, duplicated near-identically in
    `orchestrator/run.py` and `proactive/watchers.py`). If the daemon isn't
    reachable, this fails with a clear, actionable message -- never a stack
    trace, and never a silent fallback to a local queue (that fallback
    would silently reintroduce the exact bug this unit fixes).

    R28/R29's bounded-task-type gate (`fallback/quota.py`'s
    `TaskNotBoundedError`) is still enforced here, client-side, before any
    network call is made -- a non-bounded `task_type` is refused
    immediately rather than round-tripping to the daemon for nothing.
    """
    import asyncio

    from josu.daemon import DEFAULT_HOST, DEFAULT_PORT
    from josu.daemon_auth import resolve_daemon_token
    from josu.delegate.daemon_client import (
        DaemonNotReachableError,
        DelegateInternalError,
        post_delegate_internal,
    )
    from josu.fallback.quota import HOSTED_PAUSED_NOTICE, TaskNotBoundedError

    if args.task_type not in DELEGABLE_TASK_TYPES:
        print(str(TaskNotBoundedError(args.task_type)))
        return 1

    host = args.host or DEFAULT_HOST
    port = args.port or DEFAULT_PORT
    base_url = f"http://{host}:{port}"
    config_path = Path(args.config) if args.config else resolve_config_path()

    try:
        payload = asyncio.run(
            post_delegate_internal(
                base_url,
                {"task_type": args.task_type, "task": args.task},
                token=resolve_daemon_token(config_path),
            )
        )
    except DaemonNotReachableError as exc:
        print(f"josu delegate: {exc}")
        return 1
    except DelegateInternalError as exc:
        print(f"josu delegate: {exc.error}: {exc.detail}")
        return 1

    print(HOSTED_PAUSED_NOTICE)
    print(payload["result"])
    if payload.get("caveats"):
        print(f"caveats: {payload['caveats']}")
    return 0


def _prompt_diff_approval(diff: str) -> bool:
    """The default `approve` callback for `josu run` (U13): print the
    surfaced diff and prompt for a simple y/n developer decision. Kept as a
    free function (not inlined in `_cmd_run`) so a test can substitute a
    canned answer without going through `input()`."""
    print("----- proposed diff -----")
    print(diff if diff.strip() else "(no changes)")
    print("--------------------------")
    answer = input("Merge this diff into your working tree? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _cmd_run(args: argparse.Namespace) -> int:
    """U13's `josu run <task>`: the end-to-end orchestrator main loop --
    worktree -> snapshot -> MCP manifest -> circuit-breaker-wrapped adapter
    invocation -> diff review/merge, composed by `orchestrator/
    run.py`'s `run_task()`. This subcommand is the thin CLI shell around it:
    resolves config/paths, prints the surfaced diff and prompts for
    approval, then reports the outcome.

    Requires the daemon already running (the adapter's MCP manifest points
    at it, same as U14's `_cmd_delegate` above). `run_task()` itself also
    checks this first, before any worktree/git work -- but simplify pass:
    this subcommand now ALSO checks reachability here, at the very start,
    before `load_config()` (disk I/O with side effects) -- matching the
    "fail fast, no side effects" intent `run_task()`'s own docstring already
    documents, but which previously only started applying after this
    subcommand's own config-load side effects had already happened. Uses
    the same shared `delegate/daemon_client.py` helper `orchestrator/run.py`
    uses internally.
    """
    from josu.config import load_config
    from josu.delegate.daemon_client import DaemonNotReachableError, check_daemon_reachable
    from josu.observability.runlog import (
        RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT,
        RUN_OUTCOME_DIVERGED,
        RUN_OUTCOME_MERGED,
        RUN_OUTCOME_REJECTED,
    )
    from josu.orchestrator.run import NoUsableAdapterError, run_task

    host = args.host or DEFAULT_HOST
    port = args.port or DEFAULT_PORT

    try:
        check_daemon_reachable(host, port)
    except DaemonNotReachableError as exc:
        print(f"josu run: {exc}")
        return 1

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    config_path = Path(args.config) if args.config else resolve_config_path()
    config = load_config(config_path)
    for warning in config.warnings:
        print(f"josu run: warning: {warning}")

    try:
        result = run_task(
            args.task,
            config=config,
            repo_root=repo_root,
            approve=_prompt_diff_approval,
            adapter_name=args.adapter,
            host=host,
            port=port,
        )
    except DaemonNotReachableError as exc:
        print(f"josu run: {exc}")
        return 1
    except NoUsableAdapterError as exc:
        print(f"josu run: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any other
        # failure `run_task()` doesn't already surface via one of the
        # specific types above (e.g. `MCPServerConnectionError` if the
        # daemon crashes mid-run, `GitAllowlistViolationError`,
        # `ConfigPathStagedError`) must still fail with this file's
        # standard clean `josu run: <message>` shape, never an uncaught
        # traceback -- matching every other subcommand's error-handling
        # convention in this file.
        print(f"josu run: {exc}")
        return 1

    print(f"josu run: run {result.run_id} finished (see `josu log {result.run_id}` for details)")

    if result.outcome == RUN_OUTCOME_MERGED:
        print(f"  merged into {repo_root}")
        return 0
    if result.outcome == RUN_OUTCOME_REJECTED:
        print("  diff rejected -- no changes were merged")
        return 0
    if result.outcome == RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT:
        worktree_path = result.worktree.path if result.worktree else "<unknown>"
        print(
            f"  circuit breaker tripped: {result.error} -- worktree left at {worktree_path} "
            "for `josu cleanup` to handle"
        )
        return 1
    if result.outcome == RUN_OUTCOME_DIVERGED:
        print(f"  merge aborted: {result.error}")
        return 1

    print(f"  run failed: {result.error}")
    return 1


def _cmd_cleanup(args: argparse.Namespace) -> int:
    """U8's `josu cleanup`: run the crash-recovery scan, list every
    abandoned worktree (U7 quota-exhaustion abandonments and U8 crash
    orphans alike) with enough detail to decide, and remove the ones the
    developer names via `--remove`/`--remove-all`. No automatic background
    sweep anywhere in this codebase invokes this -- it only ever runs on
    this explicit developer command (plus the read-only detection half
    also running once at `josu daemon start`, see `_cmd_daemon_start`).
    """
    from josu.fallback.quota import default_abandoned_worktrees_dir
    from josu.orchestrator.worktree import WorktreeError, default_worktrees_dir

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    worktrees_dir = (
        Path(args.worktrees_dir) if args.worktrees_dir else default_worktrees_dir(repo_root)
    )
    abandoned_dir = default_abandoned_worktrees_dir(repo_root)

    records = abandoned_worktree_report(
        repo_root, worktrees_dir=worktrees_dir, abandoned_dir=abandoned_dir
    )

    if not records:
        print("No abandoned worktrees found.")
        return 0

    print(f"Abandoned worktrees ({len(records)}):")
    for record in records:
        print(f"  - {render_abandoned_record(record)}")

    names_to_remove: set[str] = set(args.remove or [])
    if args.remove_all:
        names_to_remove = {Path(record.worktree_path).name for record in records}

    exit_code = 0
    for name in sorted(names_to_remove):
        match = next(
            (record for record in records if Path(record.worktree_path).name == name), None
        )
        if match is None:
            print(f"  skip: {name!r} is not a listed abandoned worktree")
            exit_code = 1
            continue
        try:
            remove_abandoned_worktree(match, abandoned_dir=abandoned_dir, force=args.force)
        except WorktreeError as exc:
            print(f"  failed to remove {name!r}: {exc}")
            exit_code = 1
            continue
        print(f"  removed {name!r}")

    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="josu")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Manage the josu daemon")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command", required=True)

    start_parser = daemon_subparsers.add_parser("start", help="Start the daemon in the foreground")
    start_parser.add_argument("--host", default=DEFAULT_HOST)
    start_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    start_parser.add_argument(
        "--target",
        default=None,
        help=(
            "Repo root scoping graphify file reads and crash-orphaned-worktree "
            "scanning (default: cwd) -- does not affect the graph engine itself, "
            "which is a config-declared connection target, not something josu builds"
        ),
    )
    start_parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to josu.toml (candidate/chain config). Defaults to "
            "~/.config/josu/josu.toml, or $XDG_CONFIG_HOME/josu/josu.toml if set."
        ),
    )
    start_parser.set_defaults(func=_cmd_daemon_start)

    init_parser = subparsers.add_parser(
        "init",
        help=(
            "Install the post-commit hook that drives commit-triggered "
            "proactive checks -- chains to an existing hook "
            "rather than overwriting it"
        ),
    )
    init_parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to install the hook into (default: cwd)",
    )
    init_parser.set_defaults(func=_cmd_init)

    log_parser = subparsers.add_parser("log", help="Render a run's run-log record")
    log_parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Run id to render (default: the most recently started run)",
    )
    log_parser.add_argument(
        "--runlog-dir",
        default=None,
        help="Where run-log records are stored (default: ./.josu/runlog under cwd)",
    )
    log_parser.set_defaults(func=_cmd_log)

    delegate_parser = subparsers.add_parser(
        "delegate",
        help=(
            "Route a bounded task directly to the local delegate worker, "
            "bypassing the hosted orchestrator -- for use when Claude Code "
            "is quota/rate-limit exhausted"
        ),
    )
    delegate_parser.add_argument(
        "task_type",
        help=(
            "The task category to delegate -- one of: "
            f"{', '.join(sorted(DELEGABLE_TASK_TYPES))}. A non-bounded "
            "task_type is refused, not attempted locally."
        ),
    )
    delegate_parser.add_argument("task", help="The bounded task description to delegate")
    delegate_parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to josu.toml (candidate/chain config). Defaults to "
            "~/.config/josu/josu.toml, or $XDG_CONFIG_HOME/josu/josu.toml if set. "
            "The daemon, not this CLI process, loads the config itself -- this "
            "flag is used here only to resolve the daemon's shared-secret auth "
            "token file, which lives alongside josu.toml."
        ),
    )
    delegate_parser.add_argument(
        "--host",
        default=None,
        help=f"josu daemon host to connect to (default: {DEFAULT_HOST})",
    )
    delegate_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"josu daemon port to connect to (default: {DEFAULT_PORT})",
    )
    delegate_parser.set_defaults(func=_cmd_delegate)

    run_parser = subparsers.add_parser(
        "run",
        help=(
            "Run a task end-to-end through the hosted orchestrator loop: "
            "worktree -> snapshot -> MCP manifest -> circuit-breaker"
            "-wrapped adapter invocation -> diff review/merge. "
            "Requires the josu daemon already running."
        ),
    )
    run_parser.add_argument("task", help="The task description to hand to the orchestrator adapter")
    run_parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to run the task against (default: cwd)",
    )
    run_parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to josu.toml (candidate/chain/orchestrator-adapter config). "
            "Defaults to ~/.config/josu/josu.toml, or $XDG_CONFIG_HOME/josu/josu.toml "
            "if set."
        ),
    )
    run_parser.add_argument(
        "--adapter",
        default="claude_code",
        help="Which configured orchestrator adapter to run (default: claude_code)",
    )
    run_parser.add_argument(
        "--host",
        default=None,
        help=f"josu daemon host the adapter's MCP manifest should point at (default: {DEFAULT_HOST})",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"josu daemon port the adapter's MCP manifest should point at (default: {DEFAULT_PORT})",
    )
    run_parser.set_defaults(func=_cmd_run)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=(
            "List abandoned josu worktrees -- quota-exhaustion "
            "abandonments and crash-orphaned worktrees alike -- and "
            "optionally remove them"
        ),
    )
    cleanup_parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to scan for abandoned/orphaned worktrees (default: cwd)",
    )
    cleanup_parser.add_argument(
        "--worktrees-dir",
        default=None,
        help="Where josu worktrees live (default: <repo-root>/.josu/worktrees)",
    )
    cleanup_parser.add_argument(
        "--remove",
        metavar="NAME",
        action="append",
        default=None,
        help="Remove the named abandoned worktree (its directory name); may be repeated",
    )
    cleanup_parser.add_argument(
        "--remove-all",
        action="store_true",
        help="Remove every currently-listed abandoned worktree",
    )
    cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="Pass --force through to the underlying `git worktree remove`",
    )
    cleanup_parser.set_defaults(func=_cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code = args.func(args)
    sys.exit(exit_code)
