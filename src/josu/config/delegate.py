"""Delegate-candidate registry schema (R30, R31).

Loaded from a `[[delegate.candidates]]` TOML section via stdlib `tomllib`
and validated with a plain `pydantic.BaseModel` -- not `pydantic-settings`
(its one-field-one-fixed-env-var model doesn't fit "a developer-defined list
of candidates, each carrying a *string naming* an env var"), not YAML. This
is the actual configurable delegate roster that satisfies R30 ("adding a new
delegate requires no code change"); `models/curated.py`'s hardcoded
`CURATED_MODELS` stays a small, name-keyed `min_ram_gb` lookup consulted by
-- not replaced by -- this registry.

Credential fields are always `api_key_env: str | None` (an env var *name*),
never `api_key: str` -- a schema-level enforcement of R31, since the schema
has no field capable of holding a raw secret at all. Env var *existence* is
validated eagerly here, at config-load time (a warning if a referenced
variable isn't set) -- but only existence, never the value, and never in the
warning message. *Resolution* stays lazy, at call time, which is deliberately
left to a caller outside this module (U12's chain-execution code) so a
missing credential degrades that one candidate to unreachable rather than
crashing config load for the whole system.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ValidationError


class DelegateCandidate(BaseModel):
    """One `[[delegate.candidates]]` TOML table."""

    name: str
    endpoint: str
    api_key_env: str | None = None
    local: bool
    model: str


class DelegateConfig(BaseModel):
    """The `[delegate]` TOML section: the full candidate roster."""

    candidates: list[DelegateCandidate] = []


def load_delegate_config(data: dict) -> tuple[DelegateConfig, list[str]]:
    """Validate the `[delegate]` section of an already-parsed TOML document.

    Each `[[delegate.candidates]]` entry is validated independently --
    unlike a plain `DelegateConfig.model_validate(section)` (which would
    fail the WHOLE section on one malformed entry), a single malformed
    candidate (missing/wrong-typed field) is excluded and recorded as a
    warning, mirroring `config/orchestrator.py`'s `load_orchestrator_
    config()` "a bad entry degrades that entry, not the whole load"
    convention -- which this module's own docstring already claims but
    previously didn't implement.

    Returns `(config, warnings)` -- `warnings` never crashes config loading
    (a malformed candidate, or a bad/missing env var for an otherwise-valid
    one, degrades only that candidate, not the whole load) and never
    includes an env var's *value*, only its *name*.
    """
    section = data.get("delegate", {})
    raw_candidates = section.get("candidates", []) if isinstance(section, dict) else []

    warnings: list[str] = []
    candidates: list[DelegateCandidate] = []
    for entry in raw_candidates:
        entry_name = entry.get("name", "<unnamed>") if isinstance(entry, dict) else "<unnamed>"
        try:
            candidate = DelegateCandidate.model_validate(entry)
        except ValidationError as exc:
            warnings.append(
                f"delegate candidate {entry_name!r} rejected at load time: {exc}"
            )
            continue
        candidates.append(candidate)

    config = DelegateConfig(candidates=candidates)

    for candidate in config.candidates:
        if candidate.api_key_env is not None and candidate.api_key_env not in os.environ:
            warnings.append(
                f"delegate candidate {candidate.name!r}: environment variable "
                f"{candidate.api_key_env!r} (referenced by api_key_env) is not set"
            )
    return config, warnings
