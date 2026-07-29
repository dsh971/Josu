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
from josu.daemon import DEFAULT_HOST, DEFAULT_PORT


def _default_graph_out_dir() -> Path:
    return Path.cwd() / ".josu" / "graphify-out"


# --- U8: crash recovery and cleanup -----------------------------------------
#
# `scan_for_crash_orphaned_worktrees()` is the one place that knows about
# BOTH `orchestrator/worktree.py`'s read-only orphan detection AND
# `fallback/quota.py`'s abandonment-marker mechanism -- neither of those
# two modules imports the other (quota.py already imports `Worktree` from
# worktree.py, so the reverse would be circular), so the actual
# cross-referencing has to happen here.


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
    from josu.fallback.quota import (
        ABANDON_REASON_CRASH_ORPHANED,
        default_abandoned_worktrees_dir,
        list_abandoned_worktrees,
        mark_worktree_abandoned,
    )
    from josu.orchestrator.worktree import find_orphaned_worktrees

    resolved_abandoned_dir = abandoned_dir or default_abandoned_worktrees_dir(repo_root)
    known_names = {
        Path(record.worktree_path).name
        for record in list_abandoned_worktrees(resolved_abandoned_dir)
    }

    orphans = find_orphaned_worktrees(
        repo_root, worktrees_dir, known_abandoned_names=known_names
    )
    for worktree in orphans:
        mark_worktree_abandoned(
            worktree,
            reason=ABANDON_REASON_CRASH_ORPHANED,
            abandoned_dir=resolved_abandoned_dir,
        )
    return [worktree.path.name for worktree in orphans]


def abandoned_worktree_report(
    repo_root: Path,
    *,
    worktrees_dir: Path | None = None,
    abandoned_dir: Path | None = None,
) -> list:
    """Run U8's crash-recovery scan, then return every currently-known
    abandoned worktree record -- both U7's quota-exhaustion abandonments
    and U8's crash orphans, indistinguishable in shape (only `.reason`
    differs), exactly what `josu cleanup` lists."""
    from josu.fallback.quota import default_abandoned_worktrees_dir, list_abandoned_worktrees
    from josu.orchestrator.worktree import default_worktrees_dir

    resolved_worktrees_dir = worktrees_dir or default_worktrees_dir(repo_root)
    resolved_abandoned_dir = abandoned_dir or default_abandoned_worktrees_dir(repo_root)

    scan_for_crash_orphaned_worktrees(
        repo_root, resolved_worktrees_dir, abandoned_dir=resolved_abandoned_dir
    )
    return list_abandoned_worktrees(resolved_abandoned_dir)


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

    graph_out_dir = Path(args.graph_out_dir) if args.graph_out_dir else _default_graph_out_dir()
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
        f"(graph: {graph_out_dir}, target: {target}, config: {config_path})"
    )
    run(graph_out_dir, host=args.host, port=args.port, target=target, config_path=config_path)
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
        latest_run_id,
        load_run,
        render_run,
    )

    runlog_dir = Path(args.runlog_dir) if args.runlog_dir else default_runlog_dir(Path.cwd())
    run_id = args.run_id or latest_run_id(runlog_dir)
    if run_id is None:
        print(f"No run log entries found under {runlog_dir}")
        return 1

    try:
        record = load_run(run_id, runlog_dir)
    except RunNotFoundError as exc:
        print(str(exc))
        return 1

    print(render_run(record))
    return 0


def _cmd_delegate(args: argparse.Namespace) -> int:
    """U7's direct-request escape hatch: route a bounded `task_type` straight
    to the local delegate worker via `fallback/quota.py`'s
    `route_bounded_request()` -- no daemon, no worktree, no Claude Code
    invocation involved. Meant for a developer to invoke by hand once
    they've noticed (via `josu log`, or Claude Code simply refusing to run)
    that Claude Code is quota/rate-limit exhausted.
    """
    import asyncio

    from josu.config import load_config
    from josu.delegate.chain import ChainExhaustedError, NoCandidatesError
    from josu.delegate.queue import DelegateQueue
    from josu.fallback.quota import TaskNotBoundedError, route_bounded_request

    config_path = Path(args.config) if args.config else resolve_config_path()
    config = load_config(config_path)
    registry = {candidate.name: candidate for candidate in config.delegate.candidates}

    try:
        outcome = asyncio.run(
            route_bounded_request(
                args.task_type,
                args.task,
                chains_config=config.chains,
                registry=registry,
                queue=DelegateQueue(),
            )
        )
    except TaskNotBoundedError as exc:
        print(str(exc))
        return 1
    except (NoCandidatesError, ChainExhaustedError) as exc:
        print(str(exc))
        return 1

    print(outcome.message)
    print(outcome.delegate_result.result)
    if outcome.delegate_result.caveats:
        print(f"caveats: {outcome.delegate_result.caveats}")
    return 0


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
        "--graph-out-dir",
        default=None,
        help="Where the graph is persisted (default: ./.josu/graphify-out)",
    )
    start_parser.add_argument(
        "--target",
        default=None,
        help="Project root to build the graph from if none exists yet (default: cwd)",
    )
    start_parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to josu.toml (candidate/chain config). Defaults to the "
            "XDG-style location config/__init__.py resolves "
            "(~/.config/josu/josu.toml, or $XDG_CONFIG_HOME/josu/josu.toml)."
        ),
    )
    start_parser.set_defaults(func=_cmd_daemon_start)

    init_parser = subparsers.add_parser(
        "init",
        help=(
            "Install the post-commit hook that drives commit-triggered "
            "proactive checks (U9, R15) -- chains to an existing hook "
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
            "is quota/rate-limit exhausted (U7, R28/R29)"
        ),
    )
    delegate_parser.add_argument(
        "task_type",
        help=(
            "A config/chains.py DELEGABLE_TASK_TYPES category name "
            "(e.g. file_summarization). A non-bounded task_type is refused, "
            "not attempted locally."
        ),
    )
    delegate_parser.add_argument("task", help="The bounded task description to delegate")
    delegate_parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to josu.toml (candidate/chain config). Defaults to the "
            "XDG-style location config/__init__.py resolves."
        ),
    )
    delegate_parser.set_defaults(func=_cmd_delegate)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=(
            "List abandoned josu worktrees -- U7 quota-exhaustion "
            "abandonments and U8 crash-orphaned worktrees alike -- and "
            "optionally remove them (U8, R27)"
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
