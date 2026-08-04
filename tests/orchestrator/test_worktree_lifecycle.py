"""Tests for U8's worktree crash recovery and cleanup (R27).

Real `git` subprocess calls against real temp repos throughout (via the
`git_repo` fixture from `conftest.py`) -- no mocking of git itself,
matching `test_worktree.py`'s and `test_quota.py`'s established
convention for this repo.

Covers the plan's three test scenarios:

1. A simulated crash mid-run -- an orphaned worktree with no matching
   in-flight/abandoned state -- is detected on next startup and surfaced,
   not silently resumed.
2. `josu cleanup` lists abandoned worktrees with enough detail to decide,
   and removes one cleanly via `git worktree remove`.
3. A merged or rejected worktree from U5 doesn't appear in the abandoned
   list.
"""

from __future__ import annotations

from pathlib import Path

from josu.cli import (
    abandoned_worktree_report,
    remove_abandoned_worktree,
    render_abandoned_record,
    scan_for_crash_orphaned_worktrees,
)
from josu.fallback.quota import (
    ABANDON_REASON_CRASH_ORPHANED,
    ABANDON_REASON_QUOTA_EXHAUSTION,
    list_abandoned_worktrees,
    mark_worktree_abandoned,
)
from josu.orchestrator.merge import merge, snapshot_repo
from josu.orchestrator.worktree import (
    create_worktree,
    find_orphaned_worktrees,
    list_worktrees,
    remove_worktree,
)

# --- Scenario 1: a simulated crash mid-run is detected and surfaced --------


def test_orphaned_worktree_from_crash_is_detected(git_repo, tmp_path):
    """A worktree with no matching completed/rejected/abandoned record --
    simulating a wrapper process that died mid-run before it could either
    finish or record why it didn't -- is found by the read-only detection
    pass."""
    worktrees_dir = tmp_path / "worktrees"
    worktree = create_worktree(git_repo, worktrees_dir, name="crashed-task")

    orphans = find_orphaned_worktrees(git_repo, worktrees_dir)

    assert len(orphans) == 1
    assert orphans[0].path == worktree.path
    assert orphans[0].branch == worktree.branch


def test_startup_scan_surfaces_orphan_via_shared_abandonment_mechanism(git_repo, tmp_path):
    """The write half of the scan (what actually runs at daemon/CLI
    startup): a crash orphan gets marked abandoned through the SAME
    marker mechanism U7's quota-exhaustion path uses -- not a second,
    incompatible one -- so it's not silently resumed on the next run."""
    worktrees_dir = tmp_path / "worktrees"
    abandoned_dir = tmp_path / "abandoned"
    worktree = create_worktree(git_repo, worktrees_dir, name="crashed-task-2")

    newly_marked = scan_for_crash_orphaned_worktrees(
        git_repo, worktrees_dir, abandoned_dir=abandoned_dir
    )

    assert newly_marked == ["crashed-task-2"]
    records = list_abandoned_worktrees(abandoned_dir)
    assert len(records) == 1
    assert records[0].reason == ABANDON_REASON_CRASH_ORPHANED
    assert records[0].worktree_path == str(worktree.path)
    assert records[0].branch == worktree.branch

    # The worktree itself is untouched -- surfaced, not discarded.
    assert worktree.path.exists()

    # A repeated scan doesn't re-mark or duplicate the record.
    again = scan_for_crash_orphaned_worktrees(
        git_repo, worktrees_dir, abandoned_dir=abandoned_dir
    )
    assert again == []
    assert len(list_abandoned_worktrees(abandoned_dir)) == 1


def test_currently_in_flight_worktree_with_no_marker_is_still_flagged_not_resumed(
    git_repo, tmp_path
):
    """The detection pass never distinguishes 'this looks still in
    progress' from 'this crashed' -- it only knows what git and the
    marker directory say. A worktree found at startup is always
    surfaced, never silently resumed, matching the plan's explicit
    requirement."""
    worktrees_dir = tmp_path / "worktrees"
    create_worktree(git_repo, worktrees_dir, name="ambiguous-task")

    orphans = find_orphaned_worktrees(git_repo, worktrees_dir, known_abandoned_names=())

    assert [o.path.name for o in orphans] == ["ambiguous-task"]


# --- Scenario 2: `josu cleanup` lists with detail and removes one ---------


def test_cleanup_report_lists_both_reasons_with_enough_detail_to_decide(git_repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    abandoned_dir = tmp_path / "abandoned"

    quota_worktree = create_worktree(
        git_repo, worktrees_dir, name="quota-task", task_description="summarize module X"
    )
    mark_worktree_abandoned(
        quota_worktree, reason=ABANDON_REASON_QUOTA_EXHAUSTION, abandoned_dir=abandoned_dir
    )

    # No marker for this one -- simulates a crash the scan discovers.
    create_worktree(git_repo, worktrees_dir, name="crashed-task-3")

    records = abandoned_worktree_report(
        git_repo, worktrees_dir=worktrees_dir, abandoned_dir=abandoned_dir
    )

    names = {Path(r.worktree_path).name for r in records}
    assert names == {"quota-task", "crashed-task-3"}

    quota_record = next(r for r in records if Path(r.worktree_path).name == "quota-task")
    assert quota_record.reason == ABANDON_REASON_QUOTA_EXHAUSTION
    assert quota_record.task_description == "summarize module X"

    orphan_record = next(r for r in records if Path(r.worktree_path).name == "crashed-task-3")
    assert orphan_record.reason == ABANDON_REASON_CRASH_ORPHANED
    assert orphan_record.task_description is None

    rendered = render_abandoned_record(quota_record)
    assert "quota-task" in rendered
    assert ABANDON_REASON_QUOTA_EXHAUSTION in rendered
    assert "age=" in rendered
    assert "summarize module X" in rendered
    assert str(quota_worktree.path) in rendered


def test_cleanup_removes_one_abandoned_worktree_cleanly(git_repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    abandoned_dir = tmp_path / "abandoned"

    worktree = create_worktree(git_repo, worktrees_dir, name="quota-task")
    mark_worktree_abandoned(
        worktree, reason=ABANDON_REASON_QUOTA_EXHAUSTION, abandoned_dir=abandoned_dir
    )
    keep_worktree = create_worktree(git_repo, worktrees_dir, name="crashed-task-4")

    records = abandoned_worktree_report(
        git_repo, worktrees_dir=worktrees_dir, abandoned_dir=abandoned_dir
    )
    quota_record = next(r for r in records if Path(r.worktree_path).name == "quota-task")

    remove_abandoned_worktree(quota_record, abandoned_dir=abandoned_dir)

    # Removed via `git worktree remove` -- gone from disk AND from git's
    # own bookkeeping, never a raw `rm -rf`.
    assert not worktree.path.exists()
    assert not any(str(worktree.path) in line for line in list_worktrees(git_repo))
    assert not (abandoned_dir / "quota-task.json").exists()

    # The marker for the still-abandoned crash orphan is untouched.
    remaining = list_abandoned_worktrees(abandoned_dir)
    assert len(remaining) == 1
    assert remaining[0].reason == ABANDON_REASON_CRASH_ORPHANED
    assert keep_worktree.path.exists()


# --- Scenario 3: a merged or rejected worktree doesn't appear -------------


def test_merged_worktree_does_not_appear_in_abandoned_list(git_repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    abandoned_dir = tmp_path / "abandoned"

    worktree = create_worktree(git_repo, worktrees_dir, name="merge-me")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)
    (worktree.path / "README.md").write_text("hello\nmerged change\n", encoding="utf-8")

    result = merge(worktree, snapshot, approved=True)
    assert result.merged is True

    # Finishing a merge decision always tears the worktree down (U4's
    # `remove_worktree()`) -- that's what keeps a merged worktree out of
    # `git worktree list` at all, and therefore out of U8's scan, which
    # only ever looks at what git itself still reports. `force=True`
    # because the worktree has an uncommitted change in it (git otherwise
    # refuses to remove a dirty worktree) -- unrelated to U8's own logic.
    remove_worktree(worktree, force=True)

    records = abandoned_worktree_report(
        git_repo, worktrees_dir=worktrees_dir, abandoned_dir=abandoned_dir
    )
    assert records == []


def test_rejected_worktree_does_not_appear_in_abandoned_list(git_repo, tmp_path):
    worktrees_dir = tmp_path / "worktrees"
    abandoned_dir = tmp_path / "abandoned"

    worktree = create_worktree(git_repo, worktrees_dir, name="reject-me")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)
    (worktree.path / "README.md").write_text("hello\nrejected change\n", encoding="utf-8")

    result = merge(worktree, snapshot, approved=False)
    assert result.merged is False

    # Same `force=True` note as the merged-worktree test above -- the
    # worktree still has its (rejected) uncommitted change in it.
    remove_worktree(worktree, force=True)

    records = abandoned_worktree_report(
        git_repo, worktrees_dir=worktrees_dir, abandoned_dir=abandoned_dir
    )
    assert records == []


def test_still_open_merge_decision_worktree_is_not_treated_as_orphaned_while_present(
    git_repo, tmp_path
):
    """A worktree whose diff simply hasn't been reviewed/decided yet (still
    sitting on disk, mid-review) is indistinguishable from a crash orphan
    by this scan -- both are just 'git still reports it, no marker yet'.
    That's intentional (see plan Approach): anything git still reports
    with no record is surfaced for the developer to triage via `josu
    cleanup`, never silently assumed to be either safe or lost."""
    worktrees_dir = tmp_path / "worktrees"
    worktree = create_worktree(git_repo, worktrees_dir, name="pending-review")
    snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    orphans = find_orphaned_worktrees(git_repo, worktrees_dir)

    assert [o.path.name for o in orphans] == ["pending-review"]
