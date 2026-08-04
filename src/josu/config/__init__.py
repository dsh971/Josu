"""Resolves and loads `josu.toml`, composing per-schema config modules.

The config file lives outside any git-tracked project tree by default, at
an XDG-style path (`~/.config/josu/josu.toml`, respecting `$XDG_CONFIG_HOME`
when set) -- never the project root, where U4's `git add *`/`git commit *`
worktree allowlist could sweep it into an unattended commit. Load stats the
file and warns (or refuses, in `strict` mode) if group/world-readable bits
are set, mirroring `ssh`'s own private-key permission convention.

U11 composed only the delegate-candidate registry (`config/delegate.py`).
U12 wires in `config/chains.py` (U3) alongside it, so a single `load_config()`
call gives `daemon.py` everything it needs to construct a chain-aware
delegate server -- the candidate roster and the fallback chains that
reference it -- from one `josu.toml` read. U4 wires in
`config/orchestrator.py`'s `[[orchestrator.adapters]]` entries alongside
those, so the same `load_config()` call also gives the orchestrator engine
its adapter roster -- parsed, but not yet filtered by the `mcp_approval_verified`
attestation gate, which is a separate, deliberately-not-eager step (see that
module's docstring) applied by callers via `usable_adapters()`.

U5 adds `wall_clock_timeout_seconds` (R23) -- the whole-run budget
`orchestrator/circuit_breaker.py`'s `CircuitBreaker` enforces, distinct from
any single delegate call's own timeout. It's read directly from the raw
TOML `[orchestrator]` table here, deliberately NOT added to
`config/orchestrator.py`'s `OrchestratorConfig` pydantic model (that
module's `[[orchestrator.adapters]]` array-of-tables schema is a separate
unit's concern) -- TOML allows a table's own scalar keys to cohabit with an
array-of-tables it also defines, so `[orchestrator]\nwall_clock_timeout_seconds
= 1200` alongside `[[orchestrator.adapters]]` entries is valid and the two
readers simply look at different keys of the same section.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from josu.config.chains import ChainsConfig, load_chains_config
from josu.config.delegate import DelegateConfig, load_delegate_config
from josu.config.orchestrator import OrchestratorConfig, load_orchestrator_config

DEFAULT_CONFIG_DIRNAME = "josu"
DEFAULT_CONFIG_FILENAME = "josu.toml"

# R23's configurable default: the whole hosted-orchestrator run (not any
# single delegate call) is bounded by this many seconds of ACTIVE wall-clock
# time before `orchestrator/circuit_breaker.py`'s `CircuitBreaker` trips.
# 20 minutes is a generous default for one Claude Code turn against a
# bounded task; override via `[orchestrator] wall_clock_timeout_seconds` in
# `josu.toml`.
DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS: float = 20 * 60

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
    chains: ChainsConfig = field(default_factory=ChainsConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    wall_clock_timeout_seconds: float = DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS
    warnings: list[str] = field(default_factory=list)


def _load_wall_clock_timeout_seconds(data: dict) -> tuple[float, list[str]]:
    """Read `[orchestrator].wall_clock_timeout_seconds` (R23) directly from
    the raw parsed TOML -- see module docstring for why this bypasses
    `config/orchestrator.py`'s pydantic schema entirely. Missing key falls
    back to `DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS`, silently (that's the
    normal, expected case, not a warning-worthy one). A present-but-invalid
    value (non-numeric, zero, or negative) degrades to the default with a
    warning, mirroring `config/delegate.py`/`config/chains.py`'s "a bad
    entry degrades that entry, not the whole load" convention rather than
    crashing config loading entirely."""
    section = data.get("orchestrator", {})
    if not isinstance(section, dict) or "wall_clock_timeout_seconds" not in section:
        return DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS, []

    raw_value = section["wall_clock_timeout_seconds"]
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return (
            DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS,
            [
                f"orchestrator.wall_clock_timeout_seconds {raw_value!r} is not a number -- "
                f"falling back to the default ({DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS}s)"
            ],
        )

    if value <= 0:
        return (
            DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS,
            [
                f"orchestrator.wall_clock_timeout_seconds {value!r} must be positive -- "
                f"falling back to the default ({DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS}s)"
            ],
        )

    return value, []


def load_config(path: Path | None = None, *, strict: bool = False) -> JosuConfig:
    """Resolve (or use the given) `josu.toml` path, check its permissions,
    parse it, and validate its `[delegate]` and `[delegation]` sections.

    Permission and env-var-existence problems become warnings on `config`
    (never raised) unless `strict=True`, in which case a permission problem
    refuses to load at all.

    If the resolved path doesn't exist -- the expected state on a first run,
    before anyone has ever created `josu.toml` -- this returns a usable
    default `JosuConfig` (no delegate candidates, chains, or orchestrator
    adapters configured) with a warning recorded, rather than letting
    `Path.open()`'s `FileNotFoundError` propagate and crash every CLI/daemon
    entry point that calls this before any config file has ever been
    created. Permission checks and TOML parsing are both skipped in this
    case -- there's nothing on disk to stat or parse.
    """
    resolved = path if path is not None else resolve_config_path()

    if not resolved.exists():
        return JosuConfig(
            path=resolved,
            delegate=DelegateConfig(),
            warnings=[
                f"{resolved} does not exist -- using a default config with no delegate "
                "candidates, chains, or orchestrator adapters configured; create this "
                "file to configure josu"
            ],
        )

    warnings = check_permissions(resolved, strict=strict)

    with resolved.open("rb") as f:
        data = tomllib.load(f)

    delegate_config, delegate_warnings = load_delegate_config(data)
    warnings.extend(delegate_warnings)

    chains_config, chains_warnings = load_chains_config(data)
    warnings.extend(chains_warnings)

    orchestrator_config, orchestrator_warnings = load_orchestrator_config(data)
    warnings.extend(orchestrator_warnings)

    wall_clock_timeout_seconds, wall_clock_warnings = _load_wall_clock_timeout_seconds(data)
    warnings.extend(wall_clock_warnings)

    return JosuConfig(
        path=resolved,
        delegate=delegate_config,
        chains=chains_config,
        orchestrator=orchestrator_config,
        wall_clock_timeout_seconds=wall_clock_timeout_seconds,
        warnings=warnings,
    )
