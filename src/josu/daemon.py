"""Long-running process hosting the context-graph MCP server (and, once
built, the local-delegate MCP server) over local HTTP/SSE.

Not stdio: stdio transport spawns a fresh server process per Claude Code
session, which would give every worktree its own graph-engine instance and
(once the delegate server is mounted here too) its own delegate queue --
defeating the reason a single shared daemon exists. Every per-worktree
--mcp-config manifest and every direct-call path (quota fallback, proactive
checks) points at this same running instance. See plan Key Technical
Decisions.

U12: startup loads `josu.toml` (via `config/__init__.py`, which composes
U11's candidate registry and U3's fallback-chain schema) and passes the
resolved config into the delegate server's construction, replacing the
single hardcoded `delegate_model` string U1/U2 shipped with -- the daemon
no longer knows about "a model", only about the candidate/chain config a
developer has authored.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from josu.config import JosuConfig, load_config
from josu.delegate.internal_api import build_delegate_internal_route
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS
from josu.delegate.queue import DelegateQueue
from josu.delegate.server import build_server as build_delegate_server
from josu.graph.build import GraphifyEngine
from josu.graph.server import build_server as build_graph_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def create_app(
    graph_out_dir: Path,
    target: Path | None = None,
    config: JosuConfig | None = None,
    config_path: Path | None = None,
    delegate_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    delegate_client_factory=None,
) -> Starlette:
    """Build the Starlette ASGI app mounting both MCP servers over SSE.

    If no graph has been built yet at `graph_out_dir`, builds one over
    `target` (defaulting to the current directory) before serving --
    otherwise the daemon would come up with nothing to answer queries with.

    `config` is the already-loaded `josu.toml` contents; if omitted, it's
    loaded here via `config/__init__.py.load_config(config_path)`
    (`config_path=None` resolves the XDG-style default location). Passing
    `config` directly lets tests construct a fixture config without writing
    a real file to disk when they don't need to also exercise path
    resolution/permission checks.

    The delegate server shares this same process and holds the one graph
    engine instance, so its queue (U2) is genuinely process-wide rather than
    per-worktree, and it queries the same graph the orchestrator does (R8).

    U14: exactly ONE `DelegateQueue` is constructed here and shared between
    `build_delegate_server()` (the MCP tool, `delegate_to_local`) and
    `build_delegate_internal_route()` (the new internal HTTP endpoint used
    by `josu delegate` and the commit hook) -- previously `build_server()`
    constructed its own queue internally with no way for a second caller to
    reach it, which meant "the daemon owns the one canonical queue" wasn't
    actually true. Both entry points now serialize through this single
    instance's lock.

    `delegate_client_factory` (U14 test seam): threaded into both the MCP
    tool and the internal route, mirroring `build_delegate_server()`'s own
    existing `client_factory` parameter -- lets tests inject a fake
    `DelegateClient` (e.g. to prove a candidate is never contacted) for a
    daemon started through this same production entry point, without
    reaching over the network. `None` (the default) preserves existing
    production behavior unchanged.
    """
    if config is None:
        config = load_config(config_path)

    engine = GraphifyEngine(out_dir=graph_out_dir)
    if not engine.graph_path.exists():
        engine.build(target or Path.cwd())
    graph_server = build_graph_server(engine)
    graph_sse = SseServerTransport("/graph/messages/")

    registry = {candidate.name: candidate for candidate in config.delegate.candidates}
    delegate_queue = DelegateQueue()
    delegate_server = build_delegate_server(
        graph_engine=engine,
        chains_config=config.chains,
        registry=registry,
        timeout=delegate_timeout,
        queue=delegate_queue,
        client_factory=delegate_client_factory,
    )
    delegate_sse = SseServerTransport("/delegate/messages/")

    # U14: the internal route shares `delegate_queue`, `config.chains`, and
    # `registry` with the MCP tool above -- see this module's and
    # `delegate/internal_api.py`'s docstrings for the queue-sharing
    # rationale and the deliberate loopback-TCP-vs-UDS trust-boundary
    # decision (mounted into this same app/port, not a separate UDS
    # listener, given `run()`'s current single-blocking-`uvicorn.run()`
    # structure).
    internal_delegate_route = build_delegate_internal_route(
        queue=delegate_queue,
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
    return Starlette(routes=routes)


def run(
    graph_out_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    target: Path | None = None,
    config: JosuConfig | None = None,
    config_path: Path | None = None,
    delegate_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    app = create_app(
        graph_out_dir,
        target=target,
        config=config,
        config_path=config_path,
        delegate_timeout=delegate_timeout,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
