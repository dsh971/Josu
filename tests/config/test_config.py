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
