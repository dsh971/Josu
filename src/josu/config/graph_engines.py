"""Declarative graph-engine connection-target config.

Mirrors `config/orchestrator.py`'s `[[orchestrator.adapters]]` pattern: a
`[[graph.engines]]` TOML entry names a connection target (host/port), never
an install/spawn instruction. josu's daemon connects to the configured
target as a pure client -- it never installs, spawns, or authenticates the
target process itself (that's the user's own setup, same as the hosted CLI
agent is already a user-installed prerequisite).

`api_key_env` mirrors `config/delegate.py`'s field exactly: an env var
*name*, never a raw credential. Existence is checked eagerly here (a
warning if the referenced variable isn't set); the *value* is resolved
lazily, at connect time, by a caller outside this module -- never read or
logged here.

v1 is a flat list, not a priority-ordered fallback chain (unlike
`[[delegation.chains]]`, which does support ordered fallback). Only the
first successfully-validated entry becomes the active target; additional
entries are dropped with a warning naming them, not silently ignored. Real
multi-engine selection is out of scope for now.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ValidationError

# Hosts considered "local" for the cleartext-credential warning below. A
# connection to any of these never leaves the machine, so an api_key_env
# credential riding along with it isn't exposed to the network the way a
# non-loopback target's would be.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class GraphEngineTargetConfig(BaseModel):
    """One `[[graph.engines]]` TOML entry."""

    name: str
    host: str
    port: int
    api_key_env: str | None = None


class GraphEnginesConfig(BaseModel):
    """The `[graph]` TOML section: every declared engine-target entry that
    parsed successfully. See module docstring -- only the first entry is
    ever used as the active target."""

    engines: list[GraphEngineTargetConfig] = []


def load_graph_engines_config(data: dict[str, Any]) -> tuple[GraphEnginesConfig, list[str]]:
    """Validate the `[[graph.engines]]` entries of an already-parsed TOML
    document.

    Each entry is validated independently -- a single malformed entry
    (missing/wrong-typed field) is excluded and recorded as a warning,
    mirroring `config/orchestrator.py`'s/`config/delegate.py`'s "a bad
    entry degrades that entry, not the whole load" convention.

    Returns `(config, warnings)` -- `warnings` never crashes config loading.
    """
    section = data.get("graph", {})
    raw_engines = section.get("engines", []) if isinstance(section, dict) else []

    warnings: list[str] = []
    parsed: list[GraphEngineTargetConfig] = []
    for entry in raw_engines:
        entry_name = entry.get("name", "<unnamed>") if isinstance(entry, dict) else "<unnamed>"
        try:
            engine = GraphEngineTargetConfig.model_validate(entry)
        except ValidationError as exc:
            warnings.append(f"graph engine {entry_name!r} rejected at load time: {exc}")
            continue
        parsed.append(engine)

    if len(parsed) > 1:
        ignored_names = [engine.name for engine in parsed[1:]]
        warnings.append(
            f"multiple [[graph.engines]] entries configured; only {parsed[0].name!r} is "
            f"used as the active target -- ignoring {ignored_names!r} (v1 does not support "
            "a priority-ordered fallback chain across multiple engines)"
        )

    config = GraphEnginesConfig(engines=parsed)

    if config.engines:
        active = config.engines[0]
        if active.api_key_env is not None and active.api_key_env not in os.environ:
            warnings.append(
                f"graph engine {active.name!r}: environment variable "
                f"{active.api_key_env!r} (referenced by api_key_env) is not set"
            )
        if active.host not in _LOOPBACK_HOSTS and active.api_key_env is not None:
            warnings.append(
                f"graph engine {active.name!r} targets non-loopback host {active.host!r} "
                "with api_key_env set -- the connection is unauthenticated HTTP (no TLS "
                "support), so the configured credential and all query content would "
                "travel in cleartext over the network"
            )

    return config, warnings
