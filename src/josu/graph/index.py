"""Incremental re-indexing triggered by commit/save/merge events (U10, R14).

Rewritten for the graphify-to-gortex swap. The graphify-era implementation
bypassed the `GraphEngine` interface entirely -- it called graphify's
`extract`/`build_merge` functions directly and reached into
`GraphifyEngine._graph`/`_root` private fields to hand a live engine
instance the freshly merged graph, because `GraphifyEngine.update()` itself
ignored the caller-supplied changed-file list and recomputed its own via
graphify's manifest. Gortex's `reindex_repository` tool accepts an explicit
`paths` argument, so `GortexEngine.update(root, changed_files)` can honor
the caller-supplied list directly -- removing the reason for the old
workaround. This module now only computes the bounded changed-file list
(via git, unchanged) and calls the daemon's live engine through
`GraphEngine.update()`, via `graph/internal_api.py`'s internal HTTP route
-- never a per-process throwaway engine.

Bug this closes (doc review, graphify-to-gortex plan revision): every
caller of this module's trigger functions previously constructed its OWN
graph engine to reindex against -- `orchestrator/run.py`'s post-merge
reindex used a CLI-process-local `GraphifyEngine`, and
`proactive/watchers.py`'s commit/save triggers were never wired to call
these functions at all (dead code, zero production callers). The running
daemon's own served graph -- what every MCP query actually reads -- never
picked up those writes. Routing every trigger through the daemon's
`/graph/internal/reindex` route (this module's only graph-mutating call
now) closes both gaps at once: the graph a reindex writes to and the graph
queries read from are now provably the same instance.

Three triggers, matching the plan's "same commit/save events as U9 plus
when a merge (U5) completes":

- `reindex_on_commit()` -- the same commit event `proactive/watchers.py`'s
  `install_commit_hook()` (U9) fires a post-commit hook on.
- `reindex_on_save()` -- the same save event `proactive/watchers.py`'s
  `DebouncedSaveWatcher` (U9) fires on. A single saved file already IS the
  bounded change set; no git diff is needed to compute it.
- `reindex_on_merge()` -- fires once `orchestrator/merge.py`'s `merge()`
  (U5) returns `MergeResult(merged=True, ...)`.

All three funnel through `reindex_changed_files()`, which no longer does
any language/file-type filtering itself (graphify-era `classify_file()`
gating was specific to graphify's own AST-only, code-extension-based
scope) -- gortex's 257-language indexer decides file relevance on its own
side, not josu's.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from josu.delegate.daemon_client import DaemonNotReachableError
from josu.graph.internal_api import GraphInternalError, post_graph_internal_reindex

if TYPE_CHECKING:
    from josu.orchestrator.merge import MergeResult
    from josu.orchestrator.worktree import Worktree

# Subcommand allowlist for the git operations this module runs on the
# developer's behalf (read-only change-set discovery) -- mirrors
# `orchestrator/merge.py`'s and `orchestrator/worktree.py`'s own
# per-module allowlist convention.
_GIT_INDEX_SUBCOMMANDS = frozenset({"diff-tree", "diff"})


class ReindexError(RuntimeError):
    """Raised when a git operation needed to compute a bounded changed-file
    set fails -- mirrors `orchestrator/merge.py`'s `MergeError`. A daemon-side
    reindex failure (the engine itself unreachable/erroring) is NOT this --
    see `ReindexResult.engine_error` below, which is reported, not raised,
    matching R13's "graph trouble degrades, doesn't block the caller" posture."""


def _run_git(
    args: list[str], *, cwd: Path, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand as an argv list (`shell=False`), never a shell
    string -- mirrors `worktree.py`'s and `merge.py`'s subprocess-safety
    contract.

    `timeout` (reliability-review fix): threaded through to
    `subprocess.run(..., timeout=...)`, mirroring `worktree.py`'s and
    `merge.py`'s own `_run_git()` fix for the same git lock-contention hang
    risk. Unlike those two -- which deliberately let `TimeoutExpired`
    propagate unwrapped for `orchestrator/circuit_breaker.py` to catch --
    this module has no circuit breaker in its call chain (every caller of
    this module's trigger functions catches `ReindexError` directly, e.g.
    `proactive/watchers.py`'s commit hook), so a timeout here is wrapped
    into `ReindexError` just like `CalledProcessError`/`FileNotFoundError`
    below, keeping this module's "every git failure funnels through
    ReindexError" contract intact.
    """
    subcommand = args[0] if args else ""
    if subcommand not in _GIT_INDEX_SUBCOMMANDS:
        raise ReindexError(
            f"git subcommand {subcommand!r} is not in index.py's own allowlist "
            f"{sorted(_GIT_INDEX_SUBCOMMANDS)}"
        )
    argv = ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            argv, shell=False, capture_output=True, text=True, check=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ReindexError(f"git {' '.join(args)} timed out in {cwd} after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        raise ReindexError(f"git {' '.join(args)} failed in {cwd}: {exc.stderr.strip()}") from exc
    except FileNotFoundError as exc:
        raise ReindexError(f"git executable not found: {exc}") from exc


def _parse_name_status(output: str) -> list[str]:
    """Parse `git diff[-tree] --name-status` output into a flat path list.
    A rename/copy ("R100\\told\\tnew" / "C100\\told\\tnew") yields BOTH the
    old and new path -- the old path still matters (it no longer exists on
    disk post-rename, so `reindex_changed_files()`'s own exists()-based
    split routes it to `pruned_files`); an add/modify/delete line yields its
    one path. Mirrors `orchestrator/merge.py`'s `_changed_paths()` parsing,
    reimplemented here rather than imported since that helper is private to
    `merge.py` and this unit's file scope doesn't extend to exporting it.
    """
    paths: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            paths.extend(parts[1:])
        else:
            paths.append(parts[-1])
    return paths


@dataclass(frozen=True)
class ReindexResult:
    """What one bounded re-index touched: the paths sent to the daemon for
    reindexing, and any deleted paths reported as pruned. Both empty is a
    deliberate no-op (nothing in the bounded change set qualified), not an
    error. `engine_error` is set (rather than an exception raised) when the
    daemon's live engine itself was unreachable/erroring for this reindex
    attempt -- distinguishable in U6's run log from a normal empty result,
    since a silently-swallowed update failure would otherwise leave the
    graph arbitrarily stale with no visible signal."""

    reindexed_files: list[str] = field(default_factory=list)
    pruned_files: list[str] = field(default_factory=list)
    engine_error: str | None = None


async def reindex_changed_files(
    root: Path,
    changed_files: Sequence[Path],
    *,
    base_url: str,
    token: str | None = None,
    timeout: float = 120.0,
) -> ReindexResult:
    """The bounded core of U10 (R14): given an explicit, already-known set
    of changed file paths -- from a commit, a save event, or a completed
    merge -- ask the daemon's live `GortexEngine` (via
    `graph/internal_api.py`'s internal route) to re-index exactly those.

    A path outside `changed_files` is never even looked at -- there is no
    directory walk in this function at all. A path that no longer exists on
    disk is reported as pruned rather than sent as a reindex target (gortex
    has nothing to re-extract from a deleted file; its own
    `reindex_repository` tool is expected to prune a since-deleted path from
    its index on its next pass over the reported changed set either way).

    A `GraphInternalError`/`DaemonNotReachableError` from the daemon call is
    caught and reported via `ReindexResult.engine_error`, not raised --
    matching R13's "graph trouble degrades gracefully" posture rather than
    failing the triggering commit/save/merge event outright.
    """
    resolved = sorted({Path(f).resolve() for f in changed_files})
    existing = [f for f in resolved if f.exists()]
    deleted = [str(f) for f in resolved if not f.exists()]

    if not existing and not deleted:
        return ReindexResult()

    try:
        await post_graph_internal_reindex(base_url, root, existing, token=token, timeout=timeout)
    except (GraphInternalError, DaemonNotReachableError) as exc:
        return ReindexResult(
            reindexed_files=[str(f) for f in existing],
            pruned_files=deleted,
            engine_error=str(exc),
        )

    return ReindexResult(
        reindexed_files=[str(f) for f in existing],
        pruned_files=deleted,
    )


# --- Trigger #1: commit event (same trigger as U9's post-commit hook) ----


# Default bound for the git diff/diff-tree calls below -- these are local,
# read-only, and normally sub-second; 30s is a generous ceiling that still
# turns an unbounded git lock-contention hang (reliability-review fix) into
# a fast, reported `ReindexError` instead of hanging `git commit` forever.
_DEFAULT_GIT_TIMEOUT_SECONDS = 30.0


def _changed_paths_from_commit(root: Path, ref: str) -> list[Path]:
    """The bounded set of paths git says changed in commit `ref` relative
    to its parent. Uses `git diff-tree` (not `git diff HEAD~1 HEAD`) with
    `--root` so a commit with no parent (the repo's very first commit) is
    handled the same way -- diffed against the empty tree -- instead of
    erroring for lack of a `HEAD~1`.
    """
    result = _run_git(
        ["diff-tree", "--no-commit-id", "--name-status", "-r", "--root", ref],
        cwd=root,
        timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
    )
    return [root / p for p in _parse_name_status(result.stdout)]


async def reindex_on_commit(
    root: Path,
    *,
    base_url: str,
    token: str | None = None,
    ref: str = "HEAD",
    timeout: float = 120.0,
) -> ReindexResult:
    """U10 trigger #1: the same commit event `proactive/watchers.py`'s
    `install_commit_hook()` (U9) fires a post-commit hook on -- re-index
    exactly the files `ref` (default: the commit that was just made)
    touched, nothing else. `timeout` defaults to a general-purpose 120s but
    should be trimmed by commit-hook callers to a short budget (see
    `watchers.py`'s `_COMMIT_HOOK_HTTP_TIMEOUT_SECONDS`) so a wedged daemon
    never blocks `git commit` for long. The git diff-tree call that
    computes the changed-path set is separately bounded by
    `_DEFAULT_GIT_TIMEOUT_SECONDS` (see `_run_git()`'s own docstring).
    """
    root = root.resolve()
    changed = _changed_paths_from_commit(root, ref)
    return await reindex_changed_files(
        root, changed, base_url=base_url, token=token, timeout=timeout
    )


# --- Trigger #2: save event (same trigger as U9's DebouncedSaveWatcher) --


async def reindex_on_save(
    root: Path,
    saved_path: Path,
    *,
    base_url: str,
    token: str | None = None,
    timeout: float = 120.0,
) -> ReindexResult:
    """U10 trigger #2: the same save event `proactive/watchers.py`'s
    `DebouncedSaveWatcher` (U9) fires on. A single saved file already IS
    the bounded change set -- no git diff needed to compute it.
    """
    return await reindex_changed_files(
        root, [saved_path], base_url=base_url, token=token, timeout=timeout
    )


# --- Trigger #3: a completed merge (U5) ----------------------------------


def _changed_paths_from_worktree(worktree: "Worktree") -> list[Path]:
    """The bounded set of paths a worktree's own diff touches, relative to
    its branch-point HEAD -- the same `git diff --name-status HEAD` call
    `orchestrator/merge.py`'s private `_changed_paths()` makes to decide
    what `merge()` applies, reimplemented here (not imported) since that
    helper is private to `merge.py` and this unit's file scope doesn't
    extend to exporting it. Paths come back relative to `worktree.path`;
    the caller maps them onto `worktree.repo_root`, since that's the
    directory the graph's `root` is actually scoped to (the developer's
    real checkout, not the throwaway worktree that gets torn down).
    """
    result = _run_git(
        ["diff", "--name-status", "HEAD"], cwd=worktree.path, timeout=_DEFAULT_GIT_TIMEOUT_SECONDS
    )
    return [worktree.repo_root / p for p in _parse_name_status(result.stdout)]


async def reindex_on_merge(
    worktree: "Worktree",
    merge_result: "MergeResult",
    *,
    base_url: str,
    token: str | None = None,
) -> ReindexResult:
    """U10 trigger #3: fires when `orchestrator/merge.py`'s `merge()` (U5)
    completes -- re-index exactly the paths that merge actually applied to
    `worktree.repo_root`, nothing else. A rejected merge, or one that never
    happened for any other reason (`merge_result.merged is False`), has
    nothing to reindex -- returns an empty `ReindexResult` immediately
    without touching the graph or even computing a diff. The git diff call
    that computes the changed-path set is bounded by
    `_DEFAULT_GIT_TIMEOUT_SECONDS` (see `_run_git()`'s own docstring) --
    callers on a wall-clock budget (e.g. `orchestrator/run.py`'s post-merge
    step) should catch `ReindexError` rather than assume this never raises.
    """
    if not merge_result.merged:
        return ReindexResult()
    changed = _changed_paths_from_worktree(worktree)
    return await reindex_changed_files(
        worktree.repo_root, changed, base_url=base_url, token=token
    )
