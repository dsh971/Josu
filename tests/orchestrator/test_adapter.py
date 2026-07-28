"""Tests for the config-driven adapter engine (U4, R18, R19, R22, R36).

The injection-safety and forbidden-flag tests are the security-sensitive
core of this unit -- not boilerplate. `fake_claude_bin` (conftest.py)
installs a real, hand-written fake `claude` executable on `PATH`; the actual
subprocess boundary (`subprocess.run`) is never mocked, only the external
binary it launches is a fake, matching this repo's existing testing
convention.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from josu.config.orchestrator import McpApprovalVerified, OrchestratorAdapterConfig
from josu.orchestrator.adapter import (
    DisallowedCommandError,
    ForbiddenFlagError,
    PathEscapesWorktreeError,
    assert_paths_within_worktree,
    build_argv,
    extract_fields,
    invoke_adapter,
)


def _adapter(**overrides) -> OrchestratorAdapterConfig:
    defaults = dict(
        name="claude-code",
        command="claude",
        args=["-p", "--strict-mcp-config", "{mcp_config}", "{task}"],
        structured_output_mode="stream-json",
        field_mapping={"result": "result"},
        mcp_approval_verified=McpApprovalVerified(verified=True, verified_date=date(2026, 1, 1)),
    )
    defaults.update(overrides)
    return OrchestratorAdapterConfig(**defaults)


# --- Per-argument placeholder substitution / argv-list safety ---------------


def test_build_argv_substitutes_placeholders_per_argument():
    adapter = _adapter()
    argv = build_argv(
        adapter,
        worktree=Path("/tmp/worktree-1"),
        mcp_config=Path("/tmp/manifest.json"),
        task="summarize the auth module",
    )
    assert argv == [
        "claude",
        "-p",
        "--strict-mcp-config",
        "/tmp/manifest.json",
        "summarize the auth module",
    ]


def test_shell_metacharacters_in_task_stay_inert_within_one_argv_slot():
    """Covers: a {task} value containing shell metacharacters does not alter
    the invoked command or escape its argument slot -- invocation is
    argv-list, never a shell string."""
    adapter = _adapter()
    malicious_task = "summarize this; rm -rf /"

    argv = build_argv(
        adapter,
        worktree=Path("/tmp/worktree-1"),
        mcp_config=Path("/tmp/manifest.json"),
        task=malicious_task,
    )

    # The whole malicious string is exactly ONE argv element -- never split
    # into "summarize this;" and "rm" and "-rf" and "/" as a shell would.
    assert argv[-1] == malicious_task
    assert argv.count(malicious_task) == 1
    assert "rm" not in argv[:-1]


def test_path_traversal_sequence_in_task_stays_literal_text():
    adapter = _adapter()
    traversal_task = "../../etc/passwd"

    argv = build_argv(
        adapter,
        worktree=Path("/tmp/worktree-1"),
        mcp_config=Path("/tmp/manifest.json"),
        task=traversal_task,
    )

    assert argv[-1] == traversal_task
    assert len(argv) == 5  # unchanged shape -- traversal text didn't add/remove argv elements


def test_injected_task_never_alters_the_invoked_command_via_real_subprocess(
    fake_claude_bin, tmp_path, monkeypatch
):
    """The full injection-safety scenario against the real subprocess
    boundary: a fake `claude` executable records the exact argv it received
    (as JSON, from inside a real, separately-spawned OS process) -- if
    `{task}` had ever leaked out of its argument slot (e.g. via
    shell=True), the recorded argv would show extra/mutated arguments or the
    fake binary would never even run (having been reinterpreted as shell
    syntax)."""
    argv_log = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT_FILE", "")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT_CODE", "0")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    malicious_task = "; rm -rf / #"

    adapter = _adapter(args=["-p", "{task}"])
    result = invoke_adapter(adapter, worktree=worktree, mcp_config=tmp_path / "m.json", task=malicious_task)

    assert result.returncode == 0
    recorded_argv = json.loads(argv_log.read_text(encoding="utf-8"))
    # argv[0] is the fake claude script's own path; the rest is exactly what
    # invoke_adapter built.
    assert recorded_argv[1:] == ["-p", malicious_task]


# --- Forbidden-flag check (R19/R22) -----------------------------------------


@pytest.mark.parametrize(
    "forbidden_arg",
    [
        "--dangerously-skip-permissions",
        "--dangerously-skip-permissions=true",
        "--bypassPermissions",
        "--bypass-permissions",
    ],
)
def test_forbidden_bypass_flags_are_rejected_under_any_code_path(forbidden_arg, tmp_path):
    adapter = _adapter(args=["-p", forbidden_arg, "{task}"])
    with pytest.raises(ForbiddenFlagError):
        invoke_adapter(adapter, worktree=tmp_path, mcp_config=tmp_path / "m.json", task="task")


def test_forbidden_flag_check_runs_before_any_subprocess_is_spawned(tmp_path):
    """Even with no real `claude` on PATH at all, the forbidden-flag check
    must raise before subprocess.run ever attempts to spawn anything --
    proven here by NOT installing fake_claude_bin and confirming the raised
    exception is ForbiddenFlagError, not a FileNotFoundError from a failed
    spawn attempt."""
    adapter = _adapter(args=["--dangerously-skip-permissions", "{task}"])
    with pytest.raises(ForbiddenFlagError):
        invoke_adapter(adapter, worktree=tmp_path, mcp_config=tmp_path / "m.json", task="task")


# --- Command allowlist re-check (defense in depth) --------------------------


def test_invoke_adapter_rechecks_command_allowlist():
    """adapter.py never trusts that whatever OrchestratorAdapterConfig it
    was handed already passed config-load time's allowlist gate -- it
    re-checks immediately before spawning anything. Constructed via
    model_construct to bypass pydantic's own field validator, simulating a
    config object that reached this code path some other way."""
    adapter = OrchestratorAdapterConfig.model_construct(
        name="bad",
        command="rm",
        args=["-rf", "{worktree}"],
        structured_output_mode="stream-json",
        field_mapping={},
        mcp_approval_verified=McpApprovalVerified(verified=True, verified_date=date(2026, 1, 1)),
    )
    with pytest.raises(DisallowedCommandError):
        invoke_adapter(adapter, worktree=Path("/tmp"), mcp_config=Path("/tmp/m.json"), task="x")


# --- Path-within-worktree wrapper check -------------------------------------


def test_assert_paths_within_worktree_accepts_paths_under_root(tmp_path):
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "a.py").write_text("x", encoding="utf-8")

    assert_paths_within_worktree(["src/a.py", str(worktree / "src" / "a.py")], worktree)  # no raise


def test_assert_paths_within_worktree_rejects_path_outside_root_even_if_cli_claimed_success(
    tmp_path,
):
    """Covers: a file path outside the worktree root is rejected by the
    wrapper's check even if the CLI's own tool call claimed success -- this
    function doesn't know or care what the CLI claimed, it only checks where
    the path resolves."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(PathEscapesWorktreeError):
        assert_paths_within_worktree([str(outside)], worktree)


def test_assert_paths_within_worktree_rejects_traversal_sequence(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(PathEscapesWorktreeError):
        assert_paths_within_worktree(["../../etc/passwd"], worktree)


# --- Generic flat field-mapping extraction ----------------------------------


def test_extract_fields_flat_mapping():
    mapping = {"result": "result", "nested": "outer.inner"}
    data = {"result": "ok", "outer": {"inner": "value"}}
    assert extract_fields(mapping, data) == {"result": "ok", "nested": "value"}


def test_extract_fields_omits_missing_paths_rather_than_raising():
    mapping = {"result": "result", "missing": "nope.nope"}
    data = {"result": "ok"}
    assert extract_fields(mapping, data) == {"result": "ok"}
