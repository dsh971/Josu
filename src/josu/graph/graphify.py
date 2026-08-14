"""GraphifyEngine: a narrow secondary graph engine for Excel, Word, and
Google-Workspace formats (`.xlsx`, `.docx`, `.gdoc`, `.gsheet`, `.gslides`)
-- the formats gortex doesn't ingest at all.

Scoped deliberately narrow: this is NOT a restore of the old AST/code-graph
`GraphifyEngine` (that job now belongs to gortex). This engine only converts
one file at a time to Markdown via the `graphifyy[office,google]` extras'
own conversion functions and returns that content -- there is no persistent
index to build or update, so `build()`/`update()` are no-ops, matching how
`GortexEngine`'s equivalents became no-ops for the same underlying reason
(nothing left for this engine to own ahead of a query).

`search()` is never actually called by `RoutingEngine` (see `graph/router.py`
-- search() has no path to route on, so it always goes to the primary
engine), but is implemented for Protocol conformance.

Office formats (`.docx`/`.xlsx`) convert in-process via `python-docx`/
`openpyxl` (the `graphifyy[office]` extra). Google Workspace shortcut
formats (`.gdoc`/`.gsheet`/`.gslides`) are NOT handled via any OAuth/API
credential josu's config touches at all -- graphify shells out to a
separate, user-installed `gws` CLI (googleworkspace CLI,
https://github.com/googleworkspace/cli), authenticated by the user's own
`gws auth login`. This mirrors this plan's own "declare, don't own"
posture: josu doesn't manage that credential any more than it manages
gortex's own auth token. A missing/unauthenticated `gws` surfaces as a
clear, actionable `GraphifyUnavailableError`, not a crash.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from josu.graph.engine import GraphEngineUnavailableError

# Mirrors gortex.py's DEFAULT_MAX_RESPONSE_BYTES -- an oversized converted
# file shouldn't buffer unbounded content into the daemon or the agent's
# context any more than an oversized gortex response would.
MAX_EXTRACTED_BYTES = 2_000_000

# Office formats: pure in-process conversion via python-docx/openpyxl.
_OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx"})

# Google Workspace shortcut formats: conversion shells out to the user's own
# `gws` CLI, not a pure in-process call.
_GOOGLE_WORKSPACE_EXTENSIONS = frozenset({".gdoc", ".gsheet", ".gslides"})

RECOGNIZED_EXTENSIONS = _OFFICE_EXTENSIONS | _GOOGLE_WORKSPACE_EXTENSIONS


class GraphifyUnavailableError(GraphEngineUnavailableError):
    """Raised when graphify can't extract a requested file -- unsupported
    format, conversion failure, or (for Google Workspace formats) `gws`
    being absent/unauthenticated on the user's machine."""


class GraphifyEngine:
    """`GraphEngine` Protocol implementation backed by `graphifyy`'s
    office/Google-Workspace conversion functions."""

    async def build(self, root: Path) -> None:
        """No-op -- see module docstring."""
        return None

    async def update(self, root: Path, changed_files: list[Path]) -> None:
        """No-op -- see module docstring."""
        return None

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """No free-text search across ingested content in this role --
        `RoutingEngine` never routes `search()` calls here. Always returns
        no results rather than raising, since an empty result (not an
        error) is the correct signal for "nothing matched" per the
        `GraphEngine` Protocol's own convention."""
        return []

    async def execute(self, operation: str, params: dict) -> dict:
        """The real entry point: `params["path"]` names the file to
        extract. `operation` itself is not currently branched on -- this
        engine's only job is per-file extraction, regardless of what the
        caller names the operation."""
        path_value = params.get("path")
        if not path_value:
            raise GraphifyUnavailableError(
                f"execute({operation!r}) has no 'path' in params -- GraphifyEngine only "
                "handles per-file extraction, reached via RoutingEngine's path-based dispatch",
                reason="no-path",
            )

        path = Path(path_value)
        ext = path.suffix.lower()
        if ext not in RECOGNIZED_EXTENSIONS:
            raise GraphifyUnavailableError(
                f"{path} has extension {ext!r}, not one of "
                f"{sorted(RECOGNIZED_EXTENSIONS)}",
                reason="unsupported-format",
            )

        content = await asyncio.to_thread(self._extract, path)
        if content is None:
            raise GraphifyUnavailableError(
                f"graphify could not extract any content from {path}",
                reason="extraction-failed",
            )
        return {"path": str(path), "content": content}

    def _extract(self, path: Path) -> str | None:
        """Synchronous extraction, run off the event loop via
        `asyncio.to_thread()` -- graphify's own conversion functions do
        real file/subprocess I/O with no async variant. Raises
        `GraphifyUnavailableError` on a `gws`-not-installed condition
        (Google Workspace formats only) so the caller gets a clear,
        actionable message instead of an unhandled exception; returns
        `None` for "converted successfully but produced no content" the
        same way graphify's own functions do.
        """
        from graphify.detect import convert_office_file
        from graphify.google_workspace import convert_google_workspace_file

        ext = path.suffix.lower()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            try:
                if ext in _OFFICE_EXTENSIONS:
                    sidecar = convert_office_file(path, out_dir)
                else:
                    sidecar = convert_google_workspace_file(path, out_dir)
            except RuntimeError as exc:
                raise GraphifyUnavailableError(str(exc), reason="conversion-error") from exc
            except subprocess.TimeoutExpired as exc:
                raise GraphifyUnavailableError(
                    f"converting {path} timed out -- the `gws` CLI may be waiting on "
                    "interactive re-authentication (`gws auth login`)",
                    reason="conversion-timeout",
                ) from exc

            if sidecar is None:
                return None
            size = sidecar.stat().st_size
            if size > MAX_EXTRACTED_BYTES:
                raise GraphifyUnavailableError(
                    f"{path} converted to {size} bytes, exceeding the "
                    f"{MAX_EXTRACTED_BYTES}-byte cap -- too large to return",
                    reason="content-too-large",
                )
            return sidecar.read_text(encoding="utf-8")
