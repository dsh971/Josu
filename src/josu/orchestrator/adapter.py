"""The config-driven adapter engine (U4, R18, R19, R22, R36).

Reads an `OrchestratorAdapterConfig` (`config/orchestrator.py`) and drives
invocation: substitutes `{worktree}`/`{mcp_config}`/`{task}` placeholders
PER-ARGUMENT into an argv list, then invokes via `subprocess.run(..., shell=False)`
-- never a shell string. The command allowlist check (also applied once at
config-load time -- see `config/orchestrator.py`) is re-checked here too,
immediately before any subprocess is spawned, as defense in depth: this
module is the one true boundary where an argv actually gets executed, so it
does not trust that whatever `OrchestratorAdapterConfig` it was handed
already passed that gate.

Because each template argument is substituted with `str.format()` into ITS
OWN string and appended as ONE argv element, a `{task}` (or `{worktree}`)
value containing shell metacharacters (`; rm -rf /`) or a path-traversal
sequence (`../../etc/passwd`) can never split into multiple arguments or
escape its slot -- `subprocess.run(argv, shell=False)` hands the OS the argv
list directly, with no shell involved to reinterpret anything inside a
single argument. See `test_adapter.py`'s injection-safety test.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from josu.config.orchestrator import ALLOWED_ADAPTER_COMMANDS, OrchestratorAdapterConfig

# Never permitted in an invocation argv, under any code path (R19, R22).
# Matched case-insensitively and via substring so `--dangerously-skip-permissions=true`,
# a differently-cased variant, or a value embedding one of these strings all
# still trip it -- this check exists specifically to catch a mistake, so it
# errs toward over-matching rather than under-matching.
_FORBIDDEN_FLAG_SUBSTRINGS = (
    "dangerously-skip-permissions",
    "bypasspermissions",
    "bypass-permissions",
)


class DisallowedCommandError(RuntimeError):
    """Raised when an adapter's `command` is not on the allowlist -- should
    be unreachable in practice (config-load time already filters these out),
    but this module never assumes that and checks again."""


class ForbiddenFlagError(RuntimeError):
    """Raised if a built invocation argv would include a permission-bypass
    flag, under any code path. See module docstring."""


class PathEscapesWorktreeError(RuntimeError):
    """Raised when a path the invoked CLI claims to have read or edited does
    not resolve under the worktree root -- the wrapper's own check, applied
    independent of whatever the CLI itself reported."""


class McpApprovalNotVerifiedError(RuntimeError):
    """Raised when an adapter's `mcp_approval_verified.verified` is not
    `True`, checked here -- immediately before invocation -- rather than
    only in `config/orchestrator.py`'s `usable_adapters()`.

    `usable_adapters()` is a convenience filter for callers that want a
    pre-screened list; it is not on the actual invocation path (`run()` in
    `adapters/claude_code.py` calls `invoke_adapter()` directly, never
    `usable_adapters()`), so relying on it alone would mean R37's gate is
    enforced only for callers that happen to filter through it first. This
    check makes the gate unconditional: it fires here even if a caller
    bypasses `usable_adapters()` entirely and hands an unverified adapter
    straight to `invoke_adapter()`."""


@dataclass(frozen=True)
class InvocationResult:
    """The raw result of running an adapter's invocation -- argv actually
    executed (for logging/debugging), exit code, and captured stdout/stderr.
    Output parsing (flat field-mapping, or an adapter's own CLI-specific
    parsing) happens on top of this, not inside it."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def _check_command_allowlisted(command: str) -> None:
    if command not in ALLOWED_ADAPTER_COMMANDS:
        raise DisallowedCommandError(
            f"adapter command {command!r} is not on the allowlist "
            f"{sorted(ALLOWED_ADAPTER_COMMANDS)} -- refusing to spawn a subprocess for it"
        )


def _forbid_bypass_flags(argv: Sequence[str]) -> None:
    for arg in argv:
        lowered = arg.lower()
        if any(forbidden in lowered for forbidden in _FORBIDDEN_FLAG_SUBSTRINGS):
            raise ForbiddenFlagError(
                f"invocation argv must never include a permission-bypass flag "
                f"(R19/R22); found in argument: {arg!r}"
            )


def build_argv(
    adapter: OrchestratorAdapterConfig,
    *,
    worktree: Path,
    mcp_config: Path,
    task: str,
) -> list[str]:
    """Substitute `{worktree}`/`{mcp_config}`/`{task}` into `adapter.args`
    PER-ARGUMENT -- each template string in `adapter.args` is formatted on
    its own and appended as exactly one argv element. `task` (or `worktree`)
    values are inserted verbatim by `str.format()`; it does not re-parse the
    substituted value for further placeholders or shell syntax, so embedded
    braces, quotes, semicolons, or `../` sequences in `task` stay inert
    literal text within that one argument slot.
    """
    substitutions = {
        "worktree": str(worktree),
        "mcp_config": str(mcp_config),
        "task": task,
    }
    return [adapter.command, *(arg.format(**substitutions) for arg in adapter.args)]


def _check_mcp_approval_verified(adapter: OrchestratorAdapterConfig) -> None:
    if not adapter.mcp_approval_verified.verified:
        raise McpApprovalNotVerifiedError(
            f"adapter {adapter.name!r}: mcp_approval_verified.verified is not True -- "
            "refusing to invoke; usable_adapters() is a convenience filter, not "
            "an enforcement point, so this is checked again here, immediately before "
            "any subprocess is spawned"
        )


def _build_subprocess_env(forbidden_env_names: Iterable[str] = ()) -> dict[str, str]:
    """Build the environment passed to the spawned adapter subprocess.

    Starts from a copy of THIS process's own environment -- the invoked CLI
    (Claude Code) needs at minimum `PATH` (to find its own runtime/
    dependencies) and `HOME` (to locate its own credentials/config) to run
    at all, and the full safe allowlist of every variable the `claude` CLI
    might legitimately read isn't confidently enumerable here -- then
    explicitly deletes every name in `forbidden_env_names`.

    Callers are expected to supply every configured delegate candidate's
    `api_key_env` name (see `adapters/claude_code.py`'s `run()` /
    `_forbidden_env_names()`, which sources these from the loaded
    `DelegateConfig`): those are exactly the resolved remote-delegate
    credentials the daemon process may hold in its own environment that a
    spawned Claude Code subprocess has no legitimate need for. This is a
    documented minimum -- excluding known delegate-credential env var names
    -- not a full safe-allowlist; any other sensitive variable not
    identified this way is NOT scrubbed by this function.
    """
    env = dict(os.environ)
    for name in forbidden_env_names:
        env.pop(name, None)
    return env


def invoke_adapter(
    adapter: OrchestratorAdapterConfig,
    *,
    worktree: Path,
    mcp_config: Path,
    task: str,
    timeout: float | None = None,
    forbidden_env_names: Iterable[str] = (),
) -> InvocationResult:
    """Build the argv for `adapter` and run it via `subprocess.run(shell=False)`.

    Order of checks, all before any subprocess is spawned:
    1. `command` allowlist (defense in depth; also checked at config-load time).
    2. `mcp_approval_verified.verified` gate (R37) -- enforced here, at the
       point of actual invocation, not just in `usable_adapters()`.
    3. Build the argv (per-argument substitution, never a shell string).
    4. Forbidden-flag check (R19/R22) against the fully-built argv.

    The spawned subprocess's environment is an explicit, scrubbed copy (see
    `_build_subprocess_env()`) -- never the bare, unfiltered parent
    environment -- so `forbidden_env_names` (typically delegate-candidate
    `api_key_env` names) never reaches the invoked CLI.
    """
    _check_command_allowlisted(adapter.command)
    _check_mcp_approval_verified(adapter)
    argv = build_argv(adapter, worktree=worktree, mcp_config=mcp_config, task=task)
    _forbid_bypass_flags(argv)

    proc = subprocess.run(
        argv,
        cwd=str(worktree),
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_build_subprocess_env(forbidden_env_names),
    )
    return InvocationResult(
        argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def extract_fields(field_mapping: Mapping[str, str], data: Mapping[str, Any]) -> dict[str, Any]:
    """The generic, declarative flat field-mapping extractor: each
    `field_mapping` value is a dot-separated path into `data`. Handles any
    adapter whose structured output is (or has already been flattened to) a
    single JSON-object-shaped mapping. An adapter whose native output isn't
    flat (Claude Code's `stream-json` event stream) does its own flattening
    first and passes the result through here -- see
    `adapters/claude_code.py`; that CLI-specific step is the documented,
    expected exception to this being a flat mapping everywhere (R36).

    A path segment missing from `data` simply omits that logical field from
    the result, rather than raising -- an adapter's output not containing
    every mapped field is a normal, non-fatal outcome (e.g. no diff produced
    for a no-op task).
    """
    result: dict[str, Any] = {}
    for logical_name, path in field_mapping.items():
        value: Any = data
        found = True
        for part in path.split("."):
            if isinstance(value, Mapping) and part in value:
                value = value[part]
            else:
                found = False
                break
        if found:
            result[logical_name] = value
    return result


def assert_paths_within_worktree(paths: Iterable[str | Path], worktree: Path) -> None:
    """Verify every path in `paths` resolves under `worktree` -- called
    before treating any diff/result the invoked CLI produced as valid,
    independent of whatever the CLI's own tool call claimed. Raises
    `PathEscapesWorktreeError` on the first path that doesn't resolve under
    the worktree root (e.g. a symlink or a `../` sequence pointing outside
    it), even if the CLI's tool call reported success for it.
    """
    resolved_worktree = worktree.resolve()
    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = worktree / candidate
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(resolved_worktree)
        except ValueError:
            raise PathEscapesWorktreeError(
                f"path {raw_path!r} resolves to {resolved_candidate}, which is outside "
                f"the worktree root {resolved_worktree} -- rejected even though the CLI's "
                "own tool call may have reported success for it"
            ) from None
