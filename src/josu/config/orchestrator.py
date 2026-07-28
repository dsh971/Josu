"""Config-driven hosted-orchestrator adapter contract (U4, R36-R38).

Mirrors U11/U3's TOML+pydantic pattern (`config/delegate.py`,
`config/chains.py`): loaded from `[[orchestrator.adapters]]` TOML entries via
stdlib `tomllib`, validated with a plain `pydantic.BaseModel`. This is the
schema half of the "config-driven adapter" claim in the plan -- an adapter
entry names an invocation command template (`command` + `args`, with
`{worktree}`/`{mcp_config}`/`{task}` placeholders substituted per-argument by
`orchestrator/adapter.py`, never as a shell string), a structured-output mode,
a declarative field-mapping for extracting a result/diff from that output,
and an `mcp_approval_verified` attestation record.

Two independent gates apply to every adapter entry, deliberately kept
separate because they guard against different things:

- `command` is validated against `ALLOWED_ADAPTER_COMMANDS` and
  `structured_output_mode` against `ALLOWED_OUTPUT_MODES` at *parse* time
  (pydantic field validators, below) -- an off-allowlist command or an
  undeclarable output mode is a malformed entry, rejected independent of
  whatever its `mcp_approval_verified` attestation claims (R38). One bad
  entry is excluded and warned about, not a whole-config crash -- mirroring
  `config/delegate.py`'s "a bad candidate degrades that candidate, not the
  whole load" convention.
- `mcp_approval_verified` is an attestation about a *different* thing (R37):
  whether whoever added this adapter independently confirmed it supports
  non-interactive MCP tool approval. `usable_adapters()` (below) applies
  this gate separately, after parsing succeeds -- `verified = false` excludes
  the adapter from the usable list entirely; a `verified_date` older than
  `STALENESS_WINDOW_DAYS` produces a warning but does not exclude it.

Both gates matter and neither substitutes for the other: a well-attested
adapter with an unsafe invocation template is still rejected, and a
perfectly-shaped adapter nobody has attested to non-interactive MCP approval
for is still excluded.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

# Deliberately tiny allowlist, extended only when a second CLI orchestrator
# actually clears the MCP-approval gate (see plan Scope Boundaries re: Codex
# CLI being blocked on openai/codex#24135). This check is independent of --
# and does not substitute for -- the `mcp_approval_verified` attestation
# below: an allowlisted command with no attestation is still unusable, and an
# attested-but-off-allowlist command is still rejected here.
ALLOWED_ADAPTER_COMMANDS = frozenset({"claude"})

# The structured-output modes this adapter engine knows how to declaratively
# (or, for `stream-json`, near-declaratively -- see adapters/claude_code.py)
# extract fields from. An adapter attested for MCP approval but with no
# declarable mode here is rejected at load time (R38) rather than accepted
# and left to fail unpredictably at invocation.
ALLOWED_OUTPUT_MODES = frozenset({"stream-json", "json"})

# R37: an attestation older than this warns (not blocks) -- "silently stale
# forever" is the gap this closes, per the plan's Risks & Dependencies.
STALENESS_WINDOW_DAYS = 180


class OrchestratorConfigError(ValueError):
    """Raised for adapter-entry-shape problems that aren't per-entry
    warnings -- reserved for callers that want a hard failure instead of the
    default "reject this entry, warn, keep loading" behavior."""


class McpApprovalVerified(BaseModel):
    """R37's attestation record -- deliberately just `{verified, verified_date}`.

    A larger shape (`cli_version`, `verified_by`, ...) was considered and
    explicitly trimmed in a later plan revision; this is the smaller, final
    shape. Do not re-add fields here without a corresponding plan change.
    """

    verified: bool
    verified_date: date


class OrchestratorAdapterConfig(BaseModel):
    """One `[[orchestrator.adapters]]` TOML entry.

    `args` is a template list, substituted per-argument (never as a shell
    string) by `orchestrator/adapter.py`'s `build_argv()` -- each element may
    contain `{worktree}`, `{mcp_config}`, and/or `{task}` placeholders.
    `field_mapping` is a flat `logical_name -> dot.separated.path` mapping
    consulted by `adapter.py`'s generic `extract_fields()`; an adapter whose
    structured output isn't already a single flat object (Claude Code's
    `stream-json` event stream) does its own flattening first -- see
    `adapters/claude_code.py` -- and this mapping runs against the result.
    """

    name: str
    command: str
    args: list[str] = []
    structured_output_mode: str
    field_mapping: dict[str, str] = {}
    mcp_approval_verified: McpApprovalVerified

    @field_validator("command")
    @classmethod
    def _command_must_be_allowlisted(cls, value: str) -> str:
        if value not in ALLOWED_ADAPTER_COMMANDS:
            raise ValueError(
                f"adapter command {value!r} is not on the allowlist "
                f"{sorted(ALLOWED_ADAPTER_COMMANDS)} -- this check is independent of "
                "mcp_approval_verified and applies even to an otherwise fully attested "
                "adapter entry"
            )
        return value

    @field_validator("structured_output_mode")
    @classmethod
    def _output_mode_must_be_declarable(cls, value: str) -> str:
        if value not in ALLOWED_OUTPUT_MODES:
            raise ValueError(
                f"adapter structured_output_mode {value!r} is not a declarable mode "
                f"(one of {sorted(ALLOWED_OUTPUT_MODES)}) -- rejected at load time (R38) "
                "rather than accepted and left to fail unpredictably at invocation"
            )
        return value


class OrchestratorConfig(BaseModel):
    """The `[orchestrator]` TOML section: every adapter entry that parsed
    successfully. Entries that failed either allowlist gate above never make
    it into this list -- see `load_orchestrator_config()`."""

    adapters: list[OrchestratorAdapterConfig] = []


def load_orchestrator_config(data: dict[str, Any]) -> tuple[OrchestratorConfig, list[str]]:
    """Validate the `[[orchestrator.adapters]]` entries of an already-parsed
    TOML document.

    Each entry is validated independently -- unlike a plain
    `OrchestratorConfig.model_validate(section)` (which would fail the whole
    list on one bad entry), a single malformed entry (off-allowlist command,
    undeclarable output mode, or any other schema violation) is excluded and
    recorded as a warning, mirroring `config/delegate.py`/`config/chains.py`'s
    "a bad entry degrades that entry, not the whole load" convention.

    Returns `(config, warnings)` -- `warnings` never crashes config loading.
    """
    section = data.get("orchestrator", {})
    raw_adapters = section.get("adapters", [])

    warnings: list[str] = []
    parsed: list[OrchestratorAdapterConfig] = []
    for entry in raw_adapters:
        entry_name = entry.get("name", "<unnamed>") if isinstance(entry, dict) else "<unnamed>"
        try:
            adapter = OrchestratorAdapterConfig.model_validate(entry)
        except ValidationError as exc:
            warnings.append(
                f"orchestrator adapter {entry_name!r} rejected at load time: {exc}"
            )
            continue
        parsed.append(adapter)

    return OrchestratorConfig(adapters=parsed), warnings


def usable_adapters(
    config: OrchestratorConfig, *, today: date | None = None
) -> tuple[list[OrchestratorAdapterConfig], list[str]]:
    """Apply R37's attestation gate to already-parsed adapter entries.

    `verified = false` excludes the adapter entirely (not offered as usable).
    A `verified_date` older than `STALENESS_WINDOW_DAYS` produces a warning
    but the adapter stays usable -- staleness is a nudge to re-attest, not a
    block.
    """
    resolved_today = today if today is not None else date.today()

    usable: list[OrchestratorAdapterConfig] = []
    warnings: list[str] = []
    for adapter in config.adapters:
        attestation = adapter.mcp_approval_verified
        if not attestation.verified:
            warnings.append(
                f"orchestrator adapter {adapter.name!r} excluded: "
                "mcp_approval_verified.verified is false"
            )
            continue

        age_days = (resolved_today - attestation.verified_date).days
        if age_days > STALENESS_WINDOW_DAYS:
            warnings.append(
                f"orchestrator adapter {adapter.name!r}: mcp_approval_verified.verified_date "
                f"({attestation.verified_date}) is {age_days} days old, older than the "
                f"{STALENESS_WINDOW_DAYS}-day staleness window -- usable, but re-verify "
                "non-interactive MCP tool approval still holds"
            )

        usable.append(adapter)

    return usable, warnings
