"""Runs one bounded delegate call against a configured candidate (R4).

Structures every response as `{result, caveats}` -- `caveats` carries the
delegate model's own uncertainty notes, which Claude Code's tool description
(U3) tells it to treat as a spot-check signal. Queries the same context
graph the hosted orchestrator uses (R8); when the graph has no relevant data,
falls back to direct file exploration within scope rather than blocking
(R13), since a graph miss is exactly as likely here as it is for U4's
orchestrator-side fallback.

`delegate()` is `async def` and client-agnostic -- it's typed against
`client.py`'s `DelegateClient` Protocol, not any one transport, so Ollama is
just one configured candidate pointed at its own OpenAI-compatible `/v1`
endpoint, not a special-cased code path. Serialization and per-call timeout
enforcement are `queue.py`'s job, not this module's.

The hardware pre-flight check (R26) does NOT run inside `delegate()` -- it
used to, but the local/remote distinction that check depends on lives with
chain-execution logic (U12's `chain.py`), which calls `preflight_check`
itself before attempting a candidate it already knows is local. This unit
does not reintroduce that gate; `delegate()` never gates on hardware fit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from josu.delegate.client import (
    DelegateClient,
    DelegateMalformedResponseError,
    OpenAICompatibleDelegateClient,
)
from josu.graph.engine import GraphEngine

DEFAULT_TIMEOUT_SECONDS = 60.0

# Ollama's own OpenAI-compatible endpoint -- used only when no client is
# injected, matching the previous module-level `ollama.chat` default.
DEFAULT_ENDPOINT = "http://localhost:11434/v1"

_SYSTEM_PROMPT = (
    'Respond only with a JSON object of the shape {"result": <string>, '
    '"caveats": <string>}. `result` is the outcome of the requested task. '
    "`caveats` must note any uncertainty, missing context, or assumptions "
    "you made -- leave it an empty string only if you are fully confident."
)


@dataclass(frozen=True)
class DelegateResult:
    result: str
    caveats: str
    model: str


def _coerce_task_args(task: Any, scope: Any) -> tuple[str, dict]:
    """Coerce malformed tool-call argument types before dispatch."""
    if not isinstance(task, str):
        task = str(task)
    if scope is None:
        scope = {}
    elif isinstance(scope, str):
        # A common malformed shape is a JSON-encoded string instead of an object.
        try:
            parsed = json.loads(scope)
            scope = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            scope = {}
    elif not isinstance(scope, dict):
        scope = {}
    return task, scope


def _parse_response(content: str) -> tuple[str, str]:
    data = json.loads(content)
    return str(data["result"]), str(data.get("caveats", ""))


def _graph_context(engine: GraphEngine | None, task: str) -> list[dict]:
    if engine is None:
        return []
    try:
        return engine.search(task, limit=5)
    except RuntimeError:
        return []


def _iter_files_sorted(root: Path) -> Iterator[Path]:
    """Depth-first walk of `root`, descending into each directory's entries
    in sorted-by-name order, yielding only files. A lazy generator -- unlike
    `sorted(root.rglob("*"))`, no directory's contents are listed or sorted
    until the walk actually reaches that directory, so a caller that only
    wants the first few results (via `itertools.islice`) never pays for
    subtrees it never visits.

    Ordering note: this is a per-directory-level sorted traversal, not a
    single global sort of full path strings -- the two agree for the
    overwhelming majority of trees (any tree without a file and a
    same-named-prefix sibling directory, e.g. both `a` and `a.txt` in one
    directory) and, in particular, agree for every tree of one file per
    directory depth actually seen in practice here.
    """
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            yield from _iter_files_sorted(entry)
        elif entry.is_file():
            yield entry


def _fallback_file_context(root: Path, limit: int = 5) -> list[str]:
    """Direct file exploration within scope when the graph has no relevant
    data (R13). Bounded by `limit` -- via a lazy, early-terminating walk --
    rather than materializing and sorting every path under `root` first."""
    if not root.exists():
        return []
    return [str(p) for p in islice(_iter_files_sorted(root), limit)]


async def delegate(
    task: Any,
    scope: Any = None,
    *,
    model: str = "qwen2.5-coder:7b",
    graph_engine: GraphEngine | None = None,
    scope_root: Path | None = None,
    client: DelegateClient | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> DelegateResult:
    """Run one bounded delegate call against `client` (or a default Ollama
    OpenAI-compatible client if none is given).

    Raises `DelegateUnreachableError`, `DelegateRateLimitedError`, or
    `DelegateAPIError` (from `client.py`) immediately, un-retried -- those
    feed U12's chain-advance logic, not a same-candidate retry. A malformed
    response (from the client itself, or from this function's own parse of
    the model's `{result, caveats}` content) triggers exactly one
    same-candidate retry before raising `DelegateMalformedResponseError`
    (R24).
    """
    task, scope = _coerce_task_args(task, scope)

    context = _graph_context(graph_engine, task)
    if not context and scope_root is not None:
        context = _fallback_file_context(scope_root)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps({"task": task, "scope": scope, "context": context}),
        },
    ]

    active_client = (
        client
        if client is not None
        else OpenAICompatibleDelegateClient(DEFAULT_ENDPOINT, name=model)
    )

    async def _attempt() -> tuple[str, str] | None:
        """One call-and-parse attempt. Returns None on ANY malformation
        (envelope-level from the client, or content-level from our own
        `{result, caveats}` parse) so both feed the same retry-once path.
        Unreachable/rate-limited/API-error exceptions are NOT caught here --
        they propagate immediately, un-retried."""
        try:
            content = await active_client.complete(model=model, messages=messages, timeout=timeout)
        except DelegateMalformedResponseError:
            return None
        try:
            return _parse_response(content)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        except RecursionError:
            # `content` itself (the model's own {result, caveats} envelope)
            # can independently be deeply nested JSON, even when the
            # outer HTTP response body parsed fine at the client layer --
            # same malformed-response treatment as a JSONDecodeError/KeyError
            # here, feeding the same retry-once-then-raise path (R24) rather
            # than crashing past it.
            return None

    parsed = await _attempt()
    if parsed is None:
        parsed = await _attempt()
    if parsed is None:
        raise DelegateMalformedResponseError(
            model, "response could not be parsed as {result, caveats} after retry"
        )

    result, caveats = parsed
    return DelegateResult(result=result, caveats=caveats, model=model)
