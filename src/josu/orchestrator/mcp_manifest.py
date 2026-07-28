"""Explicit, per-worktree MCP config manifest generation (U4).

Claude Code (and any future adapter) is pointed at an explicit manifest file
via `--strict-mcp-config <path>`, never at ambient `.mcp.json` discovery --
an ambient manifest could be edited by anything on the machine between
worktree creation and invocation, or simply not be the manifest this pipeline
built. Writing one file per worktree, naming the two MCP servers this
pipeline's own daemon (`daemon.py`) serves, keeps the invoked CLI's tool
surface exactly as narrow as `daemon.py`'s own `/graph/sse` and
`/delegate/sse` routes -- see U1/U2's server names (`context-graph`,
`delegate-to-local`), which this module hard-codes to match.

`validate_manifest()` re-reads the manifest from disk and re-checks its
contents against the known-good daemon address immediately before invocation
-- closing the gap between "the manifest was written with the right content"
and "the manifest still has that content by the time the CLI reads it" (it
could have been truncated, replaced, or edited in between).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Must match `daemon.py`'s `build_server(name=...)` calls exactly --
# `graph/server.py`'s "context-graph" and `delegate/server.py`'s
# "delegate-to-local" -- since these are also the literal prefixes Claude
# Code's `--allowedTools` uses (`mcp__context-graph__search`, etc; see
# `adapters/claude_code.py`).
GRAPH_SERVER_NAME = "context-graph"
DELEGATE_SERVER_NAME = "delegate-to-local"
_EXPECTED_SERVER_NAMES = (GRAPH_SERVER_NAME, DELEGATE_SERVER_NAME)


class MCPManifestError(RuntimeError):
    """Raised when a manifest can't be written, or fails validation against
    the known-good daemon address immediately before invocation."""


@dataclass(frozen=True)
class MCPManifest:
    """A written, per-worktree MCP manifest and the daemon address it was
    generated against (kept alongside the path so `validate_manifest()` can
    re-check the on-disk content against it without a second parameter)."""

    path: Path
    host: str
    port: int


def build_manifest_data(host: str, port: int) -> dict[str, Any]:
    """The manifest content itself -- an SSE-type entry per MCP server,
    pointing at `daemon.py`'s `/graph/sse` and `/delegate/sse` routes on the
    given host/port. A pure function so `validate_manifest()` can recompute
    the expected content and compare, rather than trusting whatever was
    written earlier."""
    base = f"http://{host}:{port}"
    return {
        "mcpServers": {
            GRAPH_SERVER_NAME: {"type": "sse", "url": f"{base}/graph/sse"},
            DELEGATE_SERVER_NAME: {"type": "sse", "url": f"{base}/delegate/sse"},
        }
    }


def write_manifest(
    worktree_path: Path,
    host: str,
    port: int,
    *,
    manifests_dir: Path | None = None,
) -> MCPManifest:
    """Write an explicit MCP manifest for `worktree_path`, pointing at the
    running daemon at `host:port`.

    Written to `manifests_dir` (defaulting to `worktree_path`'s parent) as a
    sibling of the worktree directory, not inside it -- so it can never be
    swept up by the invoked CLI's own `git add *`/`git commit *` allowlist
    (`adapters/claude_code.py`), which is scoped to the worktree's tracked
    content.
    """
    directory = manifests_dir if manifests_dir is not None else worktree_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / f"{worktree_path.name}.mcp-config.json"

    data = build_manifest_data(host, port)
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return MCPManifest(path=manifest_path, host=host, port=port)


def validate_manifest(manifest: MCPManifest) -> None:
    """Re-read `manifest.path` from disk and confirm its contents still
    match the known-good daemon address (`manifest.host`/`manifest.port`) --
    call immediately before invocation, not just once at write time, to
    close the generation-to-invocation gap.

    Raises `MCPManifestError` if the file is missing/unreadable/malformed,
    if either expected MCP server entry is missing, or if either entry's
    content has drifted from what `build_manifest_data()` would produce for
    the same host/port.
    """
    try:
        raw = manifest.path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MCPManifestError(f"MCP manifest {manifest.path} is unreadable: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MCPManifestError(f"MCP manifest {manifest.path} is not valid JSON: {exc}") from exc

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        raise MCPManifestError(f"MCP manifest {manifest.path} has no `mcpServers` table")

    expected = build_manifest_data(manifest.host, manifest.port)["mcpServers"]
    for server_name in _EXPECTED_SERVER_NAMES:
        if server_name not in servers:
            raise MCPManifestError(
                f"MCP manifest {manifest.path} is missing required server {server_name!r}"
            )
        if servers[server_name] != expected[server_name]:
            raise MCPManifestError(
                f"MCP manifest {manifest.path} server {server_name!r} does not match the "
                f"known-good daemon address {manifest.host}:{manifest.port} "
                f"(found {servers[server_name]!r}, expected {expected[server_name]!r})"
            )
