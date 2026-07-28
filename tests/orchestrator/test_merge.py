"""Tests for developer-gated merge and divergence/conflict detection (U5,
R20, R21).

Real `git` subprocess calls against real temp repos throughout (via the
`git_repo` fixture from `conftest.py`) -- no mocking of git itself, matching
`test_worktree.py`'s established convention for this repo.
"""

from __future__ import annotations

import subprocess

import pytest

from josu.orchestrator.merge import (
    DivergedWorkingTreeError,
    MergeError,
    diff_for_review,
    find_diverged_paths,
    merge,
    snapshot_repo,
)
from josu.orchestrator.worktree import create_worktree


def _commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


# --- approval gating (R20) ----------------------------------------------------


def test_rejected_diff_never_merges(git_repo, tmp_path):
    """Covers: a rejected diff never merges -- repo_root's real working tree
    is left completely untouched."""
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").write_text("changed by claude code\n", encoding="utf-8")

    result = merge(worktree, snapshot, approved=False)

    assert result.merged is False
    assert result.reason == "rejected"
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_approved_diff_merges_only_after_explicit_approval(git_repo, tmp_path):
    """Covers: an approved diff merges only after explicit developer
    approval -- confirms the real working tree DOES pick up the change once
    approved=True and nothing has diverged."""
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").write_text("changed by claude code\n", encoding="utf-8")

    result = merge(worktree, snapshot, approved=True)

    assert result.merged is True
    assert result.reason == "merged"
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "changed by claude code\n"


def test_new_file_added_in_worktree_is_copied_into_repo_root_on_merge(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "new_file.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "new_file.py"], cwd=worktree.path, check=True)

    result = merge(worktree, snapshot, approved=True)

    assert result.merged is True
    assert (git_repo / "new_file.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_deleted_file_in_worktree_is_removed_from_repo_root_on_merge(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").unlink()
    subprocess.run(["git", "add", "-A"], cwd=worktree.path, check=True)

    result = merge(worktree, snapshot, approved=True)

    assert result.merged is True
    assert not (git_repo / "README.md").exists()


# --- diff surfacing for review -------------------------------------------------


def test_diff_for_review_surfaces_the_worktree_diff(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    (worktree.path / "README.md").write_text("proposed change\n", encoding="utf-8")

    diff = diff_for_review(worktree)

    assert "proposed change" in diff


# --- divergence / conflict detection (R21) ------------------------------------


def test_concurrent_edit_to_the_same_file_produces_a_surfaced_conflict(git_repo, tmp_path):
    """Covers: a concurrent edit to the same file in the real working tree
    while the orchestrator was running produces a surfaced conflict at
    merge time, rather than a corrupted merge -- repo_root's conflicting
    edit must survive unmodified after the aborted merge."""
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    # Claude Code's change, inside the isolated worktree.
    (worktree.path / "README.md").write_text("claude code's version\n", encoding="utf-8")

    # Meanwhile, the developer's real working tree changes the SAME file.
    (git_repo / "README.md").write_text("developer's concurrent edit\n", encoding="utf-8")

    with pytest.raises(DivergedWorkingTreeError) as exc_info:
        merge(worktree, snapshot, approved=True)

    assert "README.md" in exc_info.value.diverged_paths
    # Aborted cleanly: repo_root keeps the developer's own edit, never
    # overwritten and never left in some half-merged state.
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "developer's concurrent edit\n"


def test_new_commit_touching_the_same_file_produces_a_surfaced_conflict(git_repo, tmp_path):
    """Covers the "new commits ... touching the same files" half of R21 --
    not just an uncommitted concurrent edit."""
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").write_text("claude code's version\n", encoding="utf-8")

    (git_repo / "README.md").write_text("developer committed this instead\n", encoding="utf-8")
    _commit_all(git_repo, "developer's own commit while orchestrator ran")

    with pytest.raises(DivergedWorkingTreeError) as exc_info:
        merge(worktree, snapshot, approved=True)

    assert "README.md" in exc_info.value.diverged_paths
    assert (
        git_repo / "README.md"
    ).read_text(encoding="utf-8") == "developer committed this instead\n"


def test_unrelated_concurrent_edit_to_a_different_file_does_not_block_the_merge(git_repo, tmp_path):
    """Only OVERLAPPING paths matter -- a concurrent edit to a file the
    worktree's own diff never touched must not block an otherwise-clean
    merge."""
    (git_repo / "OTHER.md").write_text("original other\n", encoding="utf-8")
    _commit_all(git_repo, "add OTHER.md")

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").write_text("claude code's version\n", encoding="utf-8")

    # Concurrent edit to a DIFFERENT, non-overlapping file.
    (git_repo / "OTHER.md").write_text("developer's unrelated edit\n", encoding="utf-8")

    result = merge(worktree, snapshot, approved=True)

    assert result.merged is True
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "claude code's version\n"
    assert (git_repo / "OTHER.md").read_text(encoding="utf-8") == "developer's unrelated edit\n"


def test_find_diverged_paths_is_empty_when_nothing_diverged(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").write_text("claude code's version\n", encoding="utf-8")

    assert find_diverged_paths(worktree, snapshot) == []


def test_concurrent_edit_that_reverts_to_the_same_content_is_not_flagged_as_diverged(
    git_repo, tmp_path
):
    """A path is only "diverged" if its CONTENT actually differs from
    snapshot time -- not merely if repo_root's working tree was touched at
    all."""
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "README.md").write_text("claude code's version\n", encoding="utf-8")

    # Touch the file in repo_root but leave its content identical.
    (git_repo / "README.md").write_text("hello\n", encoding="utf-8")

    assert find_diverged_paths(worktree, snapshot) == []
    result = merge(worktree, snapshot, approved=True)
    assert result.merged is True


def test_worktree_seeded_with_uncommitted_change_merges_cleanly_against_unchanged_repo_root(
    git_repo, tmp_path
):
    """The worktree itself is seeded (via stash apply, U4) with a
    pre-existing uncommitted change -- that pre-existing content must NOT
    be misread as a "concurrent edit" by the divergence check, since
    nothing in repo_root changed after snapshot time."""
    (git_repo / "README.md").write_text("developer's pre-existing dirty edit\n", encoding="utf-8")
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    # Claude Code builds on top of the seeded uncommitted state.
    (worktree.path / "README.md").write_text(
        "developer's pre-existing dirty edit\nplus claude code's addition\n", encoding="utf-8"
    )

    assert find_diverged_paths(worktree, snapshot) == []
    result = merge(worktree, snapshot, approved=True)
    assert result.merged is True
    assert (git_repo / "README.md").read_text(encoding="utf-8") == (
        "developer's pre-existing dirty edit\nplus claude code's addition\n"
    )


def test_snapshot_repo_captures_current_head(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert snapshot.head == head


def test_merge_rejects_disallowed_git_subcommand_defensively(tmp_path):
    """merge.py's own git-invocation allowlist (mirrors worktree.py's) is
    re-checked independent of any caller -- proven directly against the
    module-private helper via a subcommand outside its allowlist."""
    from josu.orchestrator.merge import _run_git

    with pytest.raises(MergeError):
        _run_git(["push"], cwd=tmp_path)
