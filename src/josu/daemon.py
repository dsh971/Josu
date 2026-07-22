"""Long-running process hosting the context-graph MCP server (and, once
built, the local-delegate MCP server) over local HTTP/SSE.

Not stdio: stdio transport spawns a fresh server process per Claude Code
session, which would give every worktree its own graph-engine instance and
(once the delegate server is mounted here too) its own delegate queue --
defeating the reason a single shared daemon exists. Every per-worktree
--mcp-config manifest and every direct-call path (quota fallback, proactive
checks) points at this same running instance. See plan Key Technical
Decisions.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from josu.graph.build import GraphifyEngine
from josu.graph.server import build_server as build_graph_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def create_app(graph_out_dir: Path, target: Path | None = None) -> Starlette:
    """Build the Starlette ASGI app mounting the graph MCP server over SSE.

    If no graph has been built yet at `graph_out_dir`, builds one over
    `target` (defaulting to the current directory) before serving --
    otherwise the daemon would come up with nothing to answer queries with.
    """
    engine = GraphifyEngine(out_dir=graph_out_dir)
    if not engine.graph_path.exists():
        engine.build(target or Path.cwd())
    graph_server = build_graph_server(engine)
    graph_sse = SseServerTransport("/graph/messages/")

    async def handle_graph_sse(request):
        async with graph_sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await graph_server.run(
                streams[0], streams[1], graph_server.create_initialization_options()
            )
        return Response()

    # The delegate server (U2) mounts here once built, at /delegate/sse and
    # /delegate/messages/ -- same pattern, a second engine instance sharing
    # this process so its queue is genuinely process-wide, not per-worktree.

    routes = [
        Route("/graph/sse", endpoint=handle_graph_sse, methods=["GET"]),
        Mount("/graph/messages/", app=graph_sse.handle_post_message),
    ]
    return Starlette(routes=routes)


def run(
    graph_out_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    target: Path | None = None,
) -> None:
    app = create_app(graph_out_dir, target=target)
    uvicorn.run(app, host=host, port=port, log_level="warning")
