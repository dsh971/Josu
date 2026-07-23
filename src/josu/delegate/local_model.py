"""Wraps ollama.chat as the local delegate (R4).

Structures every response as `{result, caveats}` -- `caveats` carries the
local model's own uncertainty notes, which Claude Code's tool description
(U3) tells it to treat as a spot-check signal. Queries the same context
graph the hosted orchestrator uses (R8); when the graph has no relevant data,
falls back to direct file exploration within scope rather than blocking
(R13), since a graph miss is exactly as likely here as it is for U4's
orchestrator-side fallback.

`delegate()` itself is synchronous and blocking -- serialization and timeout
enforcement are queue.py's job (via asyncio.to_thread), not this module's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama

from josu.graph.engine import GraphEngine
from josu.models.curated import preflight_check

DEFAULT_TIMEOUT_SECONDS = 60.0

_SYSTEM_PROMPT = (
    'Respond only with a JSON object of the shape {"result": <string>, '
    '"caveats": <string>}. `result` is the outcome of the requested task. '
    "`caveats` must note any uncertainty, missing context, or assumptions "
    "you made -- leave it an empty string only if you are fully confident."
)


class LocalModelUnreachableError(RuntimeError):
    """Raised when Ollama itself cannot be reached (connection refused/daemon down)."""


class LocalModelMalformedResponseError(RuntimeError):
    """Raised when the local model's response can't be parsed even after one retry."""


@dataclass(frozen=True)
class DelegateResult:
    result: str
    caveats: str
    model: str
    preflight_warning: str | None = None


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


def _fallback_file_context(root: Path, limit: int = 5) -> list[str]:
    """Direct file exploration within scope when the graph has no relevant data (R13)."""
    if not root.exists():
        return []
    return [str(p) for p in sorted(root.rglob("*")) if p.is_file()][:limit]


def delegate(
    task: Any,
    scope: Any = None,
    *,
    model: str = "qwen2.5-coder:7b",
    graph_engine: GraphEngine | None = None,
    scope_root: Path | None = None,
    client: ollama.Client | None = None,
    available_ram_gb: float | None = None,
) -> DelegateResult:
    """Run one bounded delegate call against the local model.

    `available_ram_gb` overrides the real-machine reading `preflight_check`
    would otherwise take -- real RAM fluctuates run to run, so callers that
    only care about response parsing or connection handling (not R26 itself)
    should pass a fixed value to keep the pre-flight gate deterministic.
    """
    task, scope = _coerce_task_args(task, scope)

    preflight = preflight_check(model, available_ram_gb=available_ram_gb)
    if not preflight.ok:
        raise ValueError(f"pre-flight check failed: {preflight.reason}")

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

    chat = client.chat if client is not None else ollama.chat

    def _call() -> str:
        try:
            response = chat(model=model, messages=messages, format="json")
        except ConnectionError as exc:
            raise LocalModelUnreachableError(f"local model unreachable: {exc}") from exc
        return response["message"]["content"]

    content = _call()
    try:
        result, caveats = _parse_response(content)
    except (json.JSONDecodeError, KeyError, TypeError):
        # One retry on a genuinely unparseable response (R24).
        content = _call()
        try:
            result, caveats = _parse_response(content)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LocalModelMalformedResponseError(
                f"local model returned an unparseable response after retry: {content!r}"
            ) from exc

    return DelegateResult(
        result=result,
        caveats=caveats,
        model=model,
        preflight_warning=preflight.reason if not preflight.curated else None,
    )
