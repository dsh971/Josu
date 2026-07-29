"""Tests for incremental re-indexing triggered by commit/save/merge events
(U10, R14).

Real `git` subprocess calls against real temp repos (a locally-defined
`git_repo` fixture, mirroring `tests/orchestrator/conftest.py`'s own
convention) and a real `GraphifyEngine` (U1) throughout -- no mocking of
graphify or git, matching this repo's integration-first testing convention
(see `tests/graph/test_build.py`, `tests/orchestrator/test_worktree.py`).
"""

from __future__ import annotations

import subprocess

import pytest

from josu.graph.build import GraphifyEngine
from josu.graph.index import (
    ReindexResult,
    reindex_on_commit,
    reindex_on_merge,
    reindex_on_save,
)
from josu.orchestrator.merge import merge, snapshot_repo
from josu.orchestrator.worktree import create_worktree


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo seeded with two source files, mirroring
    `tests/orchestrator/conftest.py`'s `git_repo` fixture (not reused
    directly -- this unit's file scope is `index.py` + `test_index.py`
    only, so the fixture is defined locally instead of introducing a new
    shared `tests/graph/conftest.py`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    (repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n",
        encoding="utf-8",
    )
    (repo / "unrelated.py").write_text(
        "def unrelated_function():\n    return 'untouched'\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)
    return repo


def _commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


@pytest.fixture
def engine(tmp_path):
    return GraphifyEngine(out_dir=tmp_path / "graphify-out")


# --- Trigger #1: commit event --------------------------------------------


def test_single_file_commit_reindexes_only_that_file(engine, git_repo):
    """Covers the plan's first U10 test scenario: a single-file commit
    re-indexes only that file."""
    engine.build(git_repo)

    (git_repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello there, {name}!'\n\n\ndef brand_new():\n    pass\n",
        encoding="utf-8",
    )
    _commit_all(git_repo, "edit helper.py")

    result = reindex_on_commit(engine, git_repo)

    assert result.reindexed_files == [str((git_repo / "helper.py").resolve())]
    assert result.pruned_files == []

    # The new content is actually reflected in the graph.
    assert engine.search("brand_new") != []
    # An unrelated file elsewhere in the graph is untouched by the re-index.
    assert engine.search("unrelated_function") != []
    assert engine.search("main") != []


def test_commit_touching_multiple_files_reindexes_exactly_those(engine, git_repo):
    (git_repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hi, {name}!'\n", encoding="utf-8"
    )
    (git_repo / "new_module.py").write_text(
        "def brand_new_function():\n    pass\n", encoding="utf-8"
    )
    engine.build(git_repo)

    (git_repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hiya, {name}!'\n", encoding="utf-8"
    )
    (git_repo / "new_module.py").write_text(
        "def brand_new_function():\n    return 42\n", encoding="utf-8"
    )
    _commit_all(git_repo, "edit two files")

    result = reindex_on_commit(engine, git_repo)

    assert sorted(result.reindexed_files) == sorted(
        str(p.resolve()) for p in [git_repo / "helper.py", git_repo / "new_module.py"]
    )
    assert engine.search("unrelated_function") != []


def test_commit_with_no_changed_code_files_is_a_noop(engine, git_repo):
    engine.build(git_repo)
    stats_before = engine.execute("graph_stats", {})

    (git_repo / "README.md").write_text("docs only\n", encoding="utf-8")
    _commit_all(git_repo, "docs-only commit")

    result = reindex_on_commit(engine, git_repo)

    assert result == ReindexResult()
    stats_after = engine.execute("graph_stats", {})
    assert stats_before == stats_after


def test_commit_deleting_a_file_prunes_it(engine, git_repo):
    engine.build(git_repo)
    assert engine.search("unrelated_function") != []

    (git_repo / "unrelated.py").unlink()
    _commit_all(git_repo, "delete unrelated.py")

    result = reindex_on_commit(engine, git_repo)

    assert result.pruned_files == [str((git_repo / "unrelated.py").resolve())]
    assert engine.search("unrelated_function") == []
    # Other files survive the prune untouched.
    assert engine.search("greet") != []


# --- Trigger #2: save event ------------------------------------------------


def test_save_event_reindexes_only_the_saved_file(engine, git_repo):
    engine.build(git_repo)

    (git_repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n\n\n"
        "def saved_addition():\n    pass\n",
        encoding="utf-8",
    )

    result = reindex_on_save(engine, git_repo, git_repo / "main.py")

    assert result.reindexed_files == [str((git_repo / "main.py").resolve())]
    assert engine.search("saved_addition") != []
    # An unrelated file elsewhere is untouched by the save-triggered re-index.
    assert engine.search("unrelated_function") != []
    assert engine.search("greet") != []


# --- Trigger #3: a completed merge (U5) -----------------------------------


def test_merged_diff_touching_three_files_reindexes_exactly_those(engine, git_repo, tmp_path):
    """Covers the plan's second U10 test scenario: a merged diff touching
    three files re-indexes exactly those three."""
    engine.build(git_repo)

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "helper.py").write_text(
        "def greet(name):\n    return f'Yo, {name}!'\n", encoding="utf-8"
    )
    (worktree.path / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n\n\n"
        "def merged_addition():\n    pass\n",
        encoding="utf-8",
    )
    (worktree.path / "third_module.py").write_text(
        "def third_function():\n    pass\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "third_module.py"], cwd=worktree.path, check=True)

    merge_result = merge(worktree, snapshot, approved=True)
    assert merge_result.merged is True

    result = reindex_on_merge(engine, worktree, merge_result)

    expected = sorted(
        str(p.resolve())
        for p in [git_repo / "helper.py", git_repo / "main.py", git_repo / "third_module.py"]
    )
    assert sorted(result.reindexed_files) == expected

    assert engine.search("merged_addition") != []
    assert engine.search("third_function") != []
    # An unrelated file elsewhere in the graph is untouched by the merge
    # -triggered re-index.
    assert engine.search("unrelated_function") != []


def test_rejected_merge_reindexes_nothing(engine, git_repo, tmp_path):
    engine.build(git_repo)
    stats_before = engine.execute("graph_stats", {})

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)
    (worktree.path / "helper.py").write_text("def greet(name):\n    pass\n", encoding="utf-8")

    merge_result = merge(worktree, snapshot, approved=False)
    assert merge_result.merged is False

    result = reindex_on_merge(engine, worktree, merge_result)

    assert result == ReindexResult()
    stats_after = engine.execute("graph_stats", {})
    assert stats_before == stats_after


def test_merge_deleting_a_file_prunes_it_from_the_graph(engine, git_repo, tmp_path):
    engine.build(git_repo)
    assert engine.search("unrelated_function") != []

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)
    (worktree.path / "unrelated.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=worktree.path, check=True)

    merge_result = merge(worktree, snapshot, approved=True)
    assert merge_result.merged is True

    result = reindex_on_merge(engine, worktree, merge_result)

    assert result.pruned_files == [str((git_repo / "unrelated.py").resolve())]
    assert engine.search("unrelated_function") == []
    assert engine.search("greet") != []
