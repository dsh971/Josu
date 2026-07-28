"""MCP server exposing the local model as a single `delegate_to_local` tool.

One tool, mirroring the graph server's fixed-surface pattern (U1): the local
delegate doesn't need multiple entry points, just a bounded task, a
`task_type` naming which fallback chain to resolve (U3/U12), and optional
scope. The full delegation policy (when to prefer this over staying hosted)
lives in U3's guide; this tool description carries only the mechanical
usage hint.

`call_tool` (U12) is deliberately thin: resolve arguments -> call
`chain.execute_chain(...)` -> map its outcome to MCP content blocks. It owns
no retry/chain-advance decision itself -- that all lives in `chain.py`,
which is also U7's and U9's non-MCP entry point into the same logic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import mcp.types as types
from mcp.server.lowlevel import Server

from josu.config.chains import ChainsConfig
from josu.config.delegate import DelegateCandidate
from josu.delegate import chain
from josu.delegate.client import DelegateMalformedResponseError
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS
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
    chains_config: ChainsConfig | None = None,
    registry: Mapping[str, DelegateCandidate] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    name: str = "delegate-to-local",
    client_factory: chain.ClientFactory | None = None,
) -> Server:
    """Construct the low-level MCP server bound to the given graph engine and
    delegate-chain config.

    `chains_config`/`registry` are U3's fallback-chain schema and U11's
    candidate registry, resolved once (typically by `daemon.py` from
    `josu.toml`) and passed in here rather than a single hardcoded model --
    empty defaults keep this constructible standalone (e.g. for
    `list_tools`-only tests) without requiring a full config.
    """
    server: Server = Server(name)
    queue = DelegateQueue()
    resolved_chains_config = chains_config if chains_config is not None else ChainsConfig()
    resolved_registry: Mapping[str, DelegateCandidate] = registry if registry is not None else {}

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
                        "task_type": {
                            "type": "string",
                            "description": (
                                "The delegation-guide task-type category (e.g. "
                                "'file_summarization') naming which fallback chain "
                                "to resolve for this task."
                            ),
                        },
                        "scope": {
                            "type": "object",
                            "description": "Optional scoping info, e.g. {'path': '...'}",
                        },
                    },
                    "required": ["task", "task_type"],
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
        task_type = arguments.get("task_type", "")
        scope = arguments.get("scope")
        scope_root = (
            Path(scope["path"]) if isinstance(scope, dict) and "path" in scope else None
        )

        try:
            outcome = await chain.execute_chain(
                task_type,
                task,
                scope,
                chains_config=resolved_chains_config,
                registry=resolved_registry,
                queue=queue,
                graph_engine=graph_engine,
                scope_root=scope_root,
                client_factory=client_factory,
                timeout=timeout,
            )
        except chain.ChainExhaustedError as exc:
            return [
                types.TextContent(
                    type="text", text=f"error: chain exhausted: {exc}", annotations=None
                )
            ]
        except chain.NoCandidatesError as exc:
            return [types.TextContent(type="text", text=f"error: {exc}", annotations=None)]
        except DelegateMalformedResponseError as exc:
            # R24's same-candidate retry (inside delegate()) already ran and
            # failed -- distinct from chain exhaustion, deliberately not
            # unified with it (see chain.py).
            return [types.TextContent(type="text", text=f"error: {exc}", annotations=None)]

        payload = {
            "result": outcome.result,
            "caveats": outcome.caveats,
            "model": outcome.model,
        }
        return [
            types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False), annotations=None
            )
        ]

    return server
