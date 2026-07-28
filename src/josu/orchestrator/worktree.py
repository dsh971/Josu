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


@dataclass(frozen=True)
class Worktree:
    """A created, isolated worktree: its path, the branch it was created on,
    and the stash object (if any) that seeded it with the developer's
    uncommitted state."""

    path: Path
    branch: str
    repo_root: Path
    stash_ref: str | None


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

    return Worktree(path=worktree_path, branch=branch, repo_root=repo_root, stash_ref=stash_ref)


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
