"""Isolated git worktree lifecycle (U4).

Uses `subprocess` + the `git worktree`/`git stash` CLI directly, not
GitPython -- GitPython has no worktree support at all (a closed, won't-fix
upstream issue; see plan Sources/Research), so there's no library layer to
wrap here in the first place.

Every worktree is seeded via `git stash create` -- which builds a stash-like
commit object and prints its hash WITHOUT touching the index, HEAD, or the
working tree -- plus applying that stash object into the new worktree. This
is deliberately never `git stash push`: `push` mutates the developer's real
working tree (clears it out), which is exactly the side effect this unit
must never have. `git stash create` is read-only with respect to the
developer's checkout; only the new worktree is ever mutated.

All git invocations here use an argv list with `shell=False` (never a shell
string), matching the same subprocess-safety convention `orchestrator/adapter.py`
uses for the hosted CLI invocation itself.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

# Subcommand allowlist for the git operations *this module* runs on the
# developer's behalf (worktree lifecycle management) -- deliberately not the
# same allowlist as `adapters/claude_code.py`'s `--allowedTools` Bash(git ...)
# surface, which bounds what the invoked hosted CLI itself may run. This one
# bounds what `worktree.py`'s own code runs.
_GIT_LIFECYCLE_SUBCOMMANDS = frozenset(
    {"stash", "worktree", "diff", "rev-parse", "status"}
)


class WorktreeError(RuntimeError):
    """Raised when a git worktree-lifecycle operation fails."""


# `<repo_root>/.josu/worktrees/` -- where `cli.py`'s subcommands default to
# looking for josu-managed worktrees when no `--worktrees-dir` is given.
# Mirrors the `.josu/` project-local convention `fallback/quota.py`'s
# abandoned-worktree markers and `observability/runlog.py`'s run log
# already use; not itself required by `create_worktree()`, which accepts
# any `worktrees_dir` a caller passes in.
DEFAULT_WORKTREES_SUBDIR = Path(".josu") / "worktrees"


def default_worktrees_dir(repo_root: Path) -> Path:
    """`<repo_root>/.josu/worktrees/` -- see `DEFAULT_WORKTREES_SUBDIR`."""
    return repo_root / DEFAULT_WORKTREES_SUBDIR


@dataclass(frozen=True)
class Worktree:
    """A created, isolated worktree: its path, the branch it was created on,
    and the stash object (if any) that seeded it with the developer's
    uncommitted state.

    `task_description` is optional and purely informational (U8): if a
    caller has a human-readable description of the task the worktree was
    created for, it rides along on the `Worktree` itself so
    `fallback/quota.py`'s `mark_worktree_abandoned()` can carry it into an
    abandonment marker for `josu cleanup` to display later. A worktree
    rediscovered from `git worktree list --porcelain` after a crash (U8's
    `find_orphaned_worktrees()`) never has one -- nothing persists it
    anywhere `git` itself tracks -- so it's `None` in that case."""

    path: Path
    branch: str
    repo_root: Path
    stash_ref: str | None
    task_description: str | None = None


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand as an argv list (`shell=False`), never a shell
    string -- mirrors `orchestrator/adapter.py`'s subprocess-safety contract.
    """
    subcommand = args[0] if args else ""
    if subcommand not in _GIT_LIFECYCLE_SUBCOMMANDS:
        raise WorktreeError(
            f"git subcommand {subcommand!r} is not in worktree.py's own lifecycle "
            f"allowlist {sorted(_GIT_LIFECYCLE_SUBCOMMANDS)}"
        )
    argv = ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {cwd}: {exc.stderr.strip()}"
        ) from exc
    except FileNotFoundError as exc:
        raise WorktreeError(f"git executable not found: {exc}") from exc


def create_worktree(
    repo_root: Path,
    worktrees_dir: Path,
    *,
    name: str | None = None,
    task_description: str | None = None,
) -> Worktree:
    """Create a new, isolated git worktree under `worktrees_dir`, seeded with
    the developer's current state including uncommitted changes -- never
    touching `repo_root`'s own working tree.

    Steps:
    1. `git stash create` in `repo_root` -- builds (but does not apply or
       drop) a stash commit capturing the current index + working tree
       state, printing its hash. Empty output means there was nothing
       uncommitted to capture (a clean working tree), which is not an error.
    2. `git worktree add -b <branch> <path> HEAD` -- a fresh worktree,
       branched from the repo's current HEAD.
    3. If step 1 produced a stash hash, `git stash apply <hash>` inside the
       new worktree brings the uncommitted state into it. `repo_root`'s
       working tree and index are never modified by any of this -- `stash
       create` doesn't touch them, and `stash apply` only ever runs against
       the new worktree's path.
    """
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    worktree_name = name or f"josu-{uuid.uuid4().hex[:12]}"
    worktree_path = worktrees_dir / worktree_name
    branch = f"josu/{worktree_name}"

    stash_result = _run_git(["stash", "create"], cwd=repo_root)
    stash_ref = stash_result.stdout.strip() or None

    _run_git(
        ["worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
        cwd=repo_root,
    )

    if stash_ref:
        _run_git(["stash", "apply", stash_ref], cwd=worktree_path)

    return Worktree(
        path=worktree_path,
        branch=branch,
        repo_root=repo_root,
        stash_ref=stash_ref,
        task_description=task_description,
    )


def remove_worktree(worktree: Worktree, *, force: bool = False) -> None:
    """Remove a worktree via `git worktree remove` (never a raw `rm -rf`, so
    git's own bookkeeping in `.git/worktrees/` stays consistent)."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree.path))
    _run_git(args, cwd=worktree.repo_root)


def worktree_diff(worktree: Worktree) -> str:
    """Return the full diff (tracked changes vs. the worktree's branch-point
    HEAD, plus any untracked-but-staged changes) inside `worktree` -- the
    diff a merge step (U5) or a run-log entry (U6) would surface for
    developer review. Deliberately scoped to `worktree.path`, never
    `worktree.repo_root`."""
    result = _run_git(["diff", "HEAD"], cwd=worktree.path)
    return result.stdout


def list_worktrees(repo_root: Path) -> list[str]:
    """Return `git worktree list --porcelain`'s raw output, split into
    records -- a thin passthrough kept here so U8's crash-recovery cleanup
    can reuse it without re-deriving the git invocation."""
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    return result.stdout.splitlines()


# --- Crash recovery (U8) -----------------------------------------------------
#
# On a clean shutdown, a worktree's lifecycle always ends with an explicit
# `remove_worktree()` call -- whether the diff was merged or rejected (U5's
# `merge.py`), or the task was abandoned mid-run (`fallback/quota.py`'s
# `mark_worktree_abandoned()` records *why*, but the worktree itself still
# gets torn down once the developer acts on it via `josu cleanup`). So a
# worktree `git worktree list --porcelain` still reports, days later, that
# ALSO has no abandonment-marker record yet, can only mean one thing: the
# wrapper process died before it could either finish normally or record why
# it didn't -- a crash. `find_orphaned_worktrees()` below is the read-only
# detection half of that; `cli.py`'s `scan_for_crash_orphaned_worktrees()`
# is the half that actually writes the marker, via `fallback/quota.py`'s
# `mark_worktree_abandoned()` -- kept out of this module deliberately,
# since `fallback/quota.py` already imports `Worktree` from here, and a
# reverse import would be circular.


@dataclass(frozen=True)
class WorktreeListEntry:
    """One parsed record from `git worktree list --porcelain`: just the
    path and (if any) branch name -- everything porcelain output actually
    gives us for a worktree this process has no other memory of."""

    path: Path
    branch: str | None


def _parse_worktree_list_porcelain(lines: list[str]) -> list[WorktreeListEntry]:
    """Parse `git worktree list --porcelain`'s line-based record format.
    Each record starts with a `worktree <path>` line; a `branch
    refs/heads/<name>` line (if present -- a detached-HEAD worktree has
    none) follows within the same record. Records are separated by blank
    lines in real git output, but since every record also starts with a
    fresh `worktree ` line, that alone is enough to detect a new record
    without relying on the blank-line separator too."""
    entries: list[WorktreeListEntry] = []
    current_path: str | None = None
    current_branch: str | None = None

    def _flush() -> None:
        if current_path is not None:
            entries.append(WorktreeListEntry(path=Path(current_path), branch=current_branch))

    for line in lines:
        if line.startswith("worktree "):
            _flush()
            current_path = line[len("worktree ") :]
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            current_branch = ref.removeprefix("refs/heads/")
    _flush()

    return entries


def list_managed_worktrees(repo_root: Path, worktrees_dir: Path) -> list[Worktree]:
    """Every worktree `git worktree list --porcelain` reports that lives
    under `worktrees_dir` -- i.e. one josu itself created via
    `create_worktree()` -- excluding the repo's own primary working tree
    (which `git worktree list` always includes first, but which never lives
    under `worktrees_dir`). Reflects git's own bookkeeping directly, so this
    finds a worktree created by a PRIOR process invocation just as well as
    one this process created itself -- the basis for crash recovery.

    Each returned `Worktree` has `stash_ref=None` and `task_description=
    None`: neither is recoverable from `git worktree list` alone (a stash
    object's hash, and any task description, live only in the creating
    process's memory or in whatever a caller separately persisted)."""
    resolved_worktrees_dir = worktrees_dir.resolve()
    managed: list[Worktree] = []
    for entry in _parse_worktree_list_porcelain(list_worktrees(repo_root)):
        resolved_entry_path = entry.path.resolve()
        try:
            resolved_entry_path.relative_to(resolved_worktrees_dir)
        except ValueError:
            continue  # not under worktrees_dir -- e.g. the primary working tree
        managed.append(
            Worktree(
                path=entry.path,
                branch=entry.branch or "",
                repo_root=repo_root,
                stash_ref=None,
            )
        )
    return managed


def find_orphaned_worktrees(
    repo_root: Path,
    worktrees_dir: Path,
    *,
    known_abandoned_names: Collection[str] = (),
) -> list[Worktree]:
    """The read-only half of U8's crash-recovery scan: every worktree under
    `worktrees_dir` that `git worktree list --porcelain` still reports but
    that has no matching completed/rejected/abandoned record -- i.e. its
    directory name (`Worktree.path.name`, matching how
    `fallback/quota.py`'s `mark_worktree_abandoned()` names its marker
    files) isn't in `known_abandoned_names`.

    A properly merged or rejected worktree is always removed via
    `remove_worktree()` as part of finishing that decision, so it no longer
    appears in `git worktree list` at all by the time this runs -- it's
    never a candidate here in the first place, regardless of
    `known_abandoned_names`. `known_abandoned_names` only needs to rule out
    worktrees ALREADY marked abandoned (by this function on a previous
    call, or by U7's quota-exhaustion path), so a repeated scan doesn't
    re-surface the same orphan indefinitely.

    Never removes or otherwise mutates anything -- callers (`cli.py`'s
    `scan_for_crash_orphaned_worktrees()`) decide what, if anything, to do
    with the result."""
    known = set(known_abandoned_names)
    return [
        worktree
        for worktree in list_managed_worktrees(repo_root, worktrees_dir)
        if worktree.path.name not in known
    ]
