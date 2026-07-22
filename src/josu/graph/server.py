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
    "directly."
)

_EXECUTE_DESCRIPTION = (
    "Run a specific graph query. `operation` must be one of: 'get_node' (params: "
    "node_id), 'get_neighbors' (params: node_id), 'shortest_path' (params: source, "
    "target), 'graph_stats' (no params). Prefer `search` for open-ended exploration; "
    "use `execute` when you already know the specific node or relationship you need."
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
                            "enum": ["get_node", "get_neighbors", "shortest_path", "graph_stats"],
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
                result = engine.search(query, limit=limit)
            elif name == "execute":
                operation = arguments["operation"]
                params = arguments.get("params", {})
                result = engine.execute(operation, params)
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
