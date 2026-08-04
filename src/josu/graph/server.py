"""MCP server exposing the context graph as a fixed two-tool surface (R7, R12).

Deliberately narrow: `search` and `execute` are the only two tools regardless
of how many operations the underlying graph engine supports, so the schema
footprint stays constant as the graph grows richer (the Code-Mode pattern --
see origin Sources/Research). This is a thin proxy in front of whatever
GraphEngine is configured, not a wrap of graphify's own native MCP server
(which exposes seven tools and would reintroduce the bloat this avoids).
"""

from __future__ import annotations

import json

import mcp.types as types
from mcp.server.lowlevel import Server

from josu.graph.engine import GraphEngine

_SEARCH_DESCRIPTION = (
    "Fuzzy/substring search over the codebase's context graph. Returns matching "
    "nodes (functions, classes, files) plus their immediate neighbors for context. "
    "Use this to find where something is defined or referenced before reading files "
    "directly. Results reflect the primary repo's last indexed commit, not this "
    "task's own in-progress edits -- treat a miss as inconclusive, not certain."
)

_EXECUTE_DESCRIPTION = (
    "Run a specific graph operation by name (e.g. relationship/impact queries -- "
    "'who calls this', 'what breaks if I change this', dependency/dataflow analysis -- "
    "consult the graph engine's own tool catalogue for the full operation list). "
    "Prefer `search` for open-ended exploration; use `execute` when you already know "
    "the specific operation you need. If a delegation call returns an error "
    "indicating the graph is unreachable or has no data for this query, fall back to "
    "reading files directly rather than retrying the same operation."
)


def build_server(engine: GraphEngine, name: str = "context-graph") -> Server:
    """Construct the low-level MCP server bound to the given graph engine."""
    server: Server = Server(name)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="search",
                description=_SEARCH_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text"},
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10)",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="execute",
                description=_EXECUTE_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "description": "The graph operation name to invoke.",
                        },
                        "params": {"type": "object"},
                    },
                    "required": ["operation"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        try:
            if name == "search":
                query = arguments["query"]
                limit = int(arguments.get("limit", 10))
                result = await engine.search(query, limit=limit)
            elif name == "execute":
                operation = arguments["operation"]
                params = arguments.get("params", {})
                result = await engine.execute(operation, params)
            else:
                return [
                    types.TextContent(
                        type="text", text=f"unknown tool: {name}", annotations=None
                    )
                ]
        except (KeyError, ValueError, RuntimeError) as exc:
            return [
                types.TextContent(
                    type="text", text=f"error: {exc}", annotations=None
                )
            ]
        return [
            types.TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False), annotations=None
            )
        ]

    return server
