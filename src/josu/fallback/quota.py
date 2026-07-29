"""Quota-exhaustion detection and the direct-to-delegate fallback path
(U7, R28, R29).

Claude Code invocations run through `orchestrator/adapters/claude_code.py`'s
`run()` (U4), which drives `orchestrator/adapter.py`'s `invoke_adapter()`
under the hood. That call returns an `InvocationResult` -- exit code plus
raw stdout/stderr -- BEFORE any of `run()`'s own wrapper-side validation
(MCP-server-connected check, git-allowlist check, ...) runs on top of it.
This module inspects that same raw invocation surface for a quota/rate-limit
-specific signal, distinguishing it from other failure modes (an auth
error, a network blip, a wrapper-side validation failure).

Per the plan's Key Technical Decisions, the EXACT error type/exit code
Claude Code uses to signal quota/rate-limit exhaustion is explicitly
deferred to implementation-time research against the CLI's actual error
surface -- there is no single documented exit-code table for it (see this
unit's research notes). So `default_quota_classifier()` below is a best-
effort heuristic built from what IS publicly documented/observed (a
non-zero exit code, plus stderr/stdout text containing phrases like "rate
limit", "usage limit", "N-hour limit reached" -- see
https://github.com/anthropics/claude-code/issues/27336 and Anthropic's own
support docs on the "5-hour limit reached" / "Usage limit reached"
messages), explicitly excluding known auth/network-failure phrasing so an
expired API key or a DNS blip is never misclassified as quota exhaustion.

The heuristic is never hard-coded into the call sites that use it --
`QuotaClassifier` is a plain `Callable[[ClaudeCodeFailureSignal], bool]`
type alias, and every function below accepts a `classifier` override, so
refining the heuristic later (or swapping in an adapter-specific one, once
real quota-exhaustion events have been observed) never requires touching
`route_bounded_request()`/`handle_invocation_failure()` themselves -- only
the classifier passed in.

Two independent things happen when quota exhaustion is detected:

- A developer-initiated, in-scope ("bounded") request -- naming a
  `task_type` from `config/chains.py`'s `DELEGABLE_TASK_TYPES` -- routes
  directly to `delegate/chain.py`'s `execute_chain()` (U12), skipping
  worktree creation, the Claude Code invocation, and the MCP transport
  entirely (`route_bounded_request()`). The developer is told explicitly,
  via `HOSTED_PAUSED_NOTICE`, that hosted-level work is paused until quota
  resets (R29). A request naming a `task_type` OUTSIDE that category
  (`HOSTED_ONLY_TASK_TYPES`, or anything else) raises `TaskNotBoundedError`
  rather than being attempted locally or auto-decomposed into smaller
  bounded pieces -- the caller is told to wait instead.
- If quota exhaustion was detected mid-run (a worktree already exists,
  created for a task whose Claude Code invocation never reached approval
  or rejection), that worktree is classified as abandoned
  (`handle_invocation_failure()` / `mark_worktree_abandoned()`) rather than
  left in limbo. `worktree.py` itself has no persisted-state mechanism yet
  (U8, not built as of this unit) to hook into, so this writes a small JSON
  marker file per abandoned worktree under `<repo_root>/.josu/abandoned-
  worktrees/<worktree-name>.json` -- the same project-local `.josu/`
  convention `observability/runlog.py` and `cli.py`'s
  `_default_graph_out_dir()` already use, and a directory U8's future
  `josu cleanup` command can simply list to discover every abandoned
  worktree, without this module needing to know anything about how that
  command will eventually be built.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from josu.config.chains import DELEGABLE_TASK_TYPES, ChainsConfig
from josu.config.delegate import DelegateCandidate
from josu.delegate.chain import ClientFactory, execute_chain
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS, DelegateResult
from josu.delegate.queue import DelegateQueue
from josu.graph.engine import GraphEngine
from josu.orchestrator.adapter import InvocationResult
from josu.orchestrator.worktree import Worktree

# Project-local, mirroring `observability/runlog.py`'s
# `<project_root>/.josu/runlog/` convention -- abandoned-worktree markers
# are project history, not global developer config, so they live under the
# project root rather than the XDG-style location `config/__init__.py`
# resolves `josu.toml` to.
ABANDONED_WORKTREES_SUBDIR = Path(".josu") / "abandoned-worktrees"

# R29: the explicit notice a developer sees when a bounded request is
# routed through this fallback path instead of the hosted orchestrator.
HOSTED_PAUSED_NOTICE = (
    "Claude Code appears to be quota/rate-limit exhausted. Hosted-level "
    "orchestrator work is paused until quota resets; this bounded request "
    "was routed directly to the local delegate worker instead."
)

# The reason string recorded on a worktree marker when quota exhaustion is
# detected mid-run (as opposed to any other future abandonment reason a
# later unit might record via the same marker shape).
ABANDON_REASON_QUOTA_EXHAUSTION = "quota_exhaustion_detected_mid_run"


# --- Failure signal + pluggable classifier ---------------------------------


@dataclass(frozen=True)
class ClaudeCodeFailureSignal:
    """A minimal, adapter-agnostic view of one Claude Code invocation's
    outcome -- just the exit code and raw stdout/stderr text, the same
    fields `orchestrator/adapter.py`'s `InvocationResult` carries before any
    higher-level parsing runs on top of it. Kept as its own tiny type
    (rather than passing `InvocationResult` straight into the classifier)
    so a classifier can be written and unit-tested without importing
    anything from `orchestrator/adapter.py` at all.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def combined_text(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


def failure_signal_from_invocation(invocation: InvocationResult) -> ClaudeCodeFailureSignal:
    """Adapt `orchestrator/adapter.py`'s `InvocationResult` (U4) into the
    smaller `ClaudeCodeFailureSignal` shape this module's classifiers work
    over."""
    return ClaudeCodeFailureSignal(
        returncode=invocation.returncode,
        stdout=invocation.stdout,
        stderr=invocation.stderr,
    )


# A classifier is any callable from a failure signal to "this was quota
# exhaustion" -- the pluggable seam the module docstring describes. Nothing
# below ever calls `default_quota_classifier` directly except as this type's
# default value, so a caller can swap it out entirely (e.g. once real
# quota-exhaustion events reveal a more precise signal) without editing
# `route_bounded_request()`/`handle_invocation_failure()`.
QuotaClassifier = Callable[[ClaudeCodeFailureSignal], bool]


# Phrases observed in Claude Code's own quota/rate-limit messaging (see
# module docstring for sources): "API Error: Rate limit reached", "5-hour
# limit reached", "Usage limit reached", "quota exceeded/exhausted".
_QUOTA_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"rate limit",
        r"usage limit",
        r"\d+-hour limit",
        r"quota (?:exceeded|exhausted)",
        r"limit reached",
    )
)

# Phrases that mean "this failure is something else" -- an expired/invalid
# credential or a network-level failure -- checked first so a coincidental
# overlap in wording never misclassifies either of those as quota
# exhaustion.
_NON_QUOTA_EXCLUSION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"invalid api key",
        r"unauthorized",
        r"authentication (?:failed|error)",
        r"forbidden",
        r"connection (?:refused|reset|error)",
        r"network (?:error|unreachable)",
        r"could not resolve host",
        r"timed? ?out",
    )
)


def default_quota_classifier(signal: ClaudeCodeFailureSignal) -> bool:
    """Best-effort default heuristic (see module docstring for why this is
    a heuristic, not a confirmed exit-code contract): a non-zero exit code
    whose combined stdout/stderr text matches a known quota/rate-limit
    phrase and does NOT match a known auth/network-failure phrase.

    A clean (zero) exit code is never classified as quota exhaustion,
    regardless of text content -- quota exhaustion is, definitionally, a
    failed invocation.
    """
    if signal.returncode == 0:
        return False

    text = signal.combined_text
    if any(pattern.search(text) for pattern in _NON_QUOTA_EXCLUSION_PATTERNS):
        return False

    return any(pattern.search(text) for pattern in _QUOTA_PATTERNS)


def is_quota_exhaustion(
    invocation_or_signal: InvocationResult | ClaudeCodeFailureSignal,
    *,
    classifier: QuotaClassifier = default_quota_classifier,
) -> bool:
    """Run `classifier` (default `default_quota_classifier`) against a
    failure signal, accepting either the raw `InvocationResult` or an
    already-adapted `ClaudeCodeFailureSignal`."""
    signal = (
        invocation_or_signal
        if isinstance(invocation_or_signal, ClaudeCodeFailureSignal)
        else failure_signal_from_invocation(invocation_or_signal)
    )
    return classifier(signal)


# --- Worktree abandonment marking -------------------------------------------


@dataclass(frozen=True)
class AbandonedWorktreeRecord:
    """One worktree classified as abandoned -- the JSON shape persisted to
    (and read back from) a marker file under `ABANDONED_WORKTREES_SUBDIR`.
    Deliberately plain strings/primitives only, mirroring
    `observability/runlog.py`'s records -- easy for a future `josu cleanup`
    (U8) to load with nothing more than `json.loads()`."""

    worktree_path: str
    branch: str
    repo_root: str
    reason: str
    detected_at: str


def default_abandoned_worktrees_dir(repo_root: Path) -> Path:
    """`<repo_root>/.josu/abandoned-worktrees/` -- see module docstring."""
    return repo_root / ABANDONED_WORKTREES_SUBDIR


def mark_worktree_abandoned(
    worktree: Worktree,
    *,
    reason: str,
    abandoned_dir: Path | None = None,
) -> Path:
    """Classify `worktree` as abandoned by writing a JSON marker file for
    it, named after the worktree's own directory name so re-marking the
    same worktree overwrites rather than accumulates duplicate records.

    Returns the marker file's path. Never touches `worktree.py` itself or
    the worktree's own contents/git state -- the marker lives entirely
    alongside it, in `abandoned_dir` (defaults to
    `default_abandoned_worktrees_dir(worktree.repo_root)`).
    """
    resolved_dir = abandoned_dir or default_abandoned_worktrees_dir(worktree.repo_root)
    resolved_dir.mkdir(parents=True, exist_ok=True)

    record = AbandonedWorktreeRecord(
        worktree_path=str(worktree.path),
        branch=worktree.branch,
        repo_root=str(worktree.repo_root),
        reason=reason,
        detected_at=datetime.now(timezone.utc).isoformat(),
    )
    marker_path = resolved_dir / f"{worktree.path.name}.json"
    marker_path.write_text(json.dumps(asdict(record), indent=2))
    return marker_path


def list_abandoned_worktrees(abandoned_dir: Path) -> list[AbandonedWorktreeRecord]:
    """Read back every marker file in `abandoned_dir` -- the discovery side
    a future `josu cleanup` (U8) would call. A missing directory (nothing
    ever marked abandoned) returns an empty list rather than raising."""
    if not abandoned_dir.exists():
        return []

    records: list[AbandonedWorktreeRecord] = []
    for marker_path in sorted(abandoned_dir.glob("*.json")):
        data = json.loads(marker_path.read_text())
        records.append(AbandonedWorktreeRecord(**data))
    return records


@dataclass(frozen=True)
class QuotaDetectionOutcome:
    """What came of inspecting one Claude Code invocation failure: whether
    it was classified as quota exhaustion, and -- only if it was, and a
    worktree was passed in -- where that worktree's abandonment marker was
    written."""

    quota_exhausted: bool
    abandoned_marker_path: Path | None = None


def handle_invocation_failure(
    invocation: InvocationResult,
    *,
    worktree: Worktree | None = None,
    classifier: QuotaClassifier = default_quota_classifier,
    abandoned_dir: Path | None = None,
) -> QuotaDetectionOutcome:
    """The mid-run detection path: given a failed Claude Code invocation's
    raw result (and, if this run created one, the worktree it was running
    in), classify the failure and -- only for a quota-exhaustion
    classification with a worktree present -- mark that worktree abandoned.

    A non-quota failure (auth error, network blip, ...) never touches the
    worktree at all here; whatever ordinary failure-handling the caller
    already does for a failed run applies unchanged.
    """
    quota_exhausted = is_quota_exhaustion(invocation, classifier=classifier)

    marker_path: Path | None = None
    if quota_exhausted and worktree is not None:
        marker_path = mark_worktree_abandoned(
            worktree,
            reason=ABANDON_REASON_QUOTA_EXHAUSTION,
            abandoned_dir=abandoned_dir,
        )

    return QuotaDetectionOutcome(quota_exhausted=quota_exhausted, abandoned_marker_path=marker_path)


# --- Direct-to-delegate bounded-request routing -----------------------------


class TaskNotBoundedError(Exception):
    """Raised when a request made during a detected quota-exhaustion outage
    names a `task_type` outside `config/chains.py`'s `DELEGABLE_TASK_TYPES`
    (R29): complex/non-bounded work is never auto-decomposed or attempted
    locally during the outage -- the caller is told to wait for hosted
    capacity to return instead of anything being attempted on its behalf.
    """

    def __init__(self, task_type: str) -> None:
        self.task_type = task_type
        super().__init__(
            f"task_type {task_type!r} is not a bounded delegable category "
            f"(one of {sorted(DELEGABLE_TASK_TYPES)}). Hosted-level orchestrator "
            "work is paused during quota exhaustion, and this request is not a "
            "bounded task the local delegate worker can safely attempt alone -- "
            "wait for quota to reset rather than auto-decomposing it locally."
        )


@dataclass(frozen=True)
class QuotaFallbackResult:
    """The outcome of a bounded request routed through this fallback path:
    the delegate worker's own result, plus the explicit R29 notice a
    developer-facing caller (the CLI) should surface alongside it."""

    delegate_result: DelegateResult
    message: str


async def route_bounded_request(
    task_type: str,
    task: Any,
    scope: Any = None,
    *,
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
    queue: DelegateQueue,
    graph_engine: GraphEngine | None = None,
    scope_root: Path | None = None,
    client_factory: ClientFactory | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    available_ram_gb: float | None = None,
) -> QuotaFallbackResult:
    """Route a developer-initiated `task_type` directly to
    `delegate/chain.py`'s `execute_chain()` (U12) -- no worktree creation,
    no Claude Code invocation, no MCP transport anywhere in this call path.

    Raises `TaskNotBoundedError` immediately, before `execute_chain` is
    ever called, if `task_type` isn't one of `DELEGABLE_TASK_TYPES` --
    matching the plan's "a complex non-bounded request during the outage
    ... waits rather than being attempted locally" test scenario. Any
    exception `execute_chain` itself raises (`NoCandidatesError`,
    `ChainExhaustedError`) propagates unchanged; this function doesn't
    swallow or reinterpret those.
    """
    if task_type not in DELEGABLE_TASK_TYPES:
        raise TaskNotBoundedError(task_type)

    result = await execute_chain(
        task_type,
        task,
        scope,
        chains_config=chains_config,
        registry=registry,
        queue=queue,
        graph_engine=graph_engine,
        scope_root=scope_root,
        client_factory=client_factory,
        timeout=timeout,
        available_ram_gb=available_ram_gb,
    )
    return QuotaFallbackResult(delegate_result=result, message=HOSTED_PAUSED_NOTICE)
