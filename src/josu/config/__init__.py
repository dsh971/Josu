"""Resolves and loads `josu.toml`, composing per-schema config modules.

The config file lives outside any git-tracked project tree by default, at
an XDG-style path (`~/.config/josu/josu.toml`, respecting `$XDG_CONFIG_HOME`
when set) -- never the project root, where U4's `git add *`/`git commit *`
worktree allowlist could sweep it into an unattended commit. Load stats the
file and warns (or refuses, in `strict` mode) if group/world-readable bits
are set, mirroring `ssh`'s own private-key permission convention.

This unit (U11) only composes the delegate-candidate registry
(`config/delegate.py`); `config/chains.py` (U3) and `config/orchestrator.py`
(U4) don't exist yet and are wired in by their own units.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from josu.config.delegate import DelegateConfig, load_delegate_config

DEFAULT_CONFIG_DIRNAME = "josu"
DEFAULT_CONFIG_FILENAME = "josu.toml"

# Bits that mean "readable/writable/executable by group or other" -- the
# same class of bit `ssh` warns/refuses on for private key files.
_GROUP_OR_WORLD_ACCESSIBLE = stat.S_IRWXG | stat.S_IRWXO


class ConfigPermissionError(RuntimeError):
    """Raised in `strict` mode when `josu.toml` is group/world-readable."""


def resolve_config_path() -> Path:
    """Resolve the XDG-style `josu.toml` path.

    Respects `$XDG_CONFIG_HOME` when set (the XDG base-directory spec's own
    override mechanism); falls back to `~/.config` otherwise. Deliberately
    never resolves anywhere under the current working directory / a
    git-tracked project tree -- see module docstring.
    """
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / DEFAULT_CONFIG_DIRNAME / DEFAULT_CONFIG_FILENAME


def check_permissions(path: Path, *, strict: bool = False) -> list[str]:
    """Stat `path` and flag group/world-readable permission bits.

    Returns a list of warning strings (empty if the file is only
    user-accessible). In `strict` mode, raises `ConfigPermissionError`
    instead of returning a warning.
    """
    mode = path.stat().st_mode
    if not (mode & _GROUP_OR_WORLD_ACCESSIBLE):
        return []

    message = (
        f"{path} is group/world-accessible (mode {stat.S_IMODE(mode):o}) -- "
        "it may contain credential env-var references; restrict it to your "
        "user only (e.g. chmod 0600), the same convention ssh uses for "
        "private key files"
    )
    if strict:
        raise ConfigPermissionError(message)
    return [message]


@dataclass(frozen=True)
class JosuConfig:
    """The composed, validated contents of `josu.toml`."""

    path: Path
    delegate: DelegateConfig
    warnings: list[str] = field(default_factory=list)


def load_config(path: Path | None = None, *, strict: bool = False) -> JosuConfig:
    """Resolve (or use the given) `josu.toml` path, check its permissions,
    parse it, and validate its `[delegate]` section.

    Permission and env-var-existence problems become warnings on `config`
    (never raised) unless `strict=True`, in which case a permission problem
    refuses to load at all.
    """
    resolved = path if path is not None else resolve_config_path()
    warnings = check_permissions(resolved, strict=strict)

    with resolved.open("rb") as f:
        data = tomllib.load(f)

    delegate_config, delegate_warnings = load_delegate_config(data)
    warnings.extend(delegate_warnings)

    return JosuConfig(path=resolved, delegate=delegate_config, warnings=warnings)
