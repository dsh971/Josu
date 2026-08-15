"""Engine-routing composing layer (U6, U8).

Composes the configured primary graph engine (gortex by default, connected
per `config/graph_engines.py`'s `[[graph.engines]]` -- see `daemon.py`) and
a lazily-constructed `GraphifyEngine` (U7) behind the single `GraphEngine`
Protocol `daemon.py`'s server/route construction already expects, so those
call sites never change regardless of which engines are actually available.
`daemon.py` constructs exactly one `RoutingEngine` and hands it everywhere
`GortexEngine` used to go directly.

Routing rule (R10): `search()` has no file path to route on and always
goes to the primary engine -- graphify's content was never part of a
unified free-text index. `execute()` inspects `params.get("path")`: a
graphify-eligible extension there dispatches to graphify (lazily
constructing it, printing a one-time install instruction if the package
isn't present -- R9), everything else dispatches to the primary engine.
`build()` always goes to the primary engine -- its `root` argument is a
directory, never a single graphify-eligible file, so there's nothing to
route there. `update()` also always goes to the primary engine: both
`GortexEngine.update()` (U4) and `GraphifyEngine.update()` (U7) are no-ops,
and routing a mere change-notification to graphify would spuriously
trigger R9's lazy-install instruction for a file nobody has actually asked
about yet -- that should fire on real content requests (`execute()`) only.

With the primary engine unset (no target configured or reachable -- R7),
a call that would have gone to it raises the same `GraphEngineUnavailableError`
an unreachable engine raises, so existing fallback handling
(`delegate/local_model.py`'s `_graph_context()`, `graph/server.py`'s
`call_tool()`) is unaffected by which underlying reason triggered it. A
graphify-eligible `execute()` call still routes to graphify even when the
primary engine is unset -- declining a graph-engine target and installing
graphify are independent, unrelated decisions.

A target absent or unreachable at daemon startup is not permanent: when
constructed with `primary_factory`, `_require_primary()` re-probes (rate
-limited) on next use rather than leaving the session stuck until a daemon
restart. `scope_root`, when given, confines every graphify dispatch to
that root so an MCP `execute()` call can't read arbitrary files outside
the daemon's own tracked repo.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from josu.graph.engine import GraphEngine, GraphEngineUnavailableError
from josu.graph.graphify import RECOGNIZED_EXTENSIONS, GraphifyEngine


def _graphify_install_hint(path: str) -> str:
    return (
        f"josu: graphify is not installed -- run `uv sync --extra graphify` (or "
        f"`pip install josu[graphify]`) to enable Excel/Word/Google-Workspace context "
        f"for {path}"
    )


# Human-readable detail for each machine-stable reason `daemon.py`'s
# `_resolve_graph_engine_target()` can hand back -- keyed the same as the
# `reason` values it returns, so a caller catching `GraphEngineUnavailableError`
# and inspecting `.reason` gets an actionable answer, not a coincidentally
# matching string. Falls back to the pre-existing generic message for a
# reason not in this table (e.g. no `primary_factory` was ever supplied, as
# in most standalone tests).
_UNAVAILABLE_DETAIL_BY_REASON: dict[str, str] = {
    "unreachable": "the configured graph engine is not reachable",
    "version-incompatible": "the configured graph engine reports an incompatible version",
    "incapable": "the configured graph engine is missing the required tool preset",
}
_DEFAULT_UNAVAILABLE_DETAIL = "no graph-engine target is configured or reachable"


class RoutingEngine:
    """Composes a primary graph engine (optional -- unset when no target
    is configured or reachable) and a lazily-constructed `GraphifyEngine`
    behind the `GraphEngine` Protocol.

    `primary_factory`, when given, is invoked (off the event loop, via
    `asyncio.to_thread`) to re-resolve the primary engine whenever it's
    still unset at call time -- a target absent or unreachable at daemon
    startup is re-checked lazily on next use, not cached as permanently
    unusable, per the plan's own requirement. Re-probes are rate-limited
    (`_REPROBE_INTERVAL_SECONDS`) so a genuinely absent target doesn't add
    a fresh reachability/version/capability probe round-trip to every
    single query. Once a probe succeeds, the result is kept for the rest
    of the process lifetime (no re-probing a working engine away). It must
    return `(engine, reason)`: the resolved engine (or `None`), and --
    only when `None` -- a short machine-stable string naming why (see
    `_UNAVAILABLE_DETAIL_BY_REASON`), so a query made after a failed
    reprobe raises with the real cause instead of a generic message.
    `primary_unavailable_reason` seeds the same state for the *initial*
    eager resolution done before construction (`daemon.py`'s startup
    call), so a query made before the first lazy reprobe is also covered.
    Without this threading, every degrade cause -- unreachable,
    incompatible version, missing tool preset -- collapsed into one
    indistinguishable `reason="unconfigured"` error at query time, even
    though `daemon.py` knew exactly which check failed at startup.

    `scope_root`, when given, confines every graphify-eligible `execute()`
    path to that root (typically the daemon's own tracked repo) -- without
    it, `GraphifyEngine` would read any path an MCP caller names, with no
    relationship to the hosted adapter's own `Read({worktree}/**)`
    allowlist. A relative `path` is resolved against `scope_root`, not the
    daemon process's own working directory, so confinement (and the path
    graphify actually opens) doesn't depend on where `josu daemon start`
    happened to be launched from.
    """

    _REPROBE_INTERVAL_SECONDS = 30.0

    def __init__(
        self,
        primary: GraphEngine | None,
        *,
        primary_factory: Callable[[], tuple[GraphEngine | None, str | None]] | None = None,
        scope_root: Path | None = None,
        primary_unavailable_reason: str | None = None,
    ) -> None:
        self._primary = primary
        self._primary_factory = primary_factory
        self._scope_root = scope_root.resolve() if scope_root is not None else None
        self._graphify: GraphifyEngine | None = None
        self._graphify_instruction_shown = False
        self._last_reprobe_at = 0.0
        self._unavailable_reason = primary_unavailable_reason if primary is None else None

    async def _require_primary(self) -> GraphEngine:
        if self._primary is None and self._primary_factory is not None:
            now = time.monotonic()
            if now - self._last_reprobe_at >= self._REPROBE_INTERVAL_SECONDS:
                self._last_reprobe_at = now
                self._primary, self._unavailable_reason = await asyncio.to_thread(
                    self._primary_factory
                )
        if self._primary is None:
            reason = self._unavailable_reason or "unconfigured"
            detail = _UNAVAILABLE_DETAIL_BY_REASON.get(reason, _DEFAULT_UNAVAILABLE_DETAIL)
            raise GraphEngineUnavailableError(
                f"{detail} -- see `[[graph.engines]]` in josu.toml",
                reason=reason,
            )
        return self._primary

    @staticmethod
    def _is_graphify_path(path_value: object) -> bool:
        if not isinstance(path_value, str) or not path_value:
            return False
        return Path(path_value).suffix.lower() in RECOGNIZED_EXTENSIONS

    def _resolve_scoped_path(self, path_value: str) -> Path:
        """Resolve `path_value` to an absolute path, anchoring a relative
        one to `scope_root` rather than the daemon process's own current
        working directory. Without this, a caller-supplied relative path
        (e.g. `"docs/notes.docx"`) would resolve against wherever `josu
        daemon start` happened to be invoked from -- which the daemon's
        own docs never require to match the tracked repo -- rather than
        the repo the path is actually meant to be relative to, making
        confinement checking (and extraction) depend on process launch
        location instead of the declared scope."""
        path = Path(path_value)
        if not path.is_absolute() and self._scope_root is not None:
            path = self._scope_root / path
        return path.resolve()

    def _check_in_scope(self, resolved: Path, path_value: str) -> None:
        if self._scope_root is None:
            return
        if resolved != self._scope_root and self._scope_root not in resolved.parents:
            raise GraphEngineUnavailableError(
                f"{path_value} is outside the tracked repo root ({self._scope_root}) -- "
                "graphify only reads files within the daemon's own worktree/repo scope",
                reason="out-of-scope",
            )

    def _resolve_graphify(self, path_value: str) -> GraphifyEngine:
        if self._graphify is not None:
            return self._graphify
        try:
            import graphify  # noqa: F401
        except ImportError as exc:
            if not self._graphify_instruction_shown:
                print(_graphify_install_hint(path_value))
                self._graphify_instruction_shown = True
            raise GraphEngineUnavailableError(
                f"graphify is not installed -- cannot extract {path_value}. "
                f"{_graphify_install_hint(path_value)}",
                reason="graphify-not-installed",
            ) from exc
        self._graphify = GraphifyEngine()
        return self._graphify

    async def build(self, root: Path) -> None:
        primary = await self._require_primary()
        await primary.build(root)

    async def update(self, root: Path, changed_files: list[Path]) -> None:
        primary = await self._require_primary()
        await primary.update(root, changed_files)

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        primary = await self._require_primary()
        return await primary.search(query, limit)

    async def execute(self, operation: str, params: dict) -> dict:
        path_value = params.get("path") if isinstance(params, dict) else None
        if self._is_graphify_path(path_value):
            resolved = self._resolve_scoped_path(path_value)
            self._check_in_scope(resolved, path_value)
            engine = self._resolve_graphify(path_value)
            return await engine.execute(operation, {**params, "path": str(resolved)})
        primary = await self._require_primary()
        return await primary.execute(operation, params)
