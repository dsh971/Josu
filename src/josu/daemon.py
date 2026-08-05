"""Long-running process hosting the context-graph and delegate MCP servers
over local HTTP/SSE, plus the gortex subprocess the graph server depends on.

Not stdio: stdio transport spawns a fresh server process per Claude Code
session, which would give every worktree its own graph-engine instance and
its own delegate queue -- defeating the reason a single shared daemon
exists. Every per-worktree --mcp-config manifest and every direct-call path
(quota fallback, proactive checks) points at this same running instance.
See plan Key Technical Decisions.

U15: this daemon also owns the gortex subprocess's lifecycle -- spawning it
(or reusing a validated survivor from a prior crash) before constructing
`GortexEngine`, and terminating it on a clean shutdown. Startup blocks on
gortex's `/healthz` liveness probe only, never on gortex's own initial
index completing (see `graph/gortex_process.py`'s and the plan's Key
Technical Decisions) -- there is deliberately no `engine.build()` call at
startup at all: gortex indexes the path it was spawned with (`--index
<root>`) as part of starting up, so nothing here needs to separately kick
off a build the way the graphify-era `GraphifyEngine.build()` precondition
did.

Every route (graph/delegate SSE, both internal POST endpoints) requires the
daemon's shared-secret token (`daemon_auth.py`) -- loopback binding alone
authenticates nothing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
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
    DEFAULT_GORTEX_HOST,
    DEFAULT_GORTEX_PORT,
    GortexProcess,
    check_gortex_reachable,
    spawn_gortex,
    terminate_gortex,
)
from josu.graph.internal_api import build_graph_internal_route
from josu.graph.server import build_server as build_graph_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _ensure_gortex_running(
    target: Path, *, gortex_host: str, gortex_port: int
) -> GortexProcess:
    """Probe the expected gortex port first -- a validated survivor from a
    prior crash is reused (mirrors `cli.py`'s `scan_for_crash_orphaned_
    worktrees()` "surface, don't silently resume" precedent); otherwise a
    fresh subprocess is spawned. Never blindly spawns a second instance on
    top of one already there."""
    if check_gortex_reachable(gortex_host, gortex_port):
        return GortexProcess(host=gortex_host, port=gortex_port, popen=None)
    return spawn_gortex(target, host=gortex_host, port=gortex_port)


def create_app(
    target: Path | None = None,
    config: JosuConfig | None = None,
    config_path: Path | None = None,
    delegate_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    delegate_client_factory=None,
    gortex_host: str = DEFAULT_GORTEX_HOST,
    gortex_port: int = DEFAULT_GORTEX_PORT,
    gortex_process: GortexProcess | None = None,
) -> Starlette:
    """Build the Starlette ASGI app mounting both MCP servers over SSE, the
    two internal POST routes, and the auth middleware guarding all of them.

    `gortex_process` (test seam): when given, skips the real
    probe-then-spawn sequence entirely and uses this handle directly --
    lets a test construct a daemon against a stub gortex HTTP server
    without a real `gortex` binary on `PATH`. `None` (the default)
    preserves real production behavior (`_ensure_gortex_running()`).

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

    # Captured before `gortex_process` is shadowed by `resolved_gortex_
    # process` below -- a caller-supplied `gortex_process` (the test seam)
    # is never this daemon instance's to terminate, since it didn't spawn
    # it. Both the construction-failure `except` below and `lifespan`'s
    # clean-shutdown teardown gate `terminate_gortex()` on this same flag.
    owns_gortex_process = gortex_process is None
    resolved_gortex_process = gortex_process or _ensure_gortex_running(
        target or Path.cwd(), gortex_host=gortex_host, gortex_port=gortex_port
    )
    # Everything below can raise (a malformed josu.toml surfacing during
    # chain/registry construction, etc.) -- if it does, `resolved_gortex_
    # process` was already spawned above but `lifespan`'s teardown never
    # gets a chance to run (the app is never constructed/served), which
    # would otherwise leak the child process indefinitely.
    try:
        engine = GortexEngine(resolved_gortex_process.base_url)

        graph_server = build_graph_server(engine)
        graph_sse = SseServerTransport("/graph/messages/")

        registry = {candidate.name: candidate for candidate in config.delegate.candidates}
        delegate_queue = DelegateQueue()
        # feat/delegate-candidate-circuit-breaker plan: exactly ONE
        # CandidateCooldownStore is constructed here and shared between
        # `build_delegate_server()` (the MCP tool) and
        # `build_delegate_internal_route()` (the internal delegate HTTP
        # route), mirroring `delegate_queue`'s own construct-once-share-both
        # pattern immediately above -- a candidate tripped via either path
        # is skipped via both.
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
        internal_graph_route = build_graph_internal_route(engine=engine)
    except Exception:
        if owns_gortex_process:
            terminate_gortex(resolved_gortex_process)
        raise

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
        internal_graph_route,
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        # Clean-shutdown teardown, gated on `owns_gortex_process` (see
        # above). uvicorn translates SIGINT/SIGTERM into this ASGI lifespan
        # "shutdown" event; a non-clean exit (`kill -9`, OOM) does not, and
        # the orphaned gortex process is left for the next `daemon start`'s
        # reuse/conflict probe -- deliberate, see `gortex_process.py`'s
        # module docstring.
        if owns_gortex_process:
            terminate_gortex(resolved_gortex_process)

    app = Starlette(routes=routes, lifespan=lifespan)
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
