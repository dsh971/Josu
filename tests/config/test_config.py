"""Tests for `josu.toml` path resolution, permission checks, and loading
(U11) -- see plan Key Technical Decisions re: XDG-style path outside any
git-tracked tree, and permission handling mirroring ssh's private-key
convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from josu.config import (
    ConfigPermissionError,
    check_permissions,
    load_config,
    resolve_config_path,
)


def _write_toml(path, local=True, api_key_env=None):
    api_key_line = f'api_key_env = "{api_key_env}"\n' if api_key_env else ""
    path.write_text(
        "[[delegate.candidates]]\n"
        'name = "local-candidate"\n'
        'endpoint = "http://localhost:11434/v1"\n'
        f"local = {'true' if local else 'false'}\n"
        f"{api_key_line}"
        'model = "qwen2.5-coder:7b"\n',
        encoding="utf-8",
    )


def test_resolve_config_path_respects_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    resolved = resolve_config_path()
    assert resolved == tmp_path / "xdg" / "josu" / "josu.toml"


def test_resolve_config_path_falls_back_to_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    resolved = resolve_config_path()
    assert resolved == Path.home() / ".config" / "josu" / "josu.toml"


def test_user_only_permissions_produce_no_warning(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o600)
    assert check_permissions(config_path) == []


def test_group_world_readable_permissions_produce_warning(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o644)
    warnings = check_permissions(config_path)
    assert len(warnings) == 1
    assert str(config_path) in warnings[0]


def test_strict_mode_refuses_to_load_group_world_readable_file(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o644)
    with pytest.raises(ConfigPermissionError):
        check_permissions(config_path, strict=True)


def test_load_config_returns_candidates_and_warnings(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o600)

    config = load_config(config_path)
    assert config.path == config_path
    assert len(config.delegate.candidates) == 1
    assert config.delegate.candidates[0].name == "local-candidate"
    assert config.warnings == []


def test_load_config_surfaces_permission_warning_by_default(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o644)

    config = load_config(config_path)
    assert any("group/world-accessible" in w for w in config.warnings)


def test_load_config_strict_mode_raises_instead_of_warning(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o644)

    with pytest.raises(ConfigPermissionError):
        load_config(config_path, strict=True)


def test_load_config_composes_chains_alongside_delegate_candidates(tmp_path):
    """U12: `load_config()` also validates the `[delegation]` section
    (config/chains.py, U3) from the same `josu.toml` read, so `daemon.py`
    gets both the candidate registry and the fallback chains that
    reference it from one call."""
    config_path = tmp_path / "josu.toml"
    config_path.write_text(
        "[[delegate.candidates]]\n"
        'name = "local-candidate"\n'
        'endpoint = "http://localhost:11434/v1"\n'
        "local = true\n"
        'model = "qwen2.5-coder:7b"\n'
        "\n"
        "[[delegation.chains]]\n"
        'task_type = "file_summarization"\n'
        'candidates = ["local-candidate"]\n',
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)

    config = load_config(config_path)
    assert len(config.chains.chains) == 1
    assert config.chains.chains[0].task_type == "file_summarization"


def test_load_config_returns_default_config_with_warning_when_path_does_not_exist(tmp_path):
    """Covers the P1 fix: `load_config()` against a `josu.toml` path that
    doesn't exist yet (the expected state on a first run, before anyone has
    created the file) must return a usable default `JosuConfig` with a
    warning recorded -- not raise `FileNotFoundError` and crash every
    CLI/daemon entry point that calls this before a config file exists."""
    nonexistent = tmp_path / "does" / "not" / "exist" / "josu.toml"

    config = load_config(nonexistent)

    assert config.path == nonexistent
    assert config.delegate.candidates == []
    assert config.chains.chains == []
    assert config.orchestrator.adapters == []
    assert len(config.warnings) == 1
    assert str(nonexistent) in config.warnings[0]
    assert "does not exist" in config.warnings[0]


def test_load_config_defaults_chains_empty_when_no_delegation_section(tmp_path):
    config_path = tmp_path / "josu.toml"
    _write_toml(config_path)
    os.chmod(config_path, 0o600)

    config = load_config(config_path)
    assert config.chains.chains == []
