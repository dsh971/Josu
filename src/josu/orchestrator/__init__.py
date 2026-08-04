"""Isolated worktree lifecycle, MCP manifest generation, and the config-driven
hosted-orchestrator adapter engine (U4).

See `worktree.py`, `mcp_manifest.py`, `adapter.py`, and `adapters/claude_code.py`
for the individual pieces; nothing is re-exported at package level so each
module's imports stay explicit about which piece they depend on.
"""

from __future__ import annotations
