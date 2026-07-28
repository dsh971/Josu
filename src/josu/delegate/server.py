"""MCP server exposing the local model as a single `delegate_to_local` tool.

One tool, mirroring the graph server's fixed-surface pattern (U1): the local
delegate doesn't need multiple entry points, just a bounded task plus
optional scope. The full delegation policy (when to prefer this over staying
hosted) lives in U3's guide; this tool description carries only the
mechanical usage hint.
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as types
from mcp.server.lowlevel import Server

from josu.delegate.client import (
    DelegateAPIError,
    DelegateClient,
    DelegateMalformedResponseError,
    DelegateRateLimitedError,
    DelegateUnreachableError,
)
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS, delegate
from josu.delegate.queue import DelegateQueue
from josu.graph.engine import GraphEngine

_DELEGATE_DESCRIPTION = (
    "Delegate a bounded, well-defined sub-task to a local model running on this "
    "machine -- file/directory summarization, boilerplate generation against an "
    "established pattern, or simple search/extraction. Do not use for complex "
    "refactors, architecture decisions, ambiguous requirements, or security-"
    "sensitive changes -- those stay with you. Returns {result, caveats}; treat "
    "a non-empty caveats field as a signal to spot-check the result yourself. "
    "If a call returns a chain-exhausted error, perform the sub-task yourself "
    "rather than retrying the same delegation or leaving it incomplete."
)


def build_server(
    graph_engine: GraphEngine | None = None,
    model: str = "qwen2.5-coder:7b",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    name: str = "delegate-to-local",
    client: DelegateClient | None = None,
) -> Server:
    """Construct the low-level MCP server bound to the given graph engine."""
    server: Server = Server(name)
    queue = DelegateQueue()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="delegate_to_local",
                description=_DELEGATE_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The bounded task to perform",
                        },
                        "scope": {
                            "type": "object",
                            "description": "Optional scoping info, e.g. {'path': '...'}",
                        },
                    },
                    "required": ["task"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        if name != "delegate_to_local":
            return [
                types.TextContent(type="text", text=f"unknown tool: {name}", annotations=None)
            ]

        task = arguments.get("task", "")
        scope = arguments.get("scope")
        scope_root = (
            Path(scope["path"]) if isinstance(scope, dict) and "path" in scope else None
        )

        async def _run() -> dict:
            outcome = await delegate(
                task,
                scope,
                model=model,
                graph_engine=graph_engine,
                scope_root=scope_root,
                client=client,
                timeout=timeout,
            )
            return {
                "result": outcome.result,
                "caveats": outcome.caveats,
                "model": outcome.model,
            }

        try:
            payload = await queue.run(_run, timeout=timeout)
        except DelegateUnreachableError as exc:
            return [
                types.TextContent(
                    type="text", text=f"error: delegate unreachable: {exc}", annotations=None
                )
            ]
        except TimeoutError:
            return [
                types.TextContent(
                    type="text",
                    text=f"error: delegate call timed out after {timeout}s",
                    annotations=None,
                )
            ]
        except (DelegateRateLimitedError, DelegateAPIError, DelegateMalformedResponseError) as exc:
            # Chain-advance-vs-retry decisioning for these (R34/R24) is U12's
            # job (chain.py); this unit's single-candidate tool surfaces
            # whichever of the four the one configured candidate raised.
            return [types.TextContent(type="text", text=f"error: {exc}", annotations=None)]

        return [
            types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False), annotations=None
            )
        ]

    return server
