"""Tests for the local run log (U6, R25).

Local file I/O only -- no mocks, matching `test_config.py`'s `tmp_path`-based
conventions. Exercises `runlog.py`'s own API directly (no live orchestrator
loop exists yet to wire this into), using real types from `delegate/chain.py`
(U12) and `orchestrator/circuit_breaker.py` (U5) as the actual future
integration points will.
"""

from __future__ import annotations

import pytest

from josu.cli import build_parser
from josu.delegate.chain import ChainExhaustedError, SkipRecord
from josu.delegate.client import DelegateAPIError, DelegateRateLimitedError
from josu.delegate.local_model import DelegateResult
from josu.observability.runlog import (
    DELEGATION_OUTCOME_CHAIN_EXHAUSTED,
    DELEGATION_OUTCOME_SERVED,
    CircuitBreakerEvent,
    DelegationEvent,
    RunNotFoundError,
    RunRecord,
    default_runlog_dir,
    latest_run_id,
    list_runs,
    load_run,
    render_run,
    save_run,
)
from josu.orchestrator.circuit_breaker import CircuitBreakerTimeoutError


def test_default_runlog_dir_is_inside_the_project_tree(tmp_path):
    # Project-specific and inside the project, unlike josu.toml's XDG-style,
    # outside-the-tree location (config/__init__.py) -- see plan Key
    # Technical Decisions on where local state of each kind belongs.
    resolved = default_runlog_dir(tmp_path)
    assert resolved == tmp_path / ".josu" / "runlog"
    assert tmp_path in resolved.parents


def test_delegating_task_produces_entry_with_caveats_and_chain_outcome(tmp_path):
    result = DelegateResult(
        result="renamed 3 functions", caveats="unsure whether callers outside scope were updated",
        model="qwen2.5-coder:7b",
    )
    skipped = [SkipRecord(candidate="flaky-remote", error="DelegateUnreachableError")]
    event = DelegationEvent.from_result(
        task_type="refactor_rename",
        task_summary="rename helper functions in src/foo.py",
        result=result,
        candidate="local-ollama",
        duration_seconds=4.2,
        skipped=skipped,
    )

    record = RunRecord(run_id="run-delegates", started_at="2026-07-25T10:00:00+00:00")
    record.record_delegation(event)
    path = save_run(record, tmp_path)
    assert path == tmp_path / "run-delegates.json"

    loaded = load_run("run-delegates", tmp_path)
    assert len(loaded.delegations) == 1
    logged = loaded.delegations[0]
    assert logged.outcome == DELEGATION_OUTCOME_SERVED
    assert logged.candidate == "local-ollama"
    assert logged.caveats == "unsure whether callers outside scope were updated"
    assert logged.duration_seconds == 4.2
    assert len(logged.skipped) == 1
    assert logged.skipped[0].candidate == "flaky-remote"
    assert logged.skipped[0].error_class == "DelegateUnreachableError"

    rendered = render_run(loaded)
    assert "rename helper functions in src/foo.py" in rendered
    assert "served by 'local-ollama'" in rendered
    assert "unsure whether callers outside scope were updated" in rendered
    assert "flaky-remote" in rendered
    assert "DelegateUnreachableError" in rendered


def test_non_delegating_task_still_produces_an_entry(tmp_path):
    record = RunRecord(run_id="run-no-delegation", task_description="add a docstring to utils.py")
    record.record_graph_query("search", "utils.py", result_count=2)
    save_run(record, tmp_path)

    assert "run-no-delegation" in list_runs(tmp_path)
    loaded = load_run("run-no-delegation", tmp_path)
    assert loaded.delegations == []
    assert len(loaded.graph_queries) == 1
    assert loaded.graph_queries[0].operation == "search"

    rendered = render_run(loaded)
    assert "run-no-delegation" in rendered
    assert "delegations: none" in rendered


def test_chain_exhausted_is_a_distinguishable_outcome_not_generic_failure(tmp_path):
    last_error = DelegateAPIError("remote-fallback", status_code=500)
    skip_records = [
        SkipRecord(candidate="local-ollama", error="DelegateUnreachableError"),
        SkipRecord(candidate="remote-fallback", error="DelegateAPIError"),
    ]
    chain_error = ChainExhaustedError(
        task_type="file_summarization",
        attempted=["local-ollama", "remote-fallback"],
        skip_records=skip_records,
        last_error=last_error,
    )
    event = DelegationEvent.from_chain_exhausted(
        task_type="file_summarization",
        task_summary="summarize src/big_module.py",
        error=chain_error,
        duration_seconds=9.9,
    )
    assert event.outcome == DELEGATION_OUTCOME_CHAIN_EXHAUSTED
    assert event.outcome != "failed"
    assert event.candidate is None

    record = RunRecord(run_id="run-exhausted")
    record.record_delegation(event)
    save_run(record, tmp_path)

    loaded = load_run("run-exhausted", tmp_path)
    assert loaded.delegations[0].outcome == DELEGATION_OUTCOME_CHAIN_EXHAUSTED
    rendered = render_run(loaded)
    assert "delegation exhausted, orchestrator completed directly" in rendered
    assert "local-ollama" in rendered
    assert "remote-fallback" in rendered


def test_circuit_breaker_trip_shows_which_limit_tripped_and_why(tmp_path):
    error = CircuitBreakerTimeoutError(timeout_seconds=1200.0, elapsed_seconds=1215.7)
    cb_event = CircuitBreakerEvent.from_timeout_error(error)
    assert cb_event.kind == "wall_clock_timeout"
    assert cb_event.timeout_seconds == 1200.0
    assert cb_event.elapsed_seconds == 1215.7

    record = RunRecord(run_id="run-circuit-breaker")
    record.record_circuit_breaker_event(cb_event)
    save_run(record, tmp_path)

    loaded = load_run("run-circuit-breaker", tmp_path)
    assert len(loaded.circuit_breaker_events) == 1
    logged = loaded.circuit_breaker_events[0]
    assert logged.kind == "wall_clock_timeout"
    assert logged.timeout_seconds == 1200.0
    assert logged.elapsed_seconds == 1215.7

    rendered = render_run(loaded)
    assert "wall_clock_timeout tripped" in rendered
    assert "1215.70s elapsed > 1200.00s limit" in rendered


def test_sanitized_error_excludes_raw_http_headers_and_upstream_body(tmp_path):
    """Even if a delegate exception instance carried raw response details
    as extra attributes (it shouldn't, by `client.py`'s design, but this
    proves `runlog`'s own conversion path is a genuine sanitization
    boundary rather than an accidental inheritance of that discipline),
    none of that content reaches the persisted, serialized record -- only
    the sanitized error class name and candidate name do."""
    raw_headers = {"Authorization": "Bearer sk-live-abcdef123456", "X-Request-Id": "req-999"}
    raw_body = '{"error": {"message": "invalid_api_key sk-live-abcdef123456 rejected"}}'

    api_error = DelegateAPIError("remote-candidate", status_code=401)
    # Simulate a hypothetical misbehaving client that attached raw upstream
    # details onto the exception instance.
    api_error.raw_response_headers = raw_headers  # type: ignore[attr-defined]
    api_error.raw_response_body = raw_body  # type: ignore[attr-defined]

    rate_limited = DelegateRateLimitedError("remote-candidate-2", retry_after="30")
    rate_limited.raw_response_headers = raw_headers  # type: ignore[attr-defined]
    rate_limited.raw_response_body = raw_body  # type: ignore[attr-defined]

    chain_error = ChainExhaustedError(
        task_type="file_summarization",
        attempted=["remote-candidate", "remote-candidate-2"],
        skip_records=[
            SkipRecord(candidate="remote-candidate", error=type(api_error).__name__),
            SkipRecord(
                candidate="remote-candidate-2",
                error=type(rate_limited).__name__,
                retry_after=rate_limited.retry_after,
            ),
        ],
        last_error=rate_limited,
    )
    event = DelegationEvent.from_chain_exhausted(
        task_type="file_summarization",
        task_summary="summarize src/secrets_module.py",
        error=chain_error,
        duration_seconds=2.5,
    )

    record = RunRecord(run_id="run-sanitized")
    record.record_delegation(event)
    path = save_run(record, tmp_path)
    serialized = path.read_text(encoding="utf-8")

    # None of the raw, sensitive upstream content is present anywhere in
    # the persisted record.
    assert "sk-live-abcdef123456" not in serialized
    assert "Authorization" not in serialized
    assert "Bearer" not in serialized
    assert "X-Request-Id" not in serialized
    assert "req-999" not in serialized
    assert raw_body not in serialized
    assert "invalid_api_key" not in serialized

    # Only the sanitized error class name, candidate name, and the
    # non-sensitive retry-after value are present.
    assert "DelegateAPIError" in serialized
    assert "DelegateRateLimitedError" in serialized
    assert "remote-candidate" in serialized
    assert "remote-candidate-2" in serialized
    assert '"retry_after": "30"' in serialized


def test_latest_run_id_returns_most_recently_started_run(tmp_path):
    older = RunRecord(run_id="run-older", started_at="2026-07-20T00:00:00+00:00")
    newer = RunRecord(run_id="run-newer", started_at="2026-07-25T00:00:00+00:00")
    save_run(older, tmp_path)
    save_run(newer, tmp_path)

    assert latest_run_id(tmp_path) == "run-newer"


def test_latest_run_id_is_none_when_no_runs_exist(tmp_path):
    assert latest_run_id(tmp_path / "nonexistent") is None


def test_load_run_raises_run_not_found_error(tmp_path):
    with pytest.raises(RunNotFoundError):
        load_run("does-not-exist", tmp_path)


def test_task_summary_is_truncated_to_avoid_becoming_a_payload_dump(tmp_path):
    huge_task = "x" * 5000
    result = DelegateResult(result="ok", caveats="", model="local-ollama")
    event = DelegationEvent.from_result(
        task_type="file_summarization",
        task_summary=huge_task,
        result=result,
        candidate="local-ollama",
        duration_seconds=1.0,
    )
    assert len(event.task_summary) < len(huge_task)


def test_cost_is_captured_when_provided(tmp_path):
    result = DelegateResult(result="ok", caveats="", model="remote-candidate")
    event = DelegationEvent.from_result(
        task_type="file_summarization",
        task_summary="summarize README",
        result=result,
        candidate="remote-candidate",
        duration_seconds=1.0,
        cost=0.0042,
    )
    record = RunRecord(run_id="run-cost")
    record.record_delegation(event)
    save_run(record, tmp_path)

    loaded = load_run("run-cost", tmp_path)
    assert loaded.delegations[0].cost == 0.0042
    assert "0.0042" in render_run(loaded)


def test_cli_log_shows_both_a_delegating_and_a_non_delegating_run(tmp_path, capsys):
    """U6 Verification: after one delegating task and one non-delegating
    task, `josu log` shows both with enough detail to explain what
    happened."""
    delegating = RunRecord(run_id="run-a", started_at="2026-07-25T09:00:00+00:00")
    delegating.record_delegation(
        DelegationEvent.from_result(
            task_type="file_summarization",
            task_summary="summarize src/foo.py",
            result=DelegateResult(result="...", caveats="low confidence on edge cases", model="local-ollama"),
            candidate="local-ollama",
            duration_seconds=3.0,
        )
    )
    save_run(delegating, tmp_path)

    non_delegating = RunRecord(run_id="run-b", started_at="2026-07-25T10:00:00+00:00")
    non_delegating.record_graph_query("search", "auth module", result_count=4)
    save_run(non_delegating, tmp_path)

    parser = build_parser()

    args = parser.parse_args(["log", "run-a", "--runlog-dir", str(tmp_path)])
    assert args.func(args) == 0
    out_a = capsys.readouterr().out
    assert "summarize src/foo.py" in out_a
    assert "low confidence on edge cases" in out_a
    assert "served by 'local-ollama'" in out_a

    args = parser.parse_args(["log", "run-b", "--runlog-dir", str(tmp_path)])
    assert args.func(args) == 0
    out_b = capsys.readouterr().out
    assert "run-b" in out_b
    assert "delegations: none" in out_b
    assert "auth module" in out_b

    # No run id given -> most recently started run (run-b).
    args = parser.parse_args(["log", "--runlog-dir", str(tmp_path)])
    assert args.func(args) == 0
    out_latest = capsys.readouterr().out
    assert "run-b" in out_latest


def test_cli_log_reports_when_no_runs_exist(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args(["log", "--runlog-dir", str(tmp_path / "empty")])
    exit_code = args.func(args)
    assert exit_code == 1
    assert "No run log entries found" in capsys.readouterr().out
