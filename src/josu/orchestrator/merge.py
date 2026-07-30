"""Diff review, developer-gated merge, and divergence/conflict detection
(U5, R20, R21).

Surfaces a completed worktree's diff for explicit developer approval
(`diff_for_review()`), and only ever writes to the developer's real working
tree (`worktree.repo_root`) from `merge()`, and only when BOTH of these hold:

1. `approved=True` was passed explicitly -- a rejected (or simply
   not-yet-decided) diff never merges, under any code path (R20).
2. No divergence is detected between `worktree.repo_root`'s state at the
   moment the worktree was created and its state right now, restricted to
   the specific paths the worktree's own diff touches (R21). An unrelated
   commit or edit elsewhere in the repo never blocks this merge -- only
   files the worktree itself changed matter.

Divergence detection reuses `Worktree.stash_ref` (from `worktree.py`,
U4) rather than re-capturing file content itself: `git stash create`
already snapshots the developer's FULL working-tree state (every tracked
file, not just the dirty ones) into one commit-like object at the exact
moment the worktree was seeded, so `git show <stash_ref>:<path>` recovers
any tracked path's content as it stood then. `RepoSnapshot` pairs that
`stash_ref` with `repo_root`'s HEAD sha at the same moment, captured by
`snapshot_repo()` -- callers are expected to call this immediately after
`create_worktree()`, before Claude Code (or anyone else) runs, mirroring
how `worktree.stash_ref` itself is captured at creation time.

A path's "content at snapshot time" is `stash_ref:path` if a stash was
created (the stash commit is a full tree, so this works whether or not that
specific path was actually dirty), else the committed blob at `head:path`.
A path whose content differs between snapshot time and now -- whether that
happened via a new commit or a new uncommitted edit in `repo_root` -- counts
as diverged. This is a deliberate limitation: a path that was UNTRACKED
(not `git add`-ed) at snapshot time isn't captured by `git stash create`
(no `--include-untracked` flag is used, matching `worktree.py`'s own
behavior) and so can't be compared; an untracked-at-snapshot-time file is
treated as having had no prior content, which is correct unless a
concurrent edit to that exact untracked file also coincides with the
worktree independently producing the same path -- an edge case outside this
unit's test scenarios.

Applying an approved, non-diverged merge copies the worktree's resulting
file content directly onto the matching `repo_root` paths (add/modify: copy
bytes; delete: remove), rather than `git apply`-ing a textual patch --
avoiding any patch-context mismatch that could arise from unrelated history
in `repo_root` having moved since the worktree's branch point, given
divergence on the OVERLAPPING paths has already been ruled out above.

Uses `subprocess` + the `git` CLI directly (never GitPython), with an
argv list and `shell=False`, mirroring `worktree.py`'s and
`orchestrator/adapter.py`'s subprocess-safety convention.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from josu.orchestrator.worktree import Worktree, worktree_diff

# Subcommand allowlist for the git operations this module's own code runs
# (against repo_root, to inspect state -- never to mutate history) --
# mirrors worktree.py's `_GIT_LIFECYCLE_SUBCOMMANDS` pattern, scoped to what
# THIS module needs.
_GIT_MERGE_SUBCOMMANDS = frozenset({"rev-parse", "status", "diff", "show"})


class MergeError(RuntimeError):
    """Raised when a merge-lifecycle git operation fails -- mirrors
    `worktree.py`'s `WorktreeError`."""


class DivergedWorkingTreeError(MergeError):
    """Raised when the developer's real working tree has diverged (R21): at
    least one path the worktree's own diff touches has different content in
    `repo_root` now than it did when the worktree was created, via a new
    commit, a new uncommitted edit, or both. Raised BEFORE any write to
    `repo_root` -- a diverged merge aborts cleanly, never partially applies.
    Carries the specific diverged paths so the caller can surface exactly
    what conflicted.
    """

    def __init__(self, diverged_paths: list[str]) -> None:
        self.diverged_paths = list(diverged_paths)
        super().__init__(
            "merge aborted: the developer's real working tree has diverged since the "
            f"worktree was created -- overlapping path(s) changed: {self.diverged_paths}"
        )


def _run_git(
    args: list[str], *, cwd: Path, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand as an argv list (`shell=False`), never a shell
    string -- mirrors `worktree.py`'s and `adapter.py`'s subprocess-safety
    contract.

    `timeout` (P3 fix, U13/U14 Tier 2 review): mirrors `worktree.py`'s own
    `_run_git()` -- forwarded to `subprocess.run(..., timeout=...)`,
    `None` by default (unbounded, unchanged for every caller that doesn't
    pass one). A `subprocess.TimeoutExpired` is deliberately left
    uncaught here so `orchestrator/circuit_breaker.py`'s
    `run_under_circuit_breaker()` can translate it into a
    `CircuitBreakerTimeoutError`."""
    subcommand = args[0] if args else ""
    if subcommand not in _GIT_MERGE_SUBCOMMANDS:
        raise MergeError(
            f"git subcommand {subcommand!r} is not in merge.py's own allowlist "
            f"{sorted(_GIT_MERGE_SUBCOMMANDS)}"
        )
    argv = ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise MergeError(
            f"git {' '.join(args)} failed in {cwd}: {exc.stderr.strip()}"
        ) from exc
    except FileNotFoundError as exc:
        raise MergeError(f"git executable not found: {exc}") from exc


@dataclass(frozen=True)
class RepoSnapshot:
    """`repo_root`'s state at the moment its worktree was created: the HEAD
    sha, plus the same `stash_ref` `create_worktree()` produced (or `None`
    for a clean working tree at that moment). See module docstring for why
    `stash_ref` alone is enough to recover any tracked path's content as it
    stood then."""

    head: str
    stash_ref: str | None


def snapshot_repo(
    repo_root: Path, *, stash_ref: str | None, timeout: float | None = None
) -> RepoSnapshot:
    """Capture `repo_root`'s current HEAD sha as a `RepoSnapshot`, paired
    with `stash_ref` (the same value `create_worktree()` returned on its
    `Worktree`). Callers are expected to call this immediately after
    `create_worktree()`, before anything else touches `repo_root`, so the
    snapshot is a true "as of worktree creation" baseline.

    `timeout` (P3 fix): forwarded to the underlying `git rev-parse HEAD`
    call -- `None` by default (unbounded, unchanged)."""
    head = _run_git(["rev-parse", "HEAD"], cwd=repo_root, timeout=timeout).stdout.strip()
    return RepoSnapshot(head=head, stash_ref=stash_ref)


def diff_for_review(worktree: Worktree) -> str:
    """Surface `worktree`'s diff for developer review before any merge
    decision -- a thin, explicitly-named wrapper over
    `worktree.worktree_diff()` (U4) so a caller presenting a diff for
    approval doesn't need to reach into `worktree.py` directly."""
    return worktree_diff(worktree)


def _changed_paths(worktree: Worktree) -> list[tuple[str, str | None, str]]:
    """`git diff --name-status HEAD` inside `worktree`: each changed path's
    status letter (A/M/D/R...), its old path (only set for a rename/copy,
    else `None`), and its current path -- relative to the worktree's
    branch-point HEAD."""
    result = _run_git(["diff", "--name-status", "HEAD"], cwd=worktree.path)
    changed: list[tuple[str, str | None, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            # "R100\told\tnew"
            old_path = parts[1] if len(parts) > 2 else None
            new_path = parts[-1]
        else:
            old_path = None
            new_path = parts[-1]
        changed.append((status, old_path, new_path))
    return changed


def _show(repo_root: Path, ref: str, path: str) -> str | None:
    """`git show <ref>:<path>` in `repo_root`, or `None` if that path
    doesn't exist at `ref` (a non-zero exit -- not treated as a `MergeError`
    here, since "path absent at this ref" is an expected, common outcome,
    not a git-invocation failure)."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _content_at_snapshot(repo_root: Path, snapshot: RepoSnapshot, path: str) -> str | None:
    """`path`'s content in `repo_root` at snapshot time -- from
    `snapshot.stash_ref` if a stash was created (a full-tree snapshot, see
    module docstring), else from the committed blob at `snapshot.head`."""
    if snapshot.stash_ref:
        return _show(repo_root, snapshot.stash_ref, path)
    return _show(repo_root, snapshot.head, path)


def _content_now(repo_root: Path, path: str) -> str | None:
    """`path`'s CURRENT content on disk in `repo_root` -- the real working
    tree, not any git ref, since an uncommitted concurrent edit (not just a
    new commit) must also be detected."""
    full = repo_root / path
    if not full.exists():
        return None
    return full.read_text(encoding="utf-8", errors="surrogateescape")


def find_diverged_paths(
    worktree: Worktree,
    snapshot: RepoSnapshot,
    *,
    changed_paths: list[tuple[str, str | None, str]] | None = None,
) -> list[str]:
    """The R21 divergence check: for every path the worktree's own diff
    touches (its old path too, for a rename), does `repo_root`'s CURRENT
    content for that path differ from what it was at snapshot time -- via a
    new commit, a new uncommitted edit, or both? Only overlapping paths
    matter; an unrelated commit or edit elsewhere in `repo_root` never
    blocks this merge.

    `changed_paths` lets a caller that already ran `_changed_paths(worktree)`
    (e.g. `merge()`, which also needs the same list for its apply loop) pass
    it straight through instead of this function re-running the same `git
    diff --name-status` subprocess call a second time. Defaults to `None`,
    computing it here, for callers (including this module's own tests) that
    just want the divergence check on its own.
    """
    if changed_paths is None:
        changed_paths = _changed_paths(worktree)
    diverged: list[str] = []
    seen: set[str] = set()
    for _status, old_path, new_path in changed_paths:
        for path in (p for p in (old_path, new_path) if p is not None):
            if path in seen:
                continue
            seen.add(path)
            before = _content_at_snapshot(worktree.repo_root, snapshot, path)
            now = _content_now(worktree.repo_root, path)
            if before != now:
                diverged.append(path)
    return diverged


@dataclass(frozen=True)
class MergeResult:
    """The outcome of a `merge()` call: whether it actually merged, a short
    machine-readable reason (`"rejected"` or `"merged"`), and (only
    meaningful when `merged` is `False` because of rejection) no diverged
    paths -- a divergence instead raises `DivergedWorkingTreeError` directly,
    since that's a conflict to surface distinctly, not a normal outcome."""

    merged: bool
    reason: str
    diverged_paths: list[str] = field(default_factory=list)


def merge(worktree: Worktree, snapshot: RepoSnapshot, *, approved: bool) -> MergeResult:
    """Merge `worktree`'s resulting changes into `worktree.repo_root`'s REAL
    working tree.

    Never writes to `repo_root` unless BOTH:
    1. `approved` is `True` -- a rejected diff returns
       `MergeResult(merged=False, reason="rejected")` immediately, without
       even running the divergence check (R20: explicit approval gates
       everything, nothing about a rejected diff is inspected further).
    2. No divergence is found (R21) -- `find_diverged_paths()` runs BEFORE
       any write; if it finds anything, `DivergedWorkingTreeError` is raised
       and `repo_root` is left completely untouched (a clean abort, never a
       partial merge).

    On success, each changed path is applied directly: added/modified paths
    have their worktree-side bytes copied onto the matching `repo_root`
    path (creating parent directories as needed); deleted paths are removed
    from `repo_root` if present. A rename is applied as delete-old +
    add-new.
    """
    if not approved:
        return MergeResult(merged=False, reason="rejected")

    changed_paths = _changed_paths(worktree)
    diverged = find_diverged_paths(worktree, snapshot, changed_paths=changed_paths)
    if diverged:
        raise DivergedWorkingTreeError(diverged)

    for status, old_path, new_path in changed_paths:
        if old_path is not None and old_path != new_path:
            old_target = worktree.repo_root / old_path
            if old_target.exists():
                old_target.unlink()

        if status.startswith("D"):
            target = worktree.repo_root / new_path
            if target.exists():
                target.unlink()
            continue

        source = worktree.path / new_path
        target = worktree.repo_root / new_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    return MergeResult(merged=True, reason="merged")
