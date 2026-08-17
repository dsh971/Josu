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

import math
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from josu.config.chains import ChainsConfig, load_chains_config
from josu.config.delegate import DelegateConfig, load_delegate_config
from josu.config.graph_engines import GraphEnginesConfig, load_graph_engines_config
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

# delegate/cooldown.py's per-candidate circuit breaker (feat/delegate-
# candidate-circuit-breaker plan): three consecutive qualifying failures is
# enough to react to a genuinely dead candidate without tripping on one
# transient blip. 60 seconds matches `local_model.py`'s own
# `DEFAULT_TIMEOUT_SECONDS` (one delegate call's own budget) -- long enough
# to meaningfully skip several subsequent tasks while a candidate is down,
# short enough that a transient outage self-heals without a daemon restart.
# Override via `[delegate] failure_threshold`/`cooldown_seconds` in
# `josu.toml`.
DEFAULT_CANDIDATE_FAILURE_THRESHOLD: int = 3
DEFAULT_CANDIDATE_COOLDOWN_SECONDS: float = 60.0

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
    graph_engines: GraphEnginesConfig = field(default_factory=GraphEnginesConfig)
    wall_clock_timeout_seconds: float = DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS
    candidate_failure_threshold: int = DEFAULT_CANDIDATE_FAILURE_THRESHOLD
    candidate_cooldown_seconds: float = DEFAULT_CANDIDATE_COOLDOWN_SECONDS
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

    # `<= 0` alone doesn't reject `nan`/`inf` (code-review fix, found via
    # the identical gap in `_load_delegate_cooldown_config()` below): NaN
    # comparisons are always False and `inf > 0` is True, so a `wall_clock_
    # timeout_seconds = inf` TOML value would silently pass through and
    # become `CircuitBreaker.timeout_seconds` -- whose own `elapsed >
    # timeout_seconds` trip condition is also always False for inf/nan,
    # permanently disabling R23's whole-run safety net with no warning.
    if not math.isfinite(value) or value <= 0:
        return (
            DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS,
            [
                f"orchestrator.wall_clock_timeout_seconds {value!r} must be a positive, "
                f"finite number -- falling back to the default "
                f"({DEFAULT_WALL_CLOCK_TIMEOUT_SECONDS}s)"
            ],
        )

    return value, []


def _load_delegate_cooldown_config(data: dict) -> tuple[int, float, list[str]]:
    """Read `[delegate].failure_threshold`/`[delegate].cooldown_seconds`
    (feat/delegate-candidate-circuit-breaker plan) directly from the raw
    parsed TOML, mirroring `_load_wall_clock_timeout_seconds()`'s exact
    convention -- `[delegate]` already hosts `[[delegate.candidates]]` as an
    array-of-tables, and TOML allows a table's own scalar keys to cohabit
    with an array-of-tables it also defines, the same way `[orchestrator]`
    hosts both `wall_clock_timeout_seconds` and `[[orchestrator.adapters]]`.
    A missing key falls back to its default silently; a present-but-invalid
    value (non-numeric, `<= 0`) falls back to its default with a warning,
    never crashing config load. The two values are read together since both
    live in `[delegate]` and are validated the same way.
    """
    section = data.get("delegate", {})
    if not isinstance(section, dict):
        section = {}

    warnings: list[str] = []

    if "failure_threshold" not in section:
        failure_threshold = DEFAULT_CANDIDATE_FAILURE_THRESHOLD
    else:
        raw_threshold = section["failure_threshold"]
        try:
            # TOML permits an unquoted `inf` float literal for a key a
            # developer intended as an integer -- `int(raw_threshold)`
            # raises `OverflowError` for that, not `ValueError` (code-
            # review fix), which would otherwise propagate uncaught and
            # crash `load_config()`/daemon startup entirely rather than
            # degrading this one value like every other invalid input here.
            failure_threshold = int(raw_threshold)
        except (TypeError, ValueError, OverflowError):
            failure_threshold = DEFAULT_CANDIDATE_FAILURE_THRESHOLD
            warnings.append(
                f"delegate.failure_threshold {raw_threshold!r} is not a number -- "
                f"falling back to the default ({DEFAULT_CANDIDATE_FAILURE_THRESHOLD})"
            )
        else:
            if failure_threshold <= 0:
                warnings.append(
                    f"delegate.failure_threshold {failure_threshold!r} must be positive -- "
                    f"falling back to the default ({DEFAULT_CANDIDATE_FAILURE_THRESHOLD})"
                )
                failure_threshold = DEFAULT_CANDIDATE_FAILURE_THRESHOLD

    if "cooldown_seconds" not in section:
        cooldown_seconds = DEFAULT_CANDIDATE_COOLDOWN_SECONDS
    else:
        raw_cooldown = section["cooldown_seconds"]
        try:
            cooldown_seconds = float(raw_cooldown)
        except (TypeError, ValueError):
            cooldown_seconds = DEFAULT_CANDIDATE_COOLDOWN_SECONDS
            warnings.append(
                f"delegate.cooldown_seconds {raw_cooldown!r} is not a number -- "
                f"falling back to the default ({DEFAULT_CANDIDATE_COOLDOWN_SECONDS}s)"
            )
        else:
            # `<= 0` alone doesn't reject `nan`/`inf` (code-review fix):
            # NaN comparisons are always False, and `inf > 0` is True, so
            # TOML's unquoted `nan`/`inf` float literals would otherwise
            # slide through as "valid" -- a `nan` cooldown makes
            # `is_in_cooldown()`'s clock comparison always False (the
            # breaker silently never trips), and `inf` makes it always
            # True once tripped (permanently excluded until a daemon
            # restart, with no warning this is what a typo just did).
            if not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0:
                warnings.append(
                    f"delegate.cooldown_seconds {cooldown_seconds!r} must be a positive, "
                    f"finite number -- falling back to the default "
                    f"({DEFAULT_CANDIDATE_COOLDOWN_SECONDS}s)"
                )
                cooldown_seconds = DEFAULT_CANDIDATE_COOLDOWN_SECONDS

    return failure_threshold, cooldown_seconds, warnings


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

    graph_engines_config, graph_engines_warnings = load_graph_engines_config(data)
    warnings.extend(graph_engines_warnings)

    wall_clock_timeout_seconds, wall_clock_warnings = _load_wall_clock_timeout_seconds(data)
    warnings.extend(wall_clock_warnings)

    candidate_failure_threshold, candidate_cooldown_seconds, cooldown_warnings = (
        _load_delegate_cooldown_config(data)
    )
    warnings.extend(cooldown_warnings)

    return JosuConfig(
        path=resolved,
        delegate=delegate_config,
        chains=chains_config,
        orchestrator=orchestrator_config,
        graph_engines=graph_engines_config,
        wall_clock_timeout_seconds=wall_clock_timeout_seconds,
        candidate_failure_threshold=candidate_failure_threshold,
        candidate_cooldown_seconds=candidate_cooldown_seconds,
        warnings=warnings,
    )
