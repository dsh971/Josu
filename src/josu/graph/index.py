"""Incremental re-indexing triggered by commit/save/merge events (U10, R14).

Plan claim vs. graphify's real API (verified by reading the installed
package's source, not just its `SKILL.md`): the plan says graphify ships
"manifest-based change detection (`detect_incremental`) and a
merge-into-existing-graph build path (`build_merge`)" -- both exist exactly
as named, imported the same way `build.py` (U1) already imports them.
Confirmed, not corrected -- BUT with one important nuance the plan's Approach
text glosses over: `detect_incremental` determines "what changed" by calling
`detect()` over the ENTIRE directory tree and diffing every file's
mtime/hash against the manifest -- there is no parameter to bound that scan
to a caller-supplied file list. That's exactly right for `GraphifyEngine.update()`
(U1), which doesn't yet know what changed and has to ask graphify to find
out. It is the wrong tool here: a commit, a save event, or a completed merge
(U5) already hands this module an exact, bounded list of changed paths --
recomputing "what changed" via a whole-tree scan would be redundant and,
more importantly, would silently drop the "already-known set of changed
files" guarantee U10 explicitly asks for (nothing stops `detect_incremental`
from ALSO turning up an unrelated modified file elsewhere in the tree if one
happened to exist, which is precisely the failure mode the "unrelated file
is untouched" test scenario below guards against).

So this module calls `graphify.extract.extract()` and `graphify.build.build_merge()`
directly on the bounded list every trigger below computes, and stamps
graphify's own manifest (`graphify.detect.save_manifest()`) for exactly that
list -- never `detect_incremental`. `save_manifest()`'s own docstring
confirms this is an intended, supported usage, not a workaround: "Callers
saving a SUBSET of files (changed_paths hooks, skill runbooks, #917) must
leave [scan_corpus] None so their untouched rows are preserved."

Three triggers, matching the plan's "same commit/save events as U9 plus when
a merge (U5) completes":

- `reindex_on_commit()` -- the same commit event `proactive/watchers.py`'s
  `install_commit_hook()` (U9) fires a post-commit hook on.
- `reindex_on_save()` -- the same save event `proactive/watchers.py`'s
  `DebouncedSaveWatcher` (U9) fires on. A single saved file already IS the
  bounded change set; no git diff is needed to compute it.
- `reindex_on_merge()` -- fires once `orchestrator/merge.py`'s `merge()`
  (U5) returns `MergeResult(merged=True, ...)`.

All three funnel through `reindex_changed_files()`, the actual bounded
re-index primitive, which is deliberately NOT `GraphifyEngine.update()` (U1,
`build.py`) for the reason above. `build.py` is out of this unit's file
scope (see plan's U10 Files list), so this module reaches into
`GraphifyEngine`'s `_graph`/`_root` fields directly to hand a live engine
instance the freshly merged graph, mirroring what `build()`/`update()`
already do to themselves internally -- and matching `test_build.py`'s own
established convention of poking `_root` directly from outside the class
(`test_reload_from_disk_after_process_restart`).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import networkx as nx
from graphify.build import build_merge
from graphify.detect import FileType, classify_file, save_manifest
from graphify.extract import extract
from graphify.paths import GRAPHIFY_OUT

from josu.graph.build import GraphifyEngine

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
    set fails -- mirrors `orchestrator/merge.py`'s `MergeError`."""


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand as an argv list (`shell=False`), never a shell
    string -- mirrors `worktree.py`'s and `merge.py`'s subprocess-safety
    contract."""
    subcommand = args[0] if args else ""
    if subcommand not in _GIT_INDEX_SUBCOMMANDS:
        raise ReindexError(
            f"git subcommand {subcommand!r} is not in index.py's own allowlist "
            f"{sorted(_GIT_INDEX_SUBCOMMANDS)}"
        )
    argv = ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(argv, shell=False, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise ReindexError(f"git {' '.join(args)} failed in {cwd}: {exc.stderr.strip()}") from exc
    except FileNotFoundError as exc:
        raise ReindexError(f"git executable not found: {exc}") from exc


def _parse_name_status(output: str) -> list[str]:
    """Parse `git diff[-tree] --name-status` output into a flat path list.
    A rename/copy ("R100\\told\\tnew" / "C100\\told\\tnew") yields BOTH the
    old and new path -- the old path still matters (it no longer exists on
    disk post-rename, so `reindex_changed_files()`'s own exists()-based
    split routes it to pruning); an add/modify/delete line yields its one
    path. Mirrors `orchestrator/merge.py`'s `_changed_paths()` parsing,
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
    """What one bounded re-index touched: the code files actually
    extracted and merged into the graph, and any deleted files pruned from
    it. Both empty is a deliberate no-op (nothing in the bounded change set
    qualified), not an error."""

    reindexed_files: list[str] = field(default_factory=list)
    pruned_files: list[str] = field(default_factory=list)


def _persist(graph_path: Path, graph: nx.Graph) -> None:
    """Write `graph` to `graph_path` in the same shape
    `GraphifyEngine._persist()` (U1, `build.py`) writes and
    `GraphifyEngine._require_graph()` reads back. Duplicated here (a few
    lines) rather than calling the private `_persist()` method across
    modules -- `build_merge()` itself only returns the merged graph, it
    never writes it back out (despite its docstring's "and save back"
    phrasing; verified by reading its source), so *something* has to
    persist it, and this keeps that one small piece of format knowledge
    self-contained in this module instead of reaching further into
    `build.py`'s internals than the `_graph`/`_root` cache handoff already
    does.
    """
    data = {
        "nodes": [{"id": n, **d} for n, d in graph.nodes(data=True)],
        "edges": [{"source": u, "target": v, **d} for u, v, d in graph.edges(data=True)],
    }
    graph_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def reindex_changed_files(
    engine: GraphifyEngine, root: Path, changed_files: Sequence[Path]
) -> ReindexResult:
    """The bounded core of U10 (R14): given an explicit, already-known set
    of changed file paths -- from a commit, a save event, or a completed
    merge -- extract and merge ONLY those files into the existing graph.
    See module docstring for why this is deliberately not
    `GraphifyEngine.update()`.

    Non-code paths (per graphify's own `classify_file()`) are silently
    dropped -- this project's v1 scope is AST-only (see `build.py`'s module
    docstring). A path that no longer exists on disk is treated as a
    deletion and routed to `build_merge()`'s `prune_sources` instead of
    `extract()`. A path neither present-and-code nor deleted (e.g. a
    present-but-non-code file, or a duplicate) contributes nothing.

    Anything under `root`'s own `graphify-out/` cache directory (graphify's
    `GRAPHIFY_OUT` convention -- the same directory `extract(cache_root=root)`
    itself writes AST-cache JSON blobs into) is excluded up front, even
    though `.json` is one of graphify's own `CODE_EXTENSIONS` and would
    otherwise be misclassified as source: a commit or merge that happens to
    include that directory (e.g. a developer forgot to `.gitignore` it)
    must never feed graphify's own cache artifacts back into `extract()` as
    if they were code.

    A path outside `changed_files` is never even looked at, let alone
    reindexed -- there is no directory walk in this function at all.
    """
    root = root.resolve()
    cache_dir = (root / GRAPHIFY_OUT).resolve()
    all_resolved = {Path(f).resolve() for f in changed_files}
    resolved = sorted(p for p in all_resolved if not _under(p, cache_dir))

    code_files = [f for f in resolved if f.exists() and classify_file(f) == FileType.CODE]
    deleted_files = [str(f) for f in resolved if not f.exists()]

    if not code_files and not deleted_files:
        return ReindexResult()

    graph_path = engine.graph_path
    extraction = (
        extract(code_files, cache_root=root, root=root) if code_files else {"nodes": [], "edges": []}
    )

    merged = build_merge(
        [extraction],
        graph_path=str(graph_path) if graph_path.exists() else None,
        prune_sources=deleted_files or None,
        root=root,
    )

    _persist(graph_path, merged)
    # Hand the freshly merged graph straight to the live engine instance's
    # in-memory cache -- see module docstring for why this reaches into
    # `_graph`/`_root` directly rather than through a public setter.
    engine._graph = merged
    engine._root = root

    if code_files:
        save_manifest(
            {"code": [str(f) for f in code_files]},
            manifest_path=str(engine.manifest_path),
            kind="both",
            root=root,
        )
    elif engine.manifest_path.exists():
        # Deletions only, no code files re-extracted this round -- still
        # call save_manifest() with an empty file list so its own
        # unconditional "row's file no longer exists on disk -> drop it"
        # pass (graphify/detect.py) prunes the deleted paths' manifest rows
        # too, not just the graph itself.
        save_manifest({}, manifest_path=str(engine.manifest_path), kind="both", root=root)

    return ReindexResult(
        reindexed_files=[str(f) for f in code_files],
        pruned_files=deleted_files,
    )


# --- Trigger #1: commit event (same trigger as U9's post-commit hook) ----


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
    )
    return [root / p for p in _parse_name_status(result.stdout)]


def reindex_on_commit(engine: GraphifyEngine, root: Path, *, ref: str = "HEAD") -> ReindexResult:
    """U10 trigger #1: the same commit event `proactive/watchers.py`'s
    `install_commit_hook()` (U9) fires a post-commit hook on -- re-index
    exactly the files `ref` (default: the commit that was just made)
    touched, nothing else.
    """
    root = root.resolve()
    changed = _changed_paths_from_commit(root, ref)
    return reindex_changed_files(engine, root, changed)


# --- Trigger #2: save event (same trigger as U9's DebouncedSaveWatcher) --


def reindex_on_save(engine: GraphifyEngine, root: Path, saved_path: Path) -> ReindexResult:
    """U10 trigger #2: the same save event `proactive/watchers.py`'s
    `DebouncedSaveWatcher` (U9) fires on. A single saved file already IS
    the bounded change set -- no git diff needed to compute it.
    """
    return reindex_changed_files(engine, root, [saved_path])


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
    result = _run_git(["diff", "--name-status", "HEAD"], cwd=worktree.path)
    return [worktree.repo_root / p for p in _parse_name_status(result.stdout)]


def reindex_on_merge(
    engine: GraphifyEngine, worktree: "Worktree", merge_result: "MergeResult"
) -> ReindexResult:
    """U10 trigger #3: fires when `orchestrator/merge.py`'s `merge()` (U5)
    completes -- re-index exactly the paths that merge actually applied to
    `worktree.repo_root`, nothing else. A rejected merge, or one that never
    happened for any other reason (`merge_result.merged is False`), has
    nothing to reindex -- returns an empty `ReindexResult` immediately
    without touching the graph or even computing a diff.
    """
    if not merge_result.merged:
        return ReindexResult()
    changed = _changed_paths_from_worktree(worktree)
    return reindex_changed_files(engine, worktree.repo_root, changed)
