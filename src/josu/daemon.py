"""Long-running process hosting the context-graph and delegate MCP servers
over local HTTP/SSE.

Not stdio: stdio transport spawns a fresh server process per Claude Code
session, which would give every worktree its own graph-engine instance and
its own delegate queue -- defeating the reason a single shared daemon
exists. Every per-worktree --mcp-config manifest and every direct-call path
(quota fallback, proactive checks) points at this same running instance.
See plan Key Technical Decisions.

josu never installs, spawns, or owns a graph-engine process -- it connects
to whatever `[[graph.engines]]` target `josu.toml` declares (see
`config/graph_engines.py`), the same way it already treats the hosted CLI
agent it drives as a user-installed prerequisite, not something it manages
itself. `_resolve_graph_engine_target()` below reads that config and checks
reachability; an absent or unreachable target degrades to no graph engine
for the session rather than blocking startup (R1/R7) -- `RoutingEngine`
(`graph/router.py`) is what turns that "no engine" state into the same
`GraphEngineUnavailableError` an unreachable engine would raise, so
existing fallback handling doesn't need to know why the engine is missing.
There is no daemon-owned process lifecycle left to tear down on shutdown.

Every route (graph/delegate SSE, the one remaining internal POST endpoint)
requires the daemon's shared-secret token (`daemon_auth.py`) -- loopback
binding alone authenticates nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from josu.config import JosuConfig, load_config
from josu.daemon_auth import (
    DaemonAuthMiddleware,
    load_or_create_daemon_token,
    resolve_daemon_token_path,
)
from josu.delegate.cooldown import CandidateCooldownStore
from josu.delegate.internal_api import build_delegate_internal_route
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS
from josu.delegate.queue import DelegateQueue
from josu.delegate.server import build_server as build_delegate_server
from josu.graph.gortex import GortexEngine
from josu.graph.gortex_process import (
    GortexProcess,
    check_gortex_reachable,
    check_gortex_tool_surface_capable,
    check_gortex_version_compatible,
)
from josu.graph.router import RoutingEngine
from josu.graph.server import build_server as build_graph_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _resolve_graph_engine_target(
    config: JosuConfig, *, quiet: bool = False
) -> tuple[GortexProcess | None, str | None]:
    """Read the configured `[[graph.engines]]` target (only the first
    entry is ever active -- see `config/graph_engines.py`), check
    reachability, resolve its optional credential, and run the version/
    tool-surface compatibility guard (R3). Returns `(target, reason)` --
    never raises -- `target` is `None` when no target is configured, the
    configured one isn't reachable, or it fails a compatibility check; the
    caller degrades to no graph engine for the session either way (R1/R7).
    `reason` is a short machine-stable string naming *why* (`"unconfigured"`,
    `"unreachable"`, `"version-incompatible"`, `"incapable"`) when `target`
    is `None`, else `None` -- `RoutingEngine` (`graph/router.py`) threads
    this into the `GraphEngineUnavailableError` it raises on next use, so
    an agent driving a graph query sees the real cause instead of one
    generic "unconfigured" message regardless of which check actually
    failed. Every degrade condition also prints a `josu daemon: warning:
    ...` line naming what's wrong, so a misconfigured target is visible to
    a human watching the daemon's stdout too -- unless `quiet=True` (used
    by `RoutingEngine`'s lazy reprobe, which calls this every ~30s while no
    engine is available; printing the same warning on every reprobe would
    spam the daemon's log for the entire time a target stays down, when
    the user already saw it once at startup).
    """
    engines = config.graph_engines.engines
    if not engines:
        return None, "unconfigured"
    target = engines[0]

    auth_token = os.environ.get(target.api_key_env) if target.api_key_env else None

    if not check_gortex_reachable(target.host, target.port, auth_token=auth_token):
        if not quiet:
            print(
                f"josu daemon: warning: graph engine {target.name!r} at "
                f"{target.host}:{target.port} is not reachable -- proceeding without a "
                "graph engine for this session"
            )
        return None, "unreachable"

    version_compatible, version_detail = check_gortex_version_compatible(target.host)
    if version_detail and not quiet:
        print(f"josu daemon: warning: graph engine {target.name!r}: {version_detail}")
    if not version_compatible:
        return None, "version-incompatible"

    tool_surface_capable, capability_detail = check_gortex_tool_surface_capable(
        target.host, target.port, auth_token=auth_token
    )
    if not tool_surface_capable:
        if not quiet:
            print(f"josu daemon: warning: graph engine {target.name!r}: {capability_detail}")
        return None, "incapable"

    return GortexProcess(host=target.host, port=target.port, auth_token=auth_token), None


def create_app(
    target: Path | None = None,
    config: JosuConfig | None = None,
    config_path: Path | None = None,
    delegate_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    delegate_client_factory=None,
    gortex_process: GortexProcess | None = None,
) -> Starlette:
    """Build the Starlette ASGI app mounting both MCP servers over SSE, the
    one remaining internal POST route, and the auth middleware guarding all
    of them.

    `target` no longer names a repo gortex is spawned to index -- tracking
    a repo is entirely the user's own `gortex track` setup step now. It's
    still consumed here as `RoutingEngine`'s `scope_root`: the boundary a
    graphify-eligible `execute()` call's path must resolve inside, so the
    graph MCP surface can't read arbitrary files outside the daemon's own
    tracked repo (see `graph/router.py`).

    `gortex_process` (test seam): when given, used directly as the
    resolved graph-engine target instead of resolving one from `config`'s
    `[[graph.engines]]` section -- lets a test construct a daemon against a
    stub gortex HTTP server without a real `gortex` binary or a real
    `josu.toml` entry. `None` (the default) resolves the real target from
    config via `_resolve_graph_engine_target()`, which may itself be
    `None` (no target configured or reachable) -- in production, that's
    not an error; the daemon starts with no graph engine for the session
    (R1/R7), and `RoutingEngine` (below) is what makes every existing
    fallback path treat that the same as an unreachable one.

    `config` is the already-loaded `josu.toml` contents; if omitted, it's
    loaded here via `config/__init__.py.load_config(config_path)`.

    The delegate server shares this same process and holds the one graph
    engine instance, so its queue (U2) is genuinely process-wide rather than
    per-worktree, and it queries the same graph the orchestrator does (R8).

    U14: exactly ONE `DelegateQueue` is constructed here and shared between
    `build_delegate_server()` (the MCP tool) and
    `build_delegate_internal_route()` (the internal delegate HTTP route).
    The same holds for the one `CandidateCooldownStore` (feat/delegate-
    candidate-circuit-breaker plan), constructed alongside it.

    U15: the daemon's shared-secret auth token (`daemon_auth.py`) is
    loaded/created alongside `josu.toml`'s own XDG-style config directory
    and required on every route below via `DaemonAuthMiddleware`.
    """
    if config is None:
        config = load_config(config_path)

    # Surface config warnings (e.g. a group/world-readable josu.toml) at
    # startup instead of silently discarding them -- `load_config()`
    # computes these but nothing previously read `config.warnings` anywhere
    # in this codebase. This is `create_app()`'s first `print()` call --
    # otherwise a pure library function with no stdout side effects, called
    # directly by several test fixtures. Rejected the alternative of
    # threading the loaded config back out through `run()` so `cli.py`'s
    # `_cmd_daemon_start` could print it via this file's own existing
    # convention: that reshapes both functions' signatures to serve one
    # print statement. Printing here is the smaller, bounded change (code-
    # review discussion, feat/cli-ease-of-use plan).
    for warning in config.warnings:
        print(f"josu daemon: warning: {warning}")

    # Resolve the token path from `config.path` itself, not the separate
    # `config_path` parameter -- a caller passing a pre-built `config`
    # object directly (as this function's own docstring says is supported,
    # for tests) would otherwise silently fall back to the XDG default
    # token location even though `config.path` points elsewhere, causing
    # every authenticated request to fail with a token mismatch nothing
    # would explain. `config.path` is always the true resolved path
    # regardless of which parameter produced `config`.
    token_path = resolve_daemon_token_path(config.path.parent)
    token = load_or_create_daemon_token(token_path)

    def _build_primary_engine(
        *, quiet: bool = False
    ) -> tuple[GortexEngine | None, str | None]:
        resolved, reason = _resolve_graph_engine_target(config, quiet=quiet)
        if resolved is None:
            return None, reason
        return GortexEngine(resolved.base_url, auth_token=resolved.auth_token), None

    if gortex_process is not None:
        # Test seam: a fixed stub target -- not re-probed lazily, since
        # there's no real `_resolve_graph_engine_target()` call behind it.
        primary_engine: GortexEngine | None = GortexEngine(
            gortex_process.base_url, auth_token=gortex_process.auth_token
        )
        engine = RoutingEngine(primary_engine, scope_root=target)
    else:
        primary_engine, degrade_reason = _build_primary_engine()
        engine = RoutingEngine(
            primary_engine,
            primary_factory=lambda: _build_primary_engine(quiet=True),
            scope_root=target,
            primary_unavailable_reason=degrade_reason,
        )

    graph_server = build_graph_server(engine)
    graph_sse = SseServerTransport("/graph/messages/")

    registry = {candidate.name: candidate for candidate in config.delegate.candidates}
    delegate_queue = DelegateQueue()
    # Exactly ONE CandidateCooldownStore is constructed here and shared
    # between `build_delegate_server()` (the MCP tool) and
    # `build_delegate_internal_route()` (the internal delegate HTTP route),
    # mirroring `delegate_queue`'s own construct-once-share-both pattern --
    # a candidate tripped via either path is skipped via both.
    cooldown_store = CandidateCooldownStore(
        failure_threshold=config.candidate_failure_threshold,
        cooldown_seconds=config.candidate_cooldown_seconds,
    )
    delegate_server = build_delegate_server(
        graph_engine=engine,
        chains_config=config.chains,
        registry=registry,
        timeout=delegate_timeout,
        queue=delegate_queue,
        cooldown_store=cooldown_store,
        client_factory=delegate_client_factory,
    )
    delegate_sse = SseServerTransport("/delegate/messages/")

    internal_delegate_route = build_delegate_internal_route(
        queue=delegate_queue,
        cooldown_store=cooldown_store,
        chains_config=config.chains,
        registry=registry,
        graph_engine=engine,
        timeout=delegate_timeout,
        client_factory=delegate_client_factory,
    )

    async def handle_graph_sse(request):
        async with graph_sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await graph_server.run(
                streams[0], streams[1], graph_server.create_initialization_options()
            )
        return Response()

    async def handle_delegate_sse(request):
        async with delegate_sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await delegate_server.run(
                streams[0], streams[1], delegate_server.create_initialization_options()
            )
        return Response()

    routes = [
        Route("/graph/sse", endpoint=handle_graph_sse, methods=["GET"]),
        Mount("/graph/messages/", app=graph_sse.handle_post_message),
        Route("/delegate/sse", endpoint=handle_delegate_sse, methods=["GET"]),
        Mount("/delegate/messages/", app=delegate_sse.handle_post_message),
        internal_delegate_route,
    ]

    app = Starlette(routes=routes)
    return DaemonAuthMiddleware(app, token)


def run(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    target: Path | None = None,
    config: JosuConfig | None = None,
    config_path: Path | None = None,
    delegate_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    app = create_app(
        target=target,
        config=config,
        config_path=config_path,
        delegate_timeout=delegate_timeout,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
