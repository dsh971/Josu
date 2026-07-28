"""Tests for per-worktree MCP manifest generation and pre-invocation
validation (U4)."""

from __future__ import annotations

import json

import pytest

from josu.orchestrator.mcp_manifest import (
    DELEGATE_SERVER_NAME,
    GRAPH_SERVER_NAME,
    MCPManifest,
    MCPManifestError,
    build_manifest_data,
    validate_manifest,
    write_manifest,
)


def test_build_manifest_data_points_at_both_daemon_mcp_servers():
    data = build_manifest_data("127.0.0.1", 8765)
    servers = data["mcpServers"]
    assert servers[GRAPH_SERVER_NAME] == {"type": "sse", "url": "http://127.0.0.1:8765/graph/sse"}
    assert servers[DELEGATE_SERVER_NAME] == {
        "type": "sse",
        "url": "http://127.0.0.1:8765/delegate/sse",
    }


def test_write_manifest_writes_alongside_not_inside_the_worktree(tmp_path):
    worktree_path = tmp_path / "worktrees" / "task-1"
    worktree_path.mkdir(parents=True)

    manifest = write_manifest(worktree_path, "127.0.0.1", 8765)

    assert manifest.path.exists()
    assert manifest.path.parent == worktree_path.parent
    # Never inside the worktree -- so it can never be swept up by
    # `git add *`/`git commit *` (adapters/claude_code.py's allowlist is
    # scoped to the worktree's own tracked content).
    assert worktree_path not in manifest.path.parents or manifest.path.parent != worktree_path


def test_freshly_written_manifest_passes_validation(tmp_path):
    worktree_path = tmp_path / "worktrees" / "task-1"
    worktree_path.mkdir(parents=True)

    manifest = write_manifest(worktree_path, "127.0.0.1", 8765)

    validate_manifest(manifest)  # must not raise


def test_validate_manifest_raises_if_file_missing(tmp_path):
    missing = MCPManifest(path=tmp_path / "does-not-exist.json", host="127.0.0.1", port=8765)
    with pytest.raises(MCPManifestError):
        validate_manifest(missing)


def test_validate_manifest_raises_if_content_drifted_from_known_good_address(tmp_path):
    """Closes the generation-to-invocation gap: even though the manifest was
    written correctly, if its on-disk content no longer matches the known-good
    daemon address by the time invocation is about to happen, validation
    catches it rather than trusting the write-time content."""
    worktree_path = tmp_path / "worktrees" / "task-1"
    worktree_path.mkdir(parents=True)
    manifest = write_manifest(worktree_path, "127.0.0.1", 8765)

    # Simulate drift: something replaced the manifest's content between
    # generation and invocation, pointing at a different port.
    tampered = build_manifest_data("127.0.0.1", 9999)
    manifest.path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(MCPManifestError):
        validate_manifest(manifest)


def test_validate_manifest_raises_if_a_required_server_is_missing(tmp_path):
    worktree_path = tmp_path / "worktrees" / "task-1"
    worktree_path.mkdir(parents=True)
    manifest = write_manifest(worktree_path, "127.0.0.1", 8765)

    data = build_manifest_data("127.0.0.1", 8765)
    del data["mcpServers"][DELEGATE_SERVER_NAME]
    manifest.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MCPManifestError):
        validate_manifest(manifest)


def test_validate_manifest_raises_on_malformed_json(tmp_path):
    worktree_path = tmp_path / "worktrees" / "task-1"
    worktree_path.mkdir(parents=True)
    manifest = write_manifest(worktree_path, "127.0.0.1", 8765)

    manifest.path.write_text("not json", encoding="utf-8")

    with pytest.raises(MCPManifestError):
        validate_manifest(manifest)
