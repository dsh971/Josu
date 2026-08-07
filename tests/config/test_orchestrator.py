"""Tests for the config-driven hosted-orchestrator adapter schema (U4,
R36-R38).

Pure config logic -- tmp_path fixtures write real TOML, `tomllib` parses it,
and `load_orchestrator_config()`/`usable_adapters()` validate it. No mocks,
matching this repo's existing config-test conventions
(tests/config/test_delegate.py, tests/config/test_chains.py).
"""

from __future__ import annotations

import tomllib
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from josu.config.orchestrator import (
    ALLOWED_ADAPTER_COMMANDS,
    STALENESS_WINDOW_DAYS,
    McpApprovalVerified,
    OrchestratorAdapterConfig,
    load_orchestrator_config,
    usable_adapters,
)


def _parse(toml_text: str, tmp_path) -> dict:
    config_path = tmp_path / "josu.toml"
    config_path.write_text(toml_text, encoding="utf-8")
    with config_path.open("rb") as f:
        return tomllib.load(f)


_VALID_CLAUDE_ADAPTER_TOML = """
[[orchestrator.adapters]]
name = "claude-code"
command = "claude"
args = ["-p", "--strict-mcp-config", "{mcp_config}", "--allowedTools", "Read({worktree}/**)", "{task}"]
structured_output_mode = "stream-json"
field_mapping = { result = "result" }
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
"""


def test_valid_adapter_entry_parses_and_is_usable(tmp_path):
    data = _parse(_VALID_CLAUDE_ADAPTER_TOML, tmp_path)
    config, warnings = load_orchestrator_config(data)
    assert warnings == []
    assert [a.name for a in config.adapters] == ["claude-code"]

    usable, usable_warnings = usable_adapters(config, today=date(2026, 3, 1))
    assert [a.name for a in usable] == ["claude-code"]
    assert usable_warnings == []


def test_mcp_approval_verified_schema_is_the_trimmed_shape():
    """R37's attestation record is exactly {verified, verified_date} -- a
    larger shape (cli_version, verified_by, ...) was explicitly trimmed in a
    later plan revision. Extra/unknown fields not silently tolerated as part
    of a bigger intended shape -- this just asserts the two fields that must
    exist and that the model doesn't require anything more."""
    attestation = McpApprovalVerified(verified=True, verified_date=date(2026, 1, 1))
    assert attestation.model_dump() == {"verified": True, "verified_date": date(2026, 1, 1)}


def test_allowed_adapter_commands_starts_with_just_claude():
    assert ALLOWED_ADAPTER_COMMANDS == frozenset({"claude"})


def test_off_allowlist_command_rejected_at_load_time_independent_of_attestation(tmp_path):
    """Covers: an adapter entry whose `command` isn't on the allowlist is
    rejected at config-load time, independent of its mcp_approval_verified
    status -- here the attestation is fully valid and verified=true, and the
    entry is still excluded."""
    toml_text = """
[[orchestrator.adapters]]
name = "evil-adapter"
command = "rm"
args = ["-rf", "{worktree}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
"""
    data = _parse(toml_text, tmp_path)
    config, warnings = load_orchestrator_config(data)

    assert config.adapters == []
    assert len(warnings) == 1
    assert "evil-adapter" in warnings[0]
    assert "rm" in warnings[0]


def test_off_allowlist_command_rejected_directly_via_pydantic_validation():
    with pytest.raises(ValidationError):
        OrchestratorAdapterConfig(
            name="evil-adapter",
            command="bash",
            args=["-c", "echo hi"],
            structured_output_mode="stream-json",
            mcp_approval_verified=McpApprovalVerified(verified=True, verified_date=date(2026, 1, 1)),
        )


# A minimal, "-p"/"{task}"-only args shape -- no --strict-mcp-config, no
# --allowedTools -- is exactly the vulnerability this gate closes: it would
# grant Claude Code unrestricted tool access if actually invoked. This used
# to be accepted by load_orchestrator_config() with zero warnings (the bug);
# it must now be rejected at load time instead.
_UNSCOPED_ARGS_ADAPTER_TOML = """
[[orchestrator.adapters]]
name = "claude-code"
command = "claude"
args = ["-p", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }

[[orchestrator.adapters]]
name = "claude-code-unverified"
command = "claude"
args = ["-p", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = false, verified_date = 2026-01-01 }
"""


def test_unscoped_adapter_args_rejected_at_load_time_even_with_valid_attestation(tmp_path):
    """Covers the P0 gap: an adapter entry whose `args` doesn't include
    --strict-mcp-config and a non-empty --allowedTools value is rejected at
    config-load time, independent of whether its mcp_approval_verified
    attestation is otherwise complete (the first entry here is fully
    verified=true and still rejected on args alone)."""
    data = _parse(_UNSCOPED_ARGS_ADAPTER_TOML, tmp_path)
    config, warnings = load_orchestrator_config(data)

    assert config.adapters == []
    assert len(warnings) == 2
    # args=["-p", "{task}"] is missing --strict-mcp-config first (checked
    # before --allowedTools), so both entries' warnings name that.
    assert any("claude-code-unverified" not in w and "claude-code" in w and "strict-mcp-config" in w for w in warnings)
    assert any("claude-code-unverified" in w and "strict-mcp-config" in w for w in warnings)


def test_properly_scoped_adapter_entry_still_validates(tmp_path):
    """Covers: an adapter entry whose args DO include --strict-mcp-config
    and a non-empty --allowedTools value still parses and loads successfully
    -- the gate only rejects unscoped entries, not every custom entry."""
    toml_text = """
[[orchestrator.adapters]]
name = "claude-code-scoped"
command = "claude"
args = ["-p", "--strict-mcp-config", "{mcp_config}", "--allowedTools", "Read({worktree}/**)", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
"""
    data = _parse(toml_text, tmp_path)
    config, warnings = load_orchestrator_config(data)

    assert warnings == []
    assert [a.name for a in config.adapters] == ["claude-code-scoped"]


def test_verified_false_adapter_excluded_from_usable_list(tmp_path):
    """Covers: a second adapter config entry with mcp_approval_verified.verified
    = false is not offered as usable, even if its command template is
    otherwise complete. Uses properly-scoped args throughout so this test
    exercises the verified=false exclusion in isolation from the
    args-scoping gate covered separately above."""
    toml_text = """
[[orchestrator.adapters]]
name = "claude-code"
command = "claude"
args = ["-p", "--strict-mcp-config", "{mcp_config}", "--allowedTools", "Read({worktree}/**)", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }

[[orchestrator.adapters]]
name = "claude-code-unverified"
command = "claude"
args = ["-p", "--strict-mcp-config", "{mcp_config}", "--allowedTools", "Read({worktree}/**)", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = false, verified_date = 2026-01-01 }
"""
    data = _parse(toml_text, tmp_path)
    config, load_warnings = load_orchestrator_config(data)
    assert load_warnings == []
    assert {a.name for a in config.adapters} == {"claude-code", "claude-code-unverified"}

    usable, warnings = usable_adapters(config, today=date(2026, 3, 1))
    assert [a.name for a in usable] == ["claude-code"]
    assert any("claude-code-unverified" in w for w in warnings)
    assert any("verified is false" in w for w in warnings)


def test_stale_verified_date_warns_but_stays_usable(tmp_path):
    """Covers: verified = true and verified_date older than the staleness
    window produces a load-time warning but is still usable (not blocked)."""
    today = date(2026, 7, 25)
    stale_date = today - timedelta(days=STALENESS_WINDOW_DAYS + 1)
    toml_text = f"""
[[orchestrator.adapters]]
name = "claude-code"
command = "claude"
args = ["-p", "--strict-mcp-config", "{{mcp_config}}", "--allowedTools", "Read({{worktree}}/**)", "{{task}}"]
structured_output_mode = "stream-json"
mcp_approval_verified = {{ verified = true, verified_date = {stale_date.isoformat()} }}
"""
    data = _parse(toml_text, tmp_path)
    config, _ = load_orchestrator_config(data)

    usable, warnings = usable_adapters(config, today=today)
    assert [a.name for a in usable] == ["claude-code"]
    assert len(warnings) == 1
    assert "claude-code" in warnings[0]
    assert "stale" in warnings[0].lower() or "days old" in warnings[0]


def test_verified_date_within_window_produces_no_staleness_warning():
    today = date(2026, 7, 25)
    fresh_date = today - timedelta(days=STALENESS_WINDOW_DAYS - 1)
    adapter = OrchestratorAdapterConfig(
        name="claude-code",
        command="claude",
        args=["-p", "{task}"],
        structured_output_mode="stream-json",
        mcp_approval_verified=McpApprovalVerified(verified=True, verified_date=fresh_date),
    )
    from josu.config.orchestrator import OrchestratorConfig

    usable, warnings = usable_adapters(OrchestratorConfig(adapters=[adapter]), today=today)
    assert [a.name for a in usable] == ["claude-code"]
    assert warnings == []


def test_no_declarable_structured_output_mode_rejected_at_load_time(tmp_path):
    """Covers: an adapter with mcp_approval_verified.verified = true but no
    declarable structured-output mode is rejected at config-load time, not
    silently accepted then failing at invocation (R38)."""
    toml_text = """
[[orchestrator.adapters]]
name = "claude-code-freeform"
command = "claude"
args = ["-p", "{task}"]
structured_output_mode = "freeform-text"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
"""
    data = _parse(toml_text, tmp_path)
    config, warnings = load_orchestrator_config(data)

    assert config.adapters == []
    assert len(warnings) == 1
    assert "claude-code-freeform" in warnings[0]
    # feat/cli-ease-of-use plan (U5): this warning reaches the console now
    # that config warnings are surfaced -- it must not leak the internal
    # "(R38)" requirement-ID reference that used to be invisible.
    assert "R38" not in warnings[0]


def test_missing_structured_output_mode_rejected_at_load_time(tmp_path):
    toml_text = """
[[orchestrator.adapters]]
name = "claude-code-no-mode"
command = "claude"
args = ["-p", "{task}"]
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
"""
    data = _parse(toml_text, tmp_path)
    config, warnings = load_orchestrator_config(data)

    assert config.adapters == []
    assert len(warnings) == 1


def test_one_bad_adapter_entry_does_not_crash_the_whole_load(tmp_path):
    """Mirrors config/delegate.py's convention: a bad entry degrades that
    entry, not the whole config load."""
    toml_text = """
[[orchestrator.adapters]]
name = "good-claude"
command = "claude"
args = ["-p", "--strict-mcp-config", "{mcp_config}", "--allowedTools", "Read({worktree}/**)", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }

[[orchestrator.adapters]]
name = "bad-adapter"
command = "not-on-allowlist"
args = ["-p", "{task}"]
structured_output_mode = "stream-json"
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
"""
    data = _parse(toml_text, tmp_path)
    config, warnings = load_orchestrator_config(data)

    assert [a.name for a in config.adapters] == ["good-claude"]
    assert len(warnings) == 1
    assert "bad-adapter" in warnings[0]


def test_missing_orchestrator_section_yields_empty_adapters(tmp_path):
    data = _parse("allow_remote = false\n", tmp_path)
    config, warnings = load_orchestrator_config(data)
    assert config.adapters == []
    assert warnings == []
