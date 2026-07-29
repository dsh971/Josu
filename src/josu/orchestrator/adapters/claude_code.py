"""Claude Code's adapter entry (U4) -- the reference implementation of the
config-driven adapter contract (`config/orchestrator.py`, `orchestrator/adapter.py`).

`ADAPTER_ARGS_TEMPLATE`/`build_default_adapter_config()` below are fully
declarative: the same `command`/`args`/`structured_output_mode`/`field_mapping`
shape any future adapter's TOML entry would use, with no Claude-Code-specific
branch anywhere in `adapter.py`'s invocation path. Per the plan's Key
Technical Decisions, this module DOES still carry a small amount of
Claude-Code-specific *output-parsing* code beyond `adapter.py`'s generic flat
field-mapping -- `--output-format stream-json` produces a stream of
newline-delimited JSON events, not one flat JSON object, so this module
flattens that event stream into a single dict first (`flatten_stream_json_output()`)
and hands the result to `adapter.py`'s `extract_fields()` like any other
adapter's output. This is the documented, expected exception (the plan is
explicit that this is "expected, not a mechanism failure"), not evidence the
declarative mechanism silently needs custom code everywhere.

Also lives here: the git subcommand allowlist Claude Code's own `Bash(git ...)`
tool-permission strings are built from (deliberately excluding `push`,
`reset`, `clean`, `worktree`, and `config`/`-c` -- worktrees share the main
repo's object database, refs, and remotes, so any of those could affect state
outside the isolated worktree), and the `josu.toml`-exclusion check that
keeps a project-root config override out of anything `git add *`/`git commit *`
stages.

This module never constructs an invocation using `--dangerously-skip-permissions`
or `bypassPermissions`, under any code path -- `adapter.py`'s own
`_forbid_bypass_flags()` check is defense in depth on top of that, not the
only thing preventing it.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from josu.config.orchestrator import McpApprovalVerified, OrchestratorAdapterConfig
from josu.orchestrator.adapter import (
    InvocationResult,
    assert_paths_within_worktree,
    extract_fields,
    invoke_adapter,
)
from josu.orchestrator.mcp_manifest import (
    DELEGATE_SERVER_NAME,
    GRAPH_SERVER_NAME,
    MCPManifest,
    validate_manifest,
)
from josu.orchestrator.worktree import Worktree, worktree_diff

ADAPTER_NAME = "claude-code"

# The two MCP servers `daemon.py` serves (U1/U2) -- an invocation is only
# valid if the `system/init` event confirms BOTH connected, not just one.
REQUIRED_MCP_SERVERS = (GRAPH_SERVER_NAME, DELEGATE_SERVER_NAME)

# The git subcommands Claude Code's Bash tool is allowed to run inside a
# worktree (see module docstring for why the exclusions matter -- worktrees
# share the main repo's object database/refs/remotes with the developer's
# real checkout).
_ALLOWED_GIT_SUBCOMMANDS = frozenset({"status", "diff", "add", "commit"})
_FORBIDDEN_GIT_TOKENS = frozenset({"push", "reset", "clean", "worktree", "config", "-c"})
# Extra defense against a single Bash tool call chaining a disallowed git
# invocation behind an allowed one (e.g. `git add * ; git push`) -- `shlex`
# alone would still see `push` as a token, but checking for these up front
# is a cheap, obviously-correct first gate before even tokenizing.
_SHELL_METACHARACTERS = (";", "&&", "|", "`", "$(")

# Claude Code's own tool-permission strings for the two MCP servers this
# pipeline's daemon exposes -- `mcp__<server-name>__<tool-name>`, matching
# `graph/server.py`'s "search"/"execute" tools and `delegate/server.py`'s
# single "delegate_to_local" tool exactly.
_ALLOWED_TOOLS = (
    f"mcp__{GRAPH_SERVER_NAME}__search",
    f"mcp__{GRAPH_SERVER_NAME}__execute",
    f"mcp__{DELEGATE_SERVER_NAME}__delegate_to_local",
    "Read({worktree}/**)",
    "Edit({worktree}/**)",
    "Bash(git status)",
    "Bash(git diff)",
    "Bash(git add *)",
    "Bash(git commit *)",
)


def _allowed_tools_arg() -> str:
    return ",".join(_ALLOWED_TOOLS)


# Fully declarative invocation template (R36): `{worktree}`, `{mcp_config}`,
# and `{task}` are substituted per-argument by `adapter.py`'s `build_argv()`.
# `-p` and the flags are grouped first, `{task}` last as the trailing
# positional prompt -- matching the plan's literal
# `claude -p --strict-mcp-config <manifest> --allowedTools "..."` ordering,
# with `--output-format stream-json` and the task text appended.
ADAPTER_ARGS_TEMPLATE: list[str] = [
    "-p",
    "--strict-mcp-config",
    "{mcp_config}",
    "--allowedTools",
    _allowed_tools_arg(),
    "--output-format",
    "stream-json",
    "{task}",
]

# The flat field-mapping applied (via `adapter.py`'s `extract_fields()`) to
# the flattened stream-json output -- declarative, same shape any other
# adapter's `field_mapping` would use.
FIELD_MAPPING: dict[str, str] = {
    "result": "result",
    "session_id": "session_id",
    "is_error": "is_error",
}


def build_default_adapter_config(mcp_approval_verified: McpApprovalVerified) -> OrchestratorAdapterConfig:
    """Construct Claude Code's `OrchestratorAdapterConfig` entry -- the
    programmatic equivalent of hand-authoring the matching
    `[[orchestrator.adapters]]` TOML table. `mcp_approval_verified` is passed
    in rather than hard-coded here: it's an attestation someone makes after
    independently confirming non-interactive MCP tool approval (R37), not a
    fact this module can assert about itself.
    """
    return OrchestratorAdapterConfig(
        name=ADAPTER_NAME,
        command="claude",
        args=list(ADAPTER_ARGS_TEMPLATE),
        structured_output_mode="stream-json",
        field_mapping=dict(FIELD_MAPPING),
        mcp_approval_verified=mcp_approval_verified,
    )


class MCPServerConnectionError(RuntimeError):
    """Raised when the `system/init` stream-json event doesn't confirm both
    required MCP servers connected -- a run in this state is flagged, never
    silently treated as valid."""


class GitAllowlistViolationError(RuntimeError):
    """Raised when a recorded Bash tool call invokes a git subcommand
    outside the allowlist (see module docstring)."""


class ConfigPathStagedError(RuntimeError):
    """Raised when `josu.toml`'s resolved path (a project-root config
    override) is staged in the worktree's git index -- defends against
    `git add *`/`git commit *` sweeping it into an unattended commit."""


@dataclass(frozen=True)
class ParsedRun:
    """The flattened result of parsing a `stream-json` invocation's stdout:
    which MCP servers the `system/init` event confirmed connected, every
    Bash tool call recorded (for the git-allowlist check), and the final
    `result` event's fields (if any)."""

    mcp_servers_connected: frozenset[str]
    bash_commands: list[str]
    read_edit_paths: list[str]
    result_event: dict[str, Any] = field(default_factory=dict)
    raw_events: list[dict[str, Any]] = field(default_factory=list)


def parse_stream_json(stdout: str) -> ParsedRun:
    """Parse Claude Code's `--output-format stream-json` stdout: one JSON
    object per newline-delimited event. Lines that aren't valid JSON are
    skipped rather than raising -- stray non-JSON output (a warning banner,
    a blank line) shouldn't crash parsing of an otherwise-valid stream.

    This is the small amount of CLI-specific parsing the plan's Key
    Technical Decisions call out as the expected exception to a flat
    field-mapping (R36) -- Claude Code's structured output is a stream of
    typed events, not one JSON object.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    mcp_servers_connected: set[str] = set()
    bash_commands: list[str] = []
    read_edit_paths: list[str] = []
    result_event: dict[str, Any] = {}

    for event in events:
        event_type = event.get("type")

        if event_type == "system" and event.get("subtype") == "init":
            for server in event.get("mcp_servers", []):
                if isinstance(server, dict) and server.get("status") == "connected":
                    name = server.get("name")
                    if name:
                        mcp_servers_connected.add(name)

        elif event_type == "assistant":
            message = event.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name")
                tool_input = block.get("input", {})
                if not isinstance(tool_input, dict):
                    continue
                if tool_name == "Bash" and isinstance(tool_input.get("command"), str):
                    bash_commands.append(tool_input["command"])
                elif tool_name in ("Read", "Edit") and isinstance(
                    tool_input.get("file_path"), str
                ):
                    read_edit_paths.append(tool_input["file_path"])

        elif event_type == "result":
            result_event = event

    return ParsedRun(
        mcp_servers_connected=frozenset(mcp_servers_connected),
        bash_commands=bash_commands,
        read_edit_paths=read_edit_paths,
        result_event=result_event,
        raw_events=events,
    )


def assert_both_mcp_servers_connected(parsed: ParsedRun) -> None:
    """Confirm the `system/init` event reported BOTH required MCP servers
    connected -- a run where one failed to connect is flagged (raises)
    rather than silently proceeding as if the tool surface were complete."""
    missing = set(REQUIRED_MCP_SERVERS) - set(parsed.mcp_servers_connected)
    if missing:
        raise MCPServerConnectionError(
            f"MCP server(s) not confirmed connected in system/init event: {sorted(missing)}"
        )


def is_git_subcommand_allowed(command: str) -> bool:
    """Pure allowlist check over a single shell-style git invocation string,
    as it would appear in a recorded `Bash` tool call.

    Rejects outright on any shell metacharacter (`;`, `&&`, `|`, backtick,
    `$(`) that could chain a second, disallowed command behind an allowed
    one -- e.g. `git add * ; git push`. Otherwise tokenizes with `shlex` (so
    quoting can't hide a forbidden token) and requires the command to be a
    bare `git <allowed-subcommand> ...` invocation with no forbidden token
    anywhere in it -- e.g. `git config alias.x '!rm -rf /'` still contains
    the literal `config` token and is rejected, even though it's disguised
    as setting a git alias rather than running one directly.
    """
    if any(meta in command for meta in _SHELL_METACHARACTERS):
        return False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    if not tokens or tokens[0] != "git":
        return False

    rest = tokens[1:]
    if not rest:
        return False

    if any(token in _FORBIDDEN_GIT_TOKENS for token in rest):
        return False

    return rest[0] in _ALLOWED_GIT_SUBCOMMANDS


def _looks_like_git(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True  # unparsable -- treat conservatively as needing the full check
    return bool(tokens) and tokens[0] == "git"


def assert_git_allowlist_respected(parsed: ParsedRun) -> None:
    """Check every recorded Bash tool call that invokes `git` against
    `is_git_subcommand_allowed()`. Raises on the first violation -- the
    wrapper's own re-check of the CLI's behavior, independent of whatever
    `--allowedTools` itself enforced during the run. Non-git Bash calls
    (e.g. a plain `git`-free command that somehow slipped past
    `--allowedTools`) are outside this check's scope -- it only judges git
    invocations."""
    for command in parsed.bash_commands:
        if _looks_like_git(command) and not is_git_subcommand_allowed(command):
            raise GitAllowlistViolationError(
                f"recorded Bash tool call {command!r} is not an allowed git invocation"
            )


def assert_config_not_staged(worktree: Path, config_path: Path) -> None:
    """Defend the case where `josu.toml` lives inside `worktree` (a
    project-root config override -- `config/__init__.py`'s XDG default keeps
    it outside any git-tracked tree, but a project can override that) from
    being swept into a `git add *`/`git commit *` Claude Code performs.

    A no-op if `config_path` doesn't resolve under `worktree` at all (the
    default case). Otherwise inspects the worktree's git index directly
    (`git diff --cached --name-only`) and raises if the resolved config path
    is staged.
    """
    resolved_worktree = worktree.resolve()
    try:
        resolved_config = config_path.resolve()
        resolved_config.relative_to(resolved_worktree)
    except (OSError, ValueError):
        return

    result = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--name-only"],
        shell=False,
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        staged = line.strip()
        if not staged:
            continue
        if (worktree / staged).resolve() == resolved_config:
            raise ConfigPathStagedError(
                f"{config_path} (a project-root josu.toml override) is staged in the "
                "worktree's git index -- refusing to let it be swept into git add/commit"
            )


def _flatten(parsed: ParsedRun) -> dict[str, Any]:
    flat = dict(parsed.result_event)
    flat["mcp_servers_connected"] = sorted(parsed.mcp_servers_connected)
    return flat


def flatten_stream_json_output(
    stdout: str,
    *,
    after_validated: Callable[[ParsedRun], None] | None = None,
) -> dict[str, Any]:
    """Collapse Claude Code's stream-json event stream into one flat dict so
    `adapter.py`'s generic `extract_fields()` can then run against it like
    any other adapter's output -- the one piece of CLI-specific parsing this
    adapter needs (expected, see module docstring), not a workaround of the
    declarative mechanism.

    Also enforces the both-MCP-servers-connected check (raises if not
    confirmed) -- a run that didn't confirm both servers never reaches the
    point of having its output fields extracted at all.

    `after_validated`, if given, is called with the structured `ParsedRun`
    immediately after that check passes and before flattening -- this is the
    splice point `run()` uses for its own extra wrapper-side validations
    (path-within-worktree, git-allowlist, config-not-staged), which need the
    structured `ParsedRun` (recorded Bash calls, Read/Edit paths), not just
    the flat dict this function returns. Letting `run()` hook in here means
    it calls this one shared pipeline implementation instead of maintaining
    a second, hand-copied parse-then-flatten pipeline of its own.
    """
    parsed = parse_stream_json(stdout)
    assert_both_mcp_servers_connected(parsed)
    if after_validated is not None:
        after_validated(parsed)
    return _flatten(parsed)


def extract_result_and_diff(
    stdout: str,
    *,
    field_mapping: Mapping[str, str] | None = None,
    after_validated: Callable[[ParsedRun], None] | None = None,
) -> dict[str, Any]:
    """The full output-extraction path for a completed invocation: flatten
    the stream-json event stream (via `flatten_stream_json_output()`), then
    apply a declarative field-mapping via `adapter.py`'s generic
    `extract_fields()` -- the module-level `FIELD_MAPPING` by default, or a
    caller-supplied `field_mapping` (`run()` passes `adapter.field_mapping`,
    the config-driven mapping for that specific adapter entry, which is
    `FIELD_MAPPING` in the default config but need not be for a hand-authored
    `[[orchestrator.adapters]]` entry). `after_validated` is forwarded to
    `flatten_stream_json_output()` unchanged -- see its docstring.
    """
    flat = flatten_stream_json_output(stdout, after_validated=after_validated)
    resolved_mapping = FIELD_MAPPING if field_mapping is None else field_mapping
    return extract_fields(resolved_mapping, flat)


@dataclass(frozen=True)
class ClaudeCodeRunResult:
    """Everything a caller (U5's future merge step, U6's future run log)
    needs from one Claude Code invocation: the raw invocation, the parsed
    event stream, the declaratively-extracted fields, and the worktree's
    resulting diff -- only ever populated once every wrapper-side validation
    below has passed."""

    invocation: InvocationResult
    parsed: ParsedRun
    fields: dict[str, Any]
    diff: str


def run(
    adapter: OrchestratorAdapterConfig,
    *,
    worktree: Worktree,
    manifest: MCPManifest,
    config_path: Path,
    task: str,
    timeout: float | None = None,
) -> ClaudeCodeRunResult:
    """Run Claude Code against `worktree` via its declarative adapter entry,
    then apply every wrapper-side validation before treating the result as
    valid -- the CLI's own claims (a tool call reporting success, its own
    `--allowedTools` enforcement) are never trusted on their own:

    1. Re-validate the MCP manifest against the known-good daemon address
       immediately before invocation (closes the generation-to-invocation gap).
    2. Invoke via `adapter.py`'s `invoke_adapter()` (argv-list, `shell=False`,
       command allowlist + forbidden-flag checks already applied there).
    3. Parse `stream-json` output and confirm both required MCP servers
       connected -- raises rather than silently proceeding if not.
    4. Verify every Read/Edit path the CLI's tool calls claim to have
       touched resolves under the worktree root, independent of whether the
       CLI's own tool call reported success.
    5. Verify every recorded git Bash call respects the git subcommand
       allowlist.
    6. Verify `config_path` (a project-root `josu.toml` override, if in use)
       was never staged for `git add`/`git commit`.
    7. Extract the declarative result fields and combine with the
       worktree's actual `git diff` -- the diff is only ever returned after
       every check above has passed.
    """
    validate_manifest(manifest)

    invocation = invoke_adapter(
        adapter,
        worktree=worktree.path,
        mcp_config=manifest.path,
        task=task,
        timeout=timeout,
    )

    # `extract_result_and_diff()` (via `flatten_stream_json_output()`) is the
    # SAME parse -> confirm-MCP-servers -> flatten -> extract pipeline used
    # everywhere else -- steps 4-6 of this function's docstring are spliced
    # in via `after_validated`, right after the MCP-servers check passes and
    # before the output is flattened, since they need the structured
    # `ParsedRun` those functions parse internally. This is exactly one
    # pipeline implementation, not two hand-copied ones.
    parsed: ParsedRun | None = None

    def _apply_wrapper_side_checks(candidate: ParsedRun) -> None:
        nonlocal parsed
        assert_paths_within_worktree(candidate.read_edit_paths, worktree.path)
        assert_git_allowlist_respected(candidate)
        assert_config_not_staged(worktree.path, config_path)
        parsed = candidate

    fields = extract_result_and_diff(
        invocation.stdout,
        field_mapping=adapter.field_mapping,
        after_validated=_apply_wrapper_side_checks,
    )
    assert parsed is not None  # after_validated always runs before a successful return above
    diff = worktree_diff(worktree)

    return ClaudeCodeRunResult(invocation=invocation, parsed=parsed, fields=fields, diff=diff)
