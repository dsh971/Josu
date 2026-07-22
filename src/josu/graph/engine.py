"""Swappable graph-engine interface (R10).

`build.py`'s graphify wrapper is the v1 implementation of this interface.
Nothing outside this module should import graphify directly -- `server.py`
and any future consumer only see the shape defined here, so a future engine
swap only touches this file and its implementation, never the MCP surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class GraphEngine(Protocol):
    """Minimal surface a context-graph implementation must provide."""

    def build(self, root: Path) -> None:
        """(Re)build the graph for the given directory tree, in place."""
        ...

    def update(self, root: Path, changed_files: list[Path]) -> None:
        """Incrementally re-index only the given changed files (R14)."""
        ...

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Fuzzy/substring search over node labels; returns matching nodes
        plus their immediate neighbors for context."""
        ...

    def execute(self, operation: str, params: dict) -> dict:
        """Dispatch a specific graph operation by name. Valid operations
        are engine-defined; unknown operations raise ValueError."""
        ...
