"""Commit/save-triggered proactive checks (U9, R15-R17, R39).

Two event triggers -- a commit hook and a debounced file-save watcher --
plus an on-demand entry point (R16), all funnel through `run_proactive_check()`
below, which calls `delegate/chain.py`'s `execute_chain()` (U12) DIRECTLY:
no Claude Code invocation, no MCP transport, matching U7's `fallback/quota.py`
`route_bounded_request()` pattern for calling straight into the delegate
worker.

**R39 is the load-bearing behavior of this module.** `execute_chain()`
itself resolves candidates via `config/chains.py`'s `resolve_chain()` -- the
*general* per-task-type resolver, which honors the config-wide
`allow_remote` flag. It does NOT call `resolve_proactive_check_chain()`
(the distinct, local-only-by-construction resolver U3 built specifically
for this category). So simply calling
`execute_chain(PROACTIVE_CHECK_TASK_TYPE, ..., chains_config=<the developer's
real config>, registry=<the developer's real registry>)` would let a remote
candidate configured under the `proactive_check` task type slip through
whenever `allow_remote` happens to be set globally -- exactly the
unbounded, on-every-commit/save cost risk R39 exists to prevent.

`_local_only_execution_inputs()` closes that gap: it calls
`resolve_proactive_check_chain()` itself, then rebuilds a throwaway,
single-chain `ChainsConfig` (`allow_remote=False`) and a registry
containing ONLY the resolved local candidates, before ever calling
`execute_chain()`. A remote candidate is therefore structurally absent
from what `execute_chain()`'s own internal `resolve_chain()` call can see
for this call -- not filtered by a flag a developer's config could later
flip, but never present in the first place. If `resolve_proactive_check_chain()`
finds no local candidate at all, `run_proactive_check()` returns a skipped
outcome WITHOUT ever calling `execute_chain()` -- quietly, for that one
event, never falling through to a remote candidate and never raising (R39).

No standing background process (R17): this module contains no polling loop
or recurring timer that rescans the graph on its own cadence. The commit
hook only runs when `git commit` actually fires it (see
`install_commit_hook()`); `DebouncedSaveWatcher` only starts a one-shot,
event-triggered debounce timer in response to an explicit `on_save()` call
from an external save-event source (an editor plugin, `watchdog`, or
similar -- deliberately not built here, since this project takes no new
file-watching dependency and building one would itself risk becoming the
kind of standing process R17 rules out). Nothing in this module runs, or
schedules anything, on import alone.

"Serious finding" threshold: `DelegateResult` (`delegate/local_model.py`)
has no structured severity field, so `default_severity_classifier()` is a
keyword heuristic over the delegate's own `caveats`/`result` text --
mirroring `fallback/quota.py`'s `default_quota_classifier()` pattern (a
plain, injectable `Callable[[DelegateResult], bool]`, swappable without
touching `run_proactive_check()` itself, exactly like that module's
`QuotaClassifier` seam). A serious finding invokes the full Claude Code
loop via `orchestrator/adapters/claude_code.py`'s `run()` (U4) --
`default_claude_code_invoker()` below wraps it directly, not a
reimplementation of any of its wrapper-side validation.
"""

from __future__ import annotations

import re
import stat
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from josu.config.chains import (
    PROACTIVE_CHECK_TASK_TYPE,
    ChainsConfig,
    DelegationChain,
    resolve_proactive_check_chain,
)
from josu.config.delegate import DelegateCandidate
from josu.delegate.chain import ClientFactory, execute_chain
from josu.delegate.cooldown import CandidateCooldownStore
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS, DelegateResult
from josu.delegate.queue import DelegateQueue
from josu.graph.engine import GraphEngine

if TYPE_CHECKING:
    from josu.config.orchestrator import OrchestratorAdapterConfig
    from josu.orchestrator.adapters.claude_code import ClaudeCodeRunResult
    from josu.orchestrator.mcp_manifest import MCPManifest
    from josu.orchestrator.worktree import Worktree


# --- Severity classification -------------------------------------------


# Phrases that, if present in a local delegate finding's caveats/result
# text, mark it "serious" enough to wake the full hosted Claude Code loop
# (R15). Deliberately conservative and keyword-based (see module
# docstring) -- refining this is expected to happen by swapping the
# `severity_classifier` argument, never by editing this list in a vacuum.
_SERIOUS_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"security",
        r"vulnerab",
        r"credential",
        r"secret\s*(?:leak|exposed|expos)",
        r"data\s*loss",
        r"breaking\s*change",
        r"critical",
        r"crash",
        r"severe",
        r"unsafe",
        r"corrupt",
    )
)

SeverityClassifier = Callable[[DelegateResult], bool]


def default_severity_classifier(result: DelegateResult) -> bool:
    """Best-effort default heuristic: a `proactive_check` finding is
    "serious" if its `caveats` or `result` text matches one of
    `_SERIOUS_MARKERS`. Never hard-coded into `run_proactive_check()` --
    every caller can swap this out via the `severity_classifier` argument,
    matching `fallback/quota.py`'s `QuotaClassifier` seam.
    """
    text = f"{result.caveats}\n{result.result}"
    return any(pattern.search(text) for pattern in _SERIOUS_MARKERS)


# --- R39: local-only-by-construction projection -------------------------


def _local_only_execution_inputs(
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
) -> tuple[ChainsConfig | None, Mapping[str, DelegateCandidate]]:
    """Project `chains_config`/`registry` down to EXACTLY the ordered,
    local-only candidate list `resolve_proactive_check_chain()` (U3)
    returns, then rebuild a single-chain `ChainsConfig`
    (`explicit_order=True`, `allow_remote=False`) and a registry
    containing ONLY those candidates -- see module docstring for why this
    step exists at all (it's what makes calling `execute_chain()` directly
    R39-safe by construction, not merely by convention).

    Returns `(None, {})` if no local candidate is available -- the
    caller's cue to skip the check quietly for this event, never calling
    `execute_chain()` and never falling through to a remote candidate.
    """
    candidates = resolve_proactive_check_chain(chains_config, registry)
    if not candidates:
        return None, {}

    local_registry = {candidate.name: candidate for candidate in candidates}
    local_chains_config = ChainsConfig(
        chains=[
            DelegationChain(
                task_type=PROACTIVE_CHECK_TASK_TYPE,
                candidates=[candidate.name for candidate in candidates],
                explicit_order=True,
            )
        ],
        allow_remote=False,
    )
    return local_chains_config, local_registry


# --- Claude Code escalation (U4) -----------------------------------------


ClaudeCodeInvoker = Callable[[DelegateResult], Any]


@dataclass(frozen=True)
class ClaudeCodeEscalationConfig:
    """Everything `default_claude_code_invoker()` needs to run the full
    Claude Code loop (U4) for one serious finding: the adapter config, the
    worktree to run it in, the validated MCP manifest, and the project's
    `josu.toml` path (needed by `adapters/claude_code.py`'s `run()` for its
    config-not-staged check). Bundled as one object so a caller only wires
    a single optional argument through `run_proactive_check()` instead of
    four.
    """

    adapter: "OrchestratorAdapterConfig"
    worktree: "Worktree"
    manifest: "MCPManifest"
    config_path: Path
    timeout: float | None = None


def _escalation_task_text(finding: DelegateResult) -> str:
    """Compose the task text handed to Claude Code for a serious finding --
    the local delegate's own report plus its caveats, framed as an
    investigate-and-fix instruction rather than raw, unexplained model
    output."""
    return (
        "A proactive local check (josu, U9) surfaced a potentially serious "
        f"issue.\nLocal delegate finding: {finding.result}\n"
        f"Caveats/uncertainty noted by the local delegate: {finding.caveats}\n"
        "Investigate, confirm whether this is a real issue, and fix it if so."
    )


def default_claude_code_invoker(config: ClaudeCodeEscalationConfig) -> ClaudeCodeInvoker:
    """Build a `ClaudeCodeInvoker` bound to `config` -- the production
    escalation path: invokes the full Claude Code loop via
    `orchestrator/adapters/claude_code.py`'s `run()` (U4) directly, with
    every one of that module's own wrapper-side validations still applying
    unchanged. Not a reimplementation of any part of it.
    """

    def invoke(finding: DelegateResult) -> "ClaudeCodeRunResult":
        from josu.orchestrator.adapters.claude_code import run as claude_code_run

        return claude_code_run(
            config.adapter,
            worktree=config.worktree,
            manifest=config.manifest,
            config_path=config.config_path,
            task=_escalation_task_text(finding),
            timeout=config.timeout,
        )

    return invoke


# --- Core: shared by every trigger (commit hook, save watcher, on-demand) -


@dataclass(frozen=True)
class ProactiveCheckOutcome:
    """What came of one proactive-check invocation, regardless of which
    trigger (commit hook, debounced save, on-demand query) called
    `run_proactive_check()`.

    `skipped_no_local_candidate=True` means `execute_chain()` was NEVER
    called for this event (R39) -- `delegate_result`/`serious`/
    `claude_code_result` are all left at their defaults in that case.
    """

    skipped_no_local_candidate: bool
    delegate_result: DelegateResult | None = None
    serious: bool = False
    claude_code_result: Any | None = None


async def run_proactive_check(
    task: Any,
    scope: Any = None,
    *,
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
    queue: DelegateQueue,
    cooldown_store: CandidateCooldownStore,
    graph_engine: GraphEngine | None = None,
    scope_root: Path | None = None,
    client_factory: ClientFactory | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    available_ram_gb: float | None = None,
    severity_classifier: SeverityClassifier = default_severity_classifier,
    claude_code_invoker: ClaudeCodeInvoker | None = None,
) -> ProactiveCheckOutcome:
    """The one function every trigger in this module funnels through:

    1. Resolve the `proactive_check` category's local-only chain (R39, by
       construction -- see `_local_only_execution_inputs()`). No local
       candidate available -> return a skipped outcome immediately,
       without ever calling `execute_chain()` or touching any remote
       candidate, and without raising.
    2. Otherwise call `delegate/chain.py`'s `execute_chain()` (U12)
       directly -- no Claude Code invocation, no MCP transport, matching
       U7's `route_bounded_request()` pattern.
    3. Classify the result via `severity_classifier` (default
       `default_severity_classifier`). A serious finding invokes
       `claude_code_invoker` (if one was given) for the full Claude Code
       loop (U4); a non-serious finding never does, and if no invoker was
       given at all, a serious finding is still reported as serious in the
       returned outcome but nothing is invoked (see module docstring on
       the commit-hook CLI's own honest handling of this case).

    Any exception `execute_chain()` itself raises (`NoCandidatesError`,
    `ChainExhaustedError`) propagates unchanged -- this function doesn't
    swallow or reinterpret those. `NoCandidatesError` should be unreachable
    in practice here specifically BECAUSE step 1 already guarantees at
    least one candidate before `execute_chain()` is ever called.

    `cooldown_store` (feat/delegate-candidate-circuit-breaker plan), like
    `queue`, is caller-supplied rather than internally constructed -- this
    function has no in-repo production caller today (confirmed by that
    plan's Scope Boundaries; production proactive checks run through
    `internal_api.py`'s HTTP route instead), so there is no daemon-shared
    instance to thread through here.
    """
    local_chains_config, local_registry = _local_only_execution_inputs(chains_config, registry)
    if local_chains_config is None:
        return ProactiveCheckOutcome(skipped_no_local_candidate=True)

    result = await execute_chain(
        PROACTIVE_CHECK_TASK_TYPE,
        task,
        scope,
        chains_config=local_chains_config,
        registry=local_registry,
        queue=queue,
        cooldown_store=cooldown_store,
        graph_engine=graph_engine,
        scope_root=scope_root,
        client_factory=client_factory,
        timeout=timeout,
        available_ram_gb=available_ram_gb,
    )

    serious = severity_classifier(result)
    claude_code_result: Any | None = None
    if serious and claude_code_invoker is not None:
        claude_code_result = claude_code_invoker(result)

    return ProactiveCheckOutcome(
        skipped_no_local_candidate=False,
        delegate_result=result,
        serious=serious,
        claude_code_result=claude_code_result,
    )


# R16: on-demand queries bypass the commit/save event triggers entirely.
# `run_proactive_check()` carries no hook/watcher state of its own, so an
# on-demand caller (a CLI command, a future editor "check now" action) uses
# it directly, any time -- `query_on_demand` is that same function, named
# for call sites that want the R16 framing explicit.
query_on_demand = run_proactive_check


# --- Debounced file-save watcher (R15, R17) -------------------------------


# What "schedule a one-shot callback after `delay` seconds" means --
# injectable so tests never depend on real wall-clock timers (see
# `_default_scheduler` for the production implementation). Returns a
# zero-argument "cancel" callable.
Scheduler = Callable[[float, Callable[[], None]], Callable[[], None]]


def _default_scheduler(delay: float, callback: Callable[[], None]) -> Callable[[], None]:
    """Production `Scheduler`: a single `threading.Timer`, started fresh
    each time a save event arrives (and cancelled/replaced by the next
    one) -- a one-shot, event-triggered delay, not a recurring/self-
    rescheduling loop, so it never becomes the kind of standing background
    process R17 rules out."""
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer.cancel


class DebouncedSaveWatcher:
    """Debounce policy for save events (R15): fires `on_fire` once no
    further `on_save()` call arrives within `debounce_seconds` of the last
    one.

    This class does no OS-level filesystem watching itself -- an external
    save-event source (an editor plugin, `watchdog`, ...) is expected to
    call `on_save()` once per raw save event it observes. Nothing here
    runs, or schedules anything, until `on_save()` is actually called
    (Covers R17's "no event ... means no scan" scenario: constructing a
    watcher and letting real time pass, with `on_save()` never called,
    fires nothing).
    """

    def __init__(
        self,
        debounce_seconds: float,
        on_fire: Callable[[], Any],
        *,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._debounce_seconds = debounce_seconds
        self._on_fire = on_fire
        self._scheduler = scheduler or _default_scheduler
        self._cancel_pending: Callable[[], None] | None = None

    def on_save(self) -> None:
        """Record a save event, resetting the debounce window. Cancels any
        still-pending timer from an earlier save so rapid consecutive
        saves collapse into a single check fired only after the last one
        goes quiet for `debounce_seconds`."""
        if self._cancel_pending is not None:
            self._cancel_pending()
        self._cancel_pending = self._scheduler(self._debounce_seconds, self._fire)

    def _fire(self) -> None:
        self._cancel_pending = None
        self._on_fire()

    @property
    def pending(self) -> bool:
        """Whether a debounce timer is currently outstanding (mainly for
        tests -- production callers don't need to poll this)."""
        return self._cancel_pending is not None


# --- Hook installation (`josu init`) --------------------------------------


POST_COMMIT_HOOK_NAME = "post-commit"

# Delimits josu's own chained invocation inside a possibly-pre-existing
# hook script, so re-running installation is idempotent (detected via
# `_JOSU_MARKER_START in existing`) rather than appending a second copy.
_JOSU_MARKER_START = "# >>> josu proactive check (managed by `josu init`, U9) >>>"
_JOSU_MARKER_END = "# <<< josu proactive check (managed by `josu init`, U9) <<<"

# A conservative allowlist of shell interpreters recognized as "a plain
# shell script josu can safely chain to" -- covers Husky's and the
# `pre-commit` framework's generated hooks, and a typical hand-written
# script. Anything else (no shebang, an unrecognized interpreter, binary
# content) is treated as unsafe to blindly append to.
_RECOGNIZED_SHELL_MARKERS = ("sh", "bash", "zsh")

# A bare `exec <command>` (not a plain output redirection like `exec >
# log`) replaces the current process -- anything appended after it in the
# script would silently never run. Chaining after a hook containing one
# would be unsafe, so it's treated the same as an unparseable hook.
_BARE_EXEC_PATTERN = re.compile(r"^\s*exec\s+(?!\d*[<>]|&)\S")


class HookInstallationError(Exception):
    """Raised when `install_commit_hook()` can't safely install or chain
    to an existing `post-commit` hook -- the conflict-can't-be-resolved
    case: the existing hook isn't a simple, parseable shell script josu
    can safely wrap (no recognized shebang, non-text/binary content, or a
    bare `exec` that would swallow anything appended after it). Aborts
    installation with this clear error rather than clobbering whatever
    tooling (Husky, `pre-commit`, a hand-written script) already owns that
    hook.
    """


@dataclass(frozen=True)
class HookInstallationResult:
    """What `install_commit_hook()` did: where the hook file lives, and
    whether it chained to a pre-existing hook (`chained_existing=True`) or
    an already-installed josu block was found and left as-is
    (`already_installed=True`, idempotent re-run)."""

    hook_path: Path
    chained_existing: bool
    already_installed: bool = False


def _looks_safely_chainable(existing_content: str) -> bool:
    """See `HookInstallationError`'s docstring for what's considered
    unsafe. `existing_content` must decode as text to even reach this
    function (a `UnicodeDecodeError` on read is handled by the caller)."""
    stripped = existing_content.lstrip()
    if not stripped.startswith("#!"):
        return False
    first_line = stripped.splitlines()[0]
    if not any(marker in first_line for marker in _RECOGNIZED_SHELL_MARKERS):
        return False
    return not any(_BARE_EXEC_PATTERN.match(line) for line in existing_content.splitlines())


def _josu_hook_block(python_executable: str) -> str:
    return (
        f"{_JOSU_MARKER_START}\n"
        f'"{python_executable}" -m josu.proactive.watchers\n'
        f"{_JOSU_MARKER_END}\n"
    )


def install_commit_hook(
    repo_root: Path,
    *,
    python_executable: str | None = None,
) -> HookInstallationResult:
    """Install (or chain to) `<repo_root>/.git/hooks/post-commit` (R15's
    commit trigger).

    - No existing hook: writes a fresh, executable shell script that
      invokes `python -m josu.proactive.watchers` (this module's own
      commit-hook entry point, see `main()` below).
    - An existing hook already carrying josu's own marker block: a no-op,
      returned as `already_installed=True` -- re-running `josu init` is
      idempotent, never duplicates the block.
    - Any other existing hook: detected and CHAINED to (Husky, `pre-commit`,
      or a hand-written script) by appending josu's block after the
      existing content, rather than overwriting it -- only if
      `_looks_safely_chainable()` confirms it's a plain, recognized shell
      script with no bare `exec` that would swallow the appended block.
      Otherwise raises `HookInstallationError` and writes nothing.

    Raises `HookInstallationError` if `repo_root` has no `.git` directory
    at all (nothing to install a hook into), or if the existing-hook
    conflict above can't be safely resolved.
    """
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        raise HookInstallationError(f"{repo_root} does not look like a git repository (no .git/ directory)")

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / POST_COMMIT_HOOK_NAME
    block = _josu_hook_block(python_executable or sys.executable)

    if not hook_path.exists():
        content = "#!/bin/sh\n" + block
        hook_path.write_text(content, encoding="utf-8")
        _make_executable(hook_path)
        return HookInstallationResult(hook_path=hook_path, chained_existing=False)

    try:
        existing = hook_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HookInstallationError(
            f"{hook_path} already exists but isn't readable as a text shell script ({exc}) -- "
            "refusing to overwrite it. Install josu's proactive check manually, or move the "
            "existing hook aside first."
        ) from exc

    if _JOSU_MARKER_START in existing:
        return HookInstallationResult(hook_path=hook_path, chained_existing=False, already_installed=True)

    if not _looks_safely_chainable(existing):
        raise HookInstallationError(
            f"{hook_path} already contains a post-commit hook that doesn't look like a plain "
            "shell script josu can safely chain to (missing/unrecognized shebang, a bare `exec` "
            "that would swallow anything appended after it, or non-text content). Refusing to "
            "overwrite it -- install josu's proactive check manually, or move the existing hook "
            "aside first."
        )

    chained_content = existing.rstrip("\n") + "\n\n" + block
    hook_path.write_text(chained_content, encoding="utf-8")
    _make_executable(hook_path)
    return HookInstallationResult(hook_path=hook_path, chained_existing=True)


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --- CLI entry point (`python -m josu.proactive.watchers`) ---------------
#
# The command line `install_commit_hook()` writes into the installed
# `post-commit` hook script. Kept deliberately thin: load the developer's
# real config, run one commit-triggered check, print a short report.
# Escalation to the full Claude Code loop is not wired from this bare
# git-hook subprocess in v1 -- building the worktree/MCP-manifest
# `ClaudeCodeEscalationConfig` needs (U4) is the daemon's job (it already
# owns worktree lifecycle and manifest generation for every other call
# path), not something a standalone per-commit subprocess should
# reconstruct on its own. A serious finding is still reported to the
# developer, honestly, rather than silently doing nothing about it.
#
# U14: this subprocess used to construct its own `DelegateQueue()` and call
# `run_proactive_check()` (-> `execute_chain()`) entirely in-process -- a
# SECOND, unshared queue racing against the daemon's own on every commit.
# It now calls the daemon's `/delegate/internal` endpoint instead
# (`delegate/internal_api.py`), mirroring `delegate/client.py`'s async
# `httpx` pattern. `run_proactive_check()` itself is UNCHANGED and still
# used as-is by every other caller (the debounced save watcher, on-demand
# queries, and every existing test) -- only this one subprocess entry
# point's transport changed, not the shared composition function.
#
# **R39 preservation**: `_local_only_execution_inputs()` is still called
# HERE, client-side, exactly where it always lived -- BEFORE the daemon is
# ever contacted. The already-resolved, already-local-only candidate list
# it produces is what gets sent (the `candidates` request shape in
# `delegate/internal_api.py`), never a bare `task_type="proactive_check"`
# string the daemon's real config (possibly `allow_remote=true`) would
# resolve generally. If no local candidate exists, this returns without
# ever contacting the daemon at all -- identical to the in-process
# behavior it replaces.


def _print_outcome(outcome: ProactiveCheckOutcome) -> None:
    if outcome.skipped_no_local_candidate:
        print("josu proactive check: skipped (no local delegate candidate configured)")
        return

    assert outcome.delegate_result is not None
    print(f"josu proactive check: {outcome.delegate_result.result}")
    if outcome.delegate_result.caveats:
        print(f"  caveats: {outcome.delegate_result.caveats}")
    if outcome.serious and outcome.claude_code_result is None:
        print(
            "  this finding looks serious; escalating to the full Claude Code loop is not "
            "wired from the git-hook context yet -- please review by hand."
        )


_COMMIT_HOOK_TASK_TEXT = "Re-evaluate the repository for issues introduced by the most recent commit."

# Simplify pass after U13/U14: a stuck daemon must not block `git commit`
# for the same ~2-minute latency budget a general delegate call gets
# (`daemon_client.post_delegate_internal()`'s own 120s default) -- this is a
# bounded, local-only, low-complexity check, meant to be fast (see module
# docstring). 20s is generous for that shape of call while still keeping
# `git commit` responsive if the daemon is wedged.
_COMMIT_HOOK_HTTP_TIMEOUT_SECONDS = 20.0


def _run_reindex_on_commit(base_url: str, token: str) -> None:
    """U10/R14 wiring (doc-review fix): the commit hook is the one
    production entry point `graph/index.py`'s `reindex_on_commit()`
    previously had zero callers for. Best-effort -- a reindex failure here
    must never fail the developer's `git commit` (it already degrades
    gracefully via `ReindexResult.engine_error`, see `graph/index.py`), and
    is reported, not raised. Uses the same trimmed
    `_COMMIT_HOOK_HTTP_TIMEOUT_SECONDS` budget the delegate call below
    already does -- a wedged daemon must not block `git commit` for the
    general-purpose 120s default either call would otherwise wait out.

    Note: `reindex_on_save()`'s own production wiring is a separate,
    pre-existing gap this fix does not close -- no editor-plugin/file-watcher
    save-event source exists anywhere in this codebase yet to call it from
    (see `DebouncedSaveWatcher`'s own docstring); there's no call site here
    to add.
    """
    import asyncio

    from josu.graph.index import ReindexError, reindex_on_commit

    try:
        result = asyncio.run(
            reindex_on_commit(
                Path.cwd(),
                base_url=base_url,
                token=token,
                timeout=_COMMIT_HOOK_HTTP_TIMEOUT_SECONDS,
            )
        )
    except ReindexError as exc:
        # Computing the changed-file set itself failed (e.g. a git error) --
        # must never fail the developer's `git commit` over this any more
        # than an engine-unavailable failure would (see `ReindexResult.
        # engine_error`'s handling below).
        print(f"josu proactive check: reindex skipped ({exc})")
        return
    if result.engine_error:
        print(f"josu proactive check: reindex skipped ({result.engine_error})")
    elif result.reindexed_files or result.pruned_files:
        print(
            f"josu proactive check: reindexed {len(result.reindexed_files)} file(s), "
            f"pruned {len(result.pruned_files)}"
        )


def _run_commit_hook_from_cli() -> int:
    import asyncio

    from josu.config import load_config, resolve_config_path
    from josu.daemon import DEFAULT_HOST, DEFAULT_PORT
    from josu.daemon_auth import resolve_daemon_token
    from josu.delegate.daemon_client import (
        DaemonNotReachableError,
        DelegateInternalError,
        post_delegate_internal,
    )

    config_path = resolve_config_path()
    config = load_config(config_path)
    registry = {candidate.name: candidate for candidate in config.delegate.candidates}
    base_url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    token = resolve_daemon_token(config_path)

    _run_reindex_on_commit(base_url, token)

    # R39: resolve the local-only projection HERE, client-side, before ever
    # contacting the daemon -- see the module docstring and this section's
    # own docstring above for why this ordering is load-bearing.
    local_chains_config, local_registry = _local_only_execution_inputs(
        config.chains, registry
    )
    if local_chains_config is None:
        _print_outcome(ProactiveCheckOutcome(skipped_no_local_candidate=True))
        return 0

    candidates = list(local_registry.values())

    try:
        payload = asyncio.run(
            post_delegate_internal(
                base_url,
                {
                    "task": _COMMIT_HOOK_TASK_TEXT,
                    # NAME strings only -- `internal_api.py`'s handler
                    # resolves each against the daemon's own trusted
                    # registry server-side; it never accepts a full
                    # candidate object (endpoint/api_key_env/local) from
                    # this request body (SSRF/credential-exfiltration fix).
                    "candidates": [candidate.name for candidate in candidates],
                },
                timeout=_COMMIT_HOOK_HTTP_TIMEOUT_SECONDS,
                token=token,
            )
        )
    except DaemonNotReachableError as exc:
        print(f"josu proactive check: {exc}")
        return 1
    except DelegateInternalError as exc:
        print(f"josu proactive check: {exc.error}: {exc.detail}")
        return 1

    result = DelegateResult(
        result=payload["result"], caveats=payload["caveats"], model=payload["model"]
    )
    serious = default_severity_classifier(result)
    outcome = ProactiveCheckOutcome(
        skipped_no_local_candidate=False, delegate_result=result, serious=serious
    )
    _print_outcome(outcome)
    return 0


def main(argv: list[str] | None = None) -> int:
    """`python -m josu.proactive.watchers` -- the command the hook script
    `install_commit_hook()` writes invokes on every commit."""
    return _run_commit_hook_from_cli()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
