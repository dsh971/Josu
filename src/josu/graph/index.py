"""Incremental re-indexing, previously triggered by commit/save/merge
events (U10, R14) -- now only the save-event path remains.

josu's own commit-hook-triggered and merge-triggered reindex calls were
retired (this plan's R4): a configured gortex's own continuous fsnotify
watcher is the sole reindex trigger once a repo is tracked. Running josu's
own trigger alongside it would double-trigger on the same changes, and
josu no longer even tells gortex what to track (`gortex track` is the
user's own setup step) -- it has no basis to also drive reindexing.

`reindex_on_save()` remains -- it already has zero production callers (no
editor-plugin/file-watcher save-event source exists anywhere in this
codebase), a separate, pre-existing gap this change doesn't touch either
way. Unlike the removed commit/merge triggers, it never needed git at all
-- a single saved file already IS the bounded change set. It still funnels
through `reindex_changed_files()`, which does no language/file-type
filtering itself -- gortex's own indexer decides file relevance on its own
side, not josu's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from josu.delegate.daemon_client import DaemonNotReachableError
from josu.graph.internal_api import GraphInternalError, post_graph_internal_reindex


@dataclass(frozen=True)
class ReindexResult:
    """What one bounded re-index touched: the paths sent to the daemon for
    reindexing, and any deleted paths reported as pruned. Both empty is a
    deliberate no-op (nothing in the bounded change set qualified), not an
    error. `engine_error` is set (rather than an exception raised) when the
    daemon's live engine itself was unreachable/erroring for this reindex
    attempt -- distinguishable from a normal empty result, since a
    silently-swallowed update failure would otherwise leave the graph
    arbitrarily stale with no visible signal."""

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
    of changed file paths ask the daemon's live graph engine (via
    `graph/internal_api.py`'s internal route) to re-index exactly those.

    A path outside `changed_files` is never even looked at -- there is no
    directory walk in this function at all. A path that no longer exists on
    disk is reported as pruned rather than sent as a reindex target.

    A `GraphInternalError`/`DaemonNotReachableError` from the daemon call is
    caught and reported via `ReindexResult.engine_error`, not raised --
    matching R13's "graph trouble degrades gracefully" posture rather than
    failing the triggering event outright.
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


async def reindex_on_save(
    root: Path,
    saved_path: Path,
    *,
    base_url: str,
    token: str | None = None,
    timeout: float = 120.0,
) -> ReindexResult:
    """The save event `proactive/watchers.py`'s `DebouncedSaveWatcher` (U9)
    fires on. A single saved file already IS the bounded change set -- no
    git diff needed to compute it."""
    return await reindex_changed_files(
        root, [saved_path], base_url=base_url, token=token, timeout=timeout
    )
