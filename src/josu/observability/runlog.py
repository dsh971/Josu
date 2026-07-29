"""Local, developer-inspectable run log (U6, R25).

One structured record per task run, capturing:

- graph queries (`GraphQueryEvent`) -- each `search`/`execute` call (R12)
  made against `graph/engine.py` during the run;
- delegation calls (`DelegationEvent`) -- what was delegated, its caveats,
  timing, which chain candidate (`delegate/chain.py`'s `execute_chain`,
  U12) ultimately served it, and which candidates were tried and skipped en
  route (`chain.py`'s `SkipRecord` list); a fully-exhausted chain
  (`chain.py`'s `ChainExhaustedError`) is recorded via
  `DELEGATION_OUTCOME_CHAIN_EXHAUSTED`, a distinguishable "delegation
  exhausted, orchestrator completed directly" outcome -- never folded into
  a generic failure, per the plan's Key Technical Decisions;
- circuit-breaker events (`CircuitBreakerEvent`) -- `orchestrator/
  circuit_breaker.py`'s `CircuitBreakerTimeoutError` (U5), recording which
  limit tripped (the configured `timeout_seconds`) and by how much
  (`elapsed_seconds`).

Records store delegation *metadata* only -- candidate name, chain outcome,
timing, caveats, a sanitized error *class name* -- and NEVER raw HTTP
headers, raw upstream error bodies, or full verbatim request/response
payloads. This module's conversion helpers (`SkipEntry.from_skip_record`,
`DelegationEvent.from_chain_exhausted`) enforce that boundary themselves:
they only ever read `SkipRecord.candidate`/`.error`/`.retry_after` (already
sanitized to a class name and a non-sensitive `Retry-After` value by
`chain.py`/`client.py`) and never reach for an exception's `str()`, message,
or arbitrary instance attributes -- so even a hypothetical future exception
that (incorrectly) attached raw response details would not leak them into
the log. A verbose, full-payload log level is a deferred, explicit opt-in
(see plan Scope Boundaries), not default behavior here.

No hosted telemetry. Records live under the developer's PROJECT directory
(`default_runlog_dir()` -> `<project_root>/.josu/runlog/`), not the
XDG-style, outside-the-tree location `config/__init__.py` uses for
`josu.toml` -- a run log is project-specific history, unlike global
developer config, and follows the same `.josu/` convention `cli.py`'s
`_default_graph_out_dir()` already established for the graph.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from josu.delegate.chain import ChainExhaustedError, SkipRecord
from josu.delegate.local_model import DelegateResult
from josu.orchestrator.circuit_breaker import CircuitBreakerTimeoutError

DEFAULT_RUNLOG_SUBDIR = Path(".josu") / "runlog"

# A delegation call was served by some candidate in its chain (possibly
# after skipping earlier candidates -- see `DelegationEvent.skipped`).
DELEGATION_OUTCOME_SERVED = "served"

# Every candidate in the resolved chain failed (`ChainExhaustedError`,
# U12/R34) -- the orchestrator falls back to completing the sub-task
# itself. Deliberately its own distinguishable string, not folded into a
# generic "failed" outcome, per the plan's Key Technical Decisions.
DELEGATION_OUTCOME_CHAIN_EXHAUSTED = "delegation_exhausted_orchestrator_completed_directly"

# Guards against the task-summary field itself becoming a de facto
# full-payload dump -- this stores what was delegated (a short description
# of the sub-task), not the request/response body.
_MAX_TASK_SUMMARY_CHARS = 500


def default_runlog_dir(project_root: Path) -> Path:
    """`<project_root>/.josu/runlog/` -- see module docstring for why this
    is project-local rather than the XDG-style location `josu.toml` uses."""
    return project_root / DEFAULT_RUNLOG_SUBDIR


def new_run_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, limit: int = _MAX_TASK_SUMMARY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass(frozen=True)
class GraphQueryEvent:
    """One graph interaction (`search`/`execute`, R12) made during a run."""

    operation: str
    query: str
    result_count: int
    timestamp: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class SkipEntry:
    """One chain candidate tried and skipped en route to the one that
    ultimately served the call (or to full chain exhaustion). Mirrors
    `delegate/chain.py`'s `SkipRecord` field-for-field -- `error` there is
    already `type(exc).__name__`, never `str(exc)`, so this type never
    carries more than a candidate name, a sanitized error class name, and
    (for rate limits) the non-sensitive `Retry-After` value.
    """

    candidate: str
    error_class: str
    retry_after: str | None = None

    @classmethod
    def from_skip_record(cls, record: SkipRecord) -> SkipEntry:
        return cls(candidate=record.candidate, error_class=record.error, retry_after=record.retry_after)


@dataclass(frozen=True)
class DelegationEvent:
    """One delegation call: what was delegated, its caveats, timing, which
    candidate ultimately served it (or `None` if the chain was exhausted),
    and which candidates were skipped en route.
    """

    task_type: str
    task_summary: str
    outcome: str
    candidate: str | None
    caveats: str | None
    duration_seconds: float
    cost: float | None = None
    skipped: list[SkipEntry] = field(default_factory=list)
    timestamp: str = field(default_factory=_now_iso)

    @classmethod
    def from_result(
        cls,
        *,
        task_type: str,
        task_summary: str,
        result: DelegateResult,
        candidate: str,
        duration_seconds: float,
        skipped: Sequence[SkipRecord] = (),
        cost: float | None = None,
    ) -> DelegationEvent:
        """A chain call that succeeded -- `result` is U11/U4's
        `{result, caveats}` shape (`local_model.py`'s `DelegateResult`);
        only `caveats` and `model`-derived `candidate` are captured, never
        `result` itself (the sub-task's actual output isn't log metadata)."""
        return cls(
            task_type=task_type,
            task_summary=_truncate(str(task_summary)),
            outcome=DELEGATION_OUTCOME_SERVED,
            candidate=candidate,
            caveats=result.caveats or None,
            duration_seconds=duration_seconds,
            cost=cost,
            skipped=[SkipEntry.from_skip_record(r) for r in skipped],
        )

    @classmethod
    def from_chain_exhausted(
        cls,
        *,
        task_type: str,
        task_summary: str,
        error: ChainExhaustedError,
        duration_seconds: float,
    ) -> DelegationEvent:
        """Every candidate in the chain failed -- recorded under
        `DELEGATION_OUTCOME_CHAIN_EXHAUSTED`, never a generic failure
        outcome. Only `error.skip_records` (already-sanitized `SkipRecord`s)
        is read; `error.last_error` -- the raw underlying exception -- is
        deliberately never touched here, so nothing it might carry (now or
        in some future exception subtype) can reach the log.
        """
        return cls(
            task_type=task_type,
            task_summary=_truncate(str(task_summary)),
            outcome=DELEGATION_OUTCOME_CHAIN_EXHAUSTED,
            candidate=None,
            caveats=None,
            duration_seconds=duration_seconds,
            skipped=[SkipEntry.from_skip_record(r) for r in error.skip_records],
        )


@dataclass(frozen=True)
class CircuitBreakerEvent:
    """One circuit-breaker trip -- which limit tripped (`timeout_seconds`)
    and by how much (`elapsed_seconds`), per `orchestrator/circuit_breaker.py`'s
    `CircuitBreakerTimeoutError` (U5)."""

    kind: str
    timeout_seconds: float
    elapsed_seconds: float
    timestamp: str = field(default_factory=_now_iso)

    @classmethod
    def from_timeout_error(cls, error: CircuitBreakerTimeoutError) -> CircuitBreakerEvent:
        return cls(
            kind="wall_clock_timeout",
            timeout_seconds=error.timeout_seconds,
            elapsed_seconds=error.elapsed_seconds,
        )


@dataclass
class RunRecord:
    """One task run's full record. Mutable (unlike the event types above)
    so a caller can accumulate events over the course of a run before
    persisting via `save_run()`.
    """

    run_id: str
    started_at: str = field(default_factory=_now_iso)
    task_description: str | None = None
    graph_queries: list[GraphQueryEvent] = field(default_factory=list)
    delegations: list[DelegationEvent] = field(default_factory=list)
    circuit_breaker_events: list[CircuitBreakerEvent] = field(default_factory=list)

    def record_graph_query(self, operation: str, query: str, result_count: int) -> GraphQueryEvent:
        event = GraphQueryEvent(operation=operation, query=_truncate(query), result_count=result_count)
        self.graph_queries.append(event)
        return event

    def record_delegation(self, event: DelegationEvent) -> None:
        self.delegations.append(event)

    def record_circuit_breaker_event(self, event: CircuitBreakerEvent) -> None:
        self.circuit_breaker_events.append(event)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        delegations: list[DelegationEvent] = []
        for raw in data.get("delegations", []):
            raw = dict(raw)
            raw["skipped"] = [SkipEntry(**s) for s in raw.get("skipped", [])]
            delegations.append(DelegationEvent(**raw))

        return cls(
            run_id=data["run_id"],
            started_at=data.get("started_at", _now_iso()),
            task_description=data.get("task_description"),
            graph_queries=[GraphQueryEvent(**g) for g in data.get("graph_queries", [])],
            delegations=delegations,
            circuit_breaker_events=[
                CircuitBreakerEvent(**c) for c in data.get("circuit_breaker_events", [])
            ],
        )


class RunNotFoundError(Exception):
    """No run-log record named `run_id` exists under `runlog_dir`."""

    def __init__(self, run_id: str, runlog_dir: Path) -> None:
        self.run_id = run_id
        self.runlog_dir = runlog_dir
        super().__init__(f"no run log entry {run_id!r} found under {runlog_dir}")


def _run_path(runlog_dir: Path, run_id: str) -> Path:
    return runlog_dir / f"{run_id}.json"


def save_run(record: RunRecord, runlog_dir: Path) -> Path:
    """Persist `record` as `<runlog_dir>/<run_id>.json`, creating
    `runlog_dir` if needed. Overwrites any existing record with the same
    `run_id` -- callers re-save the same record as it accumulates events
    over a run."""
    runlog_dir.mkdir(parents=True, exist_ok=True)
    path = _run_path(runlog_dir, record.run_id)
    path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    return path


def load_run(run_id: str, runlog_dir: Path) -> RunRecord:
    path = _run_path(runlog_dir, run_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RunNotFoundError(run_id, runlog_dir) from exc
    return RunRecord.from_dict(json.loads(raw))


def list_runs(runlog_dir: Path) -> list[str]:
    """All run ids with a record under `runlog_dir`, in no particular
    order. Returns an empty list if the directory doesn't exist yet (no
    runs have been logged)."""
    if not runlog_dir.exists():
        return []
    return [p.stem for p in runlog_dir.glob("*.json")]


def latest_run_id(runlog_dir: Path) -> str | None:
    """The most recently started run under `runlog_dir` (by `started_at`),
    or `None` if there are none -- used by `josu log` when no run id is
    given."""
    run_ids = list_runs(runlog_dir)
    if not run_ids:
        return None
    records = [load_run(run_id, runlog_dir) for run_id in run_ids]
    records.sort(key=lambda r: r.started_at)
    return records[-1].run_id


def render_run(record: RunRecord) -> str:
    """Render `record` as human-readable text for `josu log` -- enough
    detail to explain what happened in the run (U6 Verification)."""
    lines = [f"Run {record.run_id} (started {record.started_at})"]
    if record.task_description:
        lines.append(f"  task: {record.task_description}")

    if not record.graph_queries and not record.delegations and not record.circuit_breaker_events:
        lines.append("  (no graph queries, delegations, or circuit-breaker events recorded)")
        return "\n".join(lines)

    if record.graph_queries:
        lines.append(f"  graph queries ({len(record.graph_queries)}):")
        for q in record.graph_queries:
            lines.append(f"    - [{q.timestamp}] {q.operation}({q.query!r}) -> {q.result_count} result(s)")
    else:
        lines.append("  graph queries: none")

    if record.delegations:
        lines.append(f"  delegations ({len(record.delegations)}):")
        for d in record.delegations:
            if d.outcome == DELEGATION_OUTCOME_CHAIN_EXHAUSTED:
                outcome_label = "delegation exhausted, orchestrator completed directly"
            elif d.outcome == DELEGATION_OUTCOME_SERVED:
                outcome_label = f"served by {d.candidate!r}"
            else:
                outcome_label = d.outcome
            lines.append(
                f"    - [{d.timestamp}] {d.task_type}: {d.task_summary!r} -- "
                f"{outcome_label} ({d.duration_seconds:.2f}s)"
            )
            if d.caveats:
                lines.append(f"        caveats: {d.caveats}")
            if d.cost is not None:
                lines.append(f"        cost: {d.cost}")
            for s in d.skipped:
                retry = f", retry-after={s.retry_after}" if s.retry_after else ""
                lines.append(f"        skipped: {s.candidate} ({s.error_class}{retry})")
    else:
        lines.append("  delegations: none")

    if record.circuit_breaker_events:
        lines.append(f"  circuit-breaker events ({len(record.circuit_breaker_events)}):")
        for c in record.circuit_breaker_events:
            lines.append(
                f"    - [{c.timestamp}] {c.kind} tripped: "
                f"{c.elapsed_seconds:.2f}s elapsed > {c.timeout_seconds:.2f}s limit"
            )
    else:
        lines.append("  circuit-breaker events: none")

    return "\n".join(lines)
