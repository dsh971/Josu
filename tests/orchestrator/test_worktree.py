"""Tests for isolated git worktree lifecycle (U4).

Real `git` subprocess calls against real temp repos throughout -- no mocking
of git itself, matching this repo's integration-first testing convention.
"""

from __future__ import annotations

import subprocess

import pytest

from josu.orchestrator.worktree import (
    Worktree,
    WorktreeError,
    create_worktree,
    list_worktrees,
    remove_worktree,
    worktree_diff,
)


def test_worktree_seeded_with_developers_uncommitted_changes(git_repo, tmp_path):
    """Covers: a task against a fixture repo produces a worktree seeded with
    the developer's current state, including uncommitted changes."""
    (git_repo / "README.md").write_text("hello\nuncommitted change\n", encoding="utf-8")

    worktree = create_worktree(git_repo, tmp_path / "worktrees")

    assert worktree.path.exists()
    seeded_content = (worktree.path / "README.md").read_text(encoding="utf-8")
    assert "uncommitted change" in seeded_content


def test_stash_create_never_touches_developers_real_working_tree(git_repo, tmp_path):
    """Covers: the developer's real working tree is never touched -- this is
    why `git stash create` (never `git stash push`) is used. Confirms the
    uncommitted change is STILL present, uncleared, in git_repo itself after
    worktree creation."""
    (git_repo / "README.md").write_text("hello\nuncommitted change\n", encoding="utf-8")

    create_worktree(git_repo, tmp_path / "worktrees")

    # If `git stash push` had been used instead, this file would have been
    # reverted to its committed content and the change moved into the stash
    # list. `stash create` must leave the real working tree untouched.
    real_content = (git_repo / "README.md").read_text(encoding="utf-8")
    assert "uncommitted change" in real_content

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True, check=True
    )
    assert "README.md" in status.stdout

    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=git_repo, capture_output=True, text=True, check=True
    )
    assert stash_list.stdout.strip() == ""


def test_worktree_with_clean_working_tree_has_no_stash_ref(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    assert worktree.stash_ref is None
    assert (worktree.path / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_worktree_diff_reflects_seeded_uncommitted_change(git_repo, tmp_path):
    (git_repo / "README.md").write_text("hello\nuncommitted change\n", encoding="utf-8")
    worktree = create_worktree(git_repo, tmp_path / "worktrees")

    diff = worktree_diff(worktree)
    assert "uncommitted change" in diff


def test_remove_worktree_cleans_up(git_repo, tmp_path):
    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    assert worktree.path.exists()

    remove_worktree(worktree)

    assert not worktree.path.exists()
    records = list_worktrees(git_repo)
    assert not any(str(worktree.path) in line for line in records)


def test_multiple_worktrees_are_independent(git_repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    first = create_worktree(git_repo, worktrees_dir, name="first")
    second = create_worktree(git_repo, worktrees_dir, name="second")

    (first.path / "README.md").write_text("first-only change\n", encoding="utf-8")

    assert (second.path / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_worktree_creation_fails_loudly_for_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(WorktreeError):
        create_worktree(not_a_repo, tmp_path / "worktrees")
