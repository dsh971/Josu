"""Tests for the orchestrator main loop, `josu run` (U13).

Real `git` subprocess calls throughout (via the `git_repo` fixture from
`tests/orchestrator/conftest.py`) and a real bound TCP socket for the
daemon-reachability check -- no mocking of `worktree.py`/`merge.py`/
`circuit_breaker.py`/`mcp_manifest.py`/`graph/index.py` internals, all of
which are already independently tested elsewhere. `run_task()` no longer
takes a graph engine directly (doc-review fix): reindexing is routed
through the daemon's `/graph/internal/reindex` route
(`graph/internal_api.py`), so `fake_daemon` below is enough for tests that
don't care about reindex correctness; the one test that does
(`test_successful_merge_triggers_exactly_the_changed_path_reindex`) uses a
real daemon against a fixture gortex HTTP server instead.

The one boundary faked here is the adapter invocation itself
(`adapters.claude_code.run()`'s call shape): `run_task()` accepts an
`adapter_run` test seam for exactly this reason (see `run.py`'s own
docstring on why). A hand-written Python callable matching that call shape
is the appropriate fake-at-the-boundary for THIS module's tests -- the
adapter's own internal behavior (stream-json parsing, the git allowlist,
the MCP-server-connected check, ...) is already covered by
`test_claude_code.py`'s hand-written fake `claude` executable on `PATH`;
re-mocking or re-faking any of that here would test nothing new. What these
tests care about is `run_task()`'s OWN composition and sequencing, so
faking exactly at the "the adapter ran" boundary is what actually isolates
that.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from josu.config import JosuConfig
from josu.config.chains import ChainsConfig
from josu.config.delegate import DelegateConfig
from josu.config.orchestrator import McpApprovalVerified, OrchestratorConfig
from josu.observability.runlog import (
    RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT,
    RUN_OUTCOME_DIVERGED,
    RUN_OUTCOME_MERGED,
    RUN_OUTCOME_REJECTED,
    load_run,
)
from josu.orchestrator.adapter import InvocationResult
from josu.orchestrator.adapters.claude_code import (
    DELEGATE_SERVER_NAME,
    GRAPH_SERVER_NAME,
    ClaudeCodeRunResult,
    ParsedRun,
    build_default_adapter_config,
)
from josu.orchestrator.run import (
    DaemonNotReachableError,
    NoUsableAdapterError,
    run_task,
)
from josu.orchestrator.worktree import worktree_diff
from tests.conftest import daemon_thread as _daemon_thread
from tests.conftest import free_port as _free_port


# --- shared fixtures ----------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo -- mirrors `tests/orchestrator/conftest.py`'s
    own `git_repo` fixture (reused directly here, since this file lives in
    the same `tests/orchestrator/` package)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)
    return repo


@pytest.fixture
def fake_daemon():
    """A real, bound (but otherwise-inert) TCP listener standing in for the
    josu daemon -- `run_task()`'s reachability check just needs SOMETHING
    to accept a connection and answer an HTTP request; it doesn't need the
    real MCP/SSE routes to run these tests (those are `test_daemon.py`'s
    concern). A raw `socket.accept()` loop, closed on fixture teardown."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    host, port = server.getsockname()

    stop = threading.Event()

    def _serve():
        with closing(server):
            server.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with closing(conn):
                    try:
                        conn.recv(4096)
                        conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        stop.set()
        thread.join(timeout=2)


@pytest.fixture
def adapter_config():
    attestation = McpApprovalVerified(verified=True, verified_date=date(2026, 1, 1))
    return build_default_adapter_config(attestation)


@pytest.fixture
def config(adapter_config, tmp_path):
    # A real (if never-written) path under tmp_path, not a literal
    # nonexistent absolute path -- `run_task()`'s reindex step resolves the
    # daemon's shared-secret token alongside this path (`daemon_auth.py`),
    # which needs a creatable parent directory.
    return JosuConfig(
        path=tmp_path / "josu.toml",
        delegate=DelegateConfig(),
        chains=ChainsConfig(),
        orchestrator=OrchestratorConfig(adapters=[adapter_config]),
    )


def _fake_run_result(worktree, *, result_text: str = "done") -> ClaudeCodeRunResult:
    return ClaudeCodeRunResult(
        invocation=InvocationResult(argv=["fake-claude"], returncode=0, stdout="", stderr=""),
        parsed=ParsedRun(
            mcp_servers_connected=frozenset({GRAPH_SERVER_NAME, DELEGATE_SERVER_NAME}),
            bash_commands=[],
            read_edit_paths=[],
        ),
        fields={"result": result_text, "is_error": False},
        diff=worktree_diff(worktree),
    )


def _make_editing_fake_adapter(write_fn):
    """A hand-written fake standing in for `adapters.claude_code.run()`:
    applies `write_fn(worktree)` (the "work Claude Code did") to the
    worktree, then returns a `ClaudeCodeRunResult`-shaped object. Records
    every call's kwargs for assertions on what `run_task()` actually passed
    through."""
    calls: list[dict] = []

    def _fake_run(adapter, *, worktree, manifest, config_path, task, timeout=None):
        calls.append(
            {
                "adapter": adapter,
                "worktree": worktree,
                "manifest": manifest,
                "config_path": config_path,
                "task": task,
                "timeout": timeout,
            }
        )
        write_fn(worktree)
        return _fake_run_result(worktree)

    return _fake_run, calls


# --- happy path: worktree -> adapter -> diff -> approve -> merge -> reindex --


def test_full_run_approved_produces_worktree_adapter_diff_merge_and_saved_record(
    git_repo, tmp_path, fake_daemon, config
):
    """Covers the plan's first U13 test scenario: a fixture task against a
    fixture repo produces a worktree, an adapter invocation, a surfaced
    diff, and (on approval) a merge, with a saved run-log record covering
    all of it."""
    host, port = fake_daemon

    def _write(worktree):
        (worktree.path / "README.md").write_text("written by claude code\n", encoding="utf-8")

    fake_run, calls = _make_editing_fake_adapter(_write)
    seen_diffs: list[str] = []

    def _approve(diff: str) -> bool:
        seen_diffs.append(diff)
        return True

    result = run_task(
        "add a docstring",
        config=config,
        repo_root=git_repo,
        approve=_approve,
        worktrees_dir=tmp_path / "worktrees",
        runlog_dir=tmp_path / "runlog",
        adapter_run=fake_run,
        host=host,
        port=port,
    )

    # The adapter invocation actually happened, against the worktree
    # run_task itself created.
    assert len(calls) == 1
    assert calls[0]["worktree"] == result.worktree
    assert calls[0]["task"] == "add a docstring"

    # A diff was surfaced for review before the merge decision.
    assert len(seen_diffs) == 1
    assert "written by claude code" in seen_diffs[0]
    assert result.diff == seen_diffs[0]

    # The merge actually happened, into the real repo_root.
    assert result.outcome == RUN_OUTCOME_MERGED
    assert result.merge_result is not None
    assert result.merge_result.merged is True
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "written by claude code\n"

    # A run-log record was saved covering the whole run.
    loaded = load_run(result.run_id, tmp_path / "runlog")
    assert loaded.outcome == RUN_OUTCOME_MERGED
    assert loaded.worktree_path == str(result.worktree.path)
    assert loaded.task_description == "add a docstring"

    # The worktree is auto-removed on a successful merge (P1 fix): gone
    # from disk AND from git's own worktree bookkeeping, so U8's
    # crash-recovery scan never mistakenly treats a normal, successful run
    # as a crash orphan.
    assert not result.worktree.path.exists()
    worktree_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(result.worktree.path) not in worktree_list


# --- developer-rejected diff: no merge, still a saved record ----------------


def test_developer_rejected_diff_produces_a_saved_record_with_no_merge(
    git_repo, tmp_path, fake_daemon, config
):
    """Covers the plan's second U13 test scenario: a developer-rejected
    diff produces a saved record with no merge."""
    host, port = fake_daemon

    def _write(worktree):
        (worktree.path / "README.md").write_text("proposed change\n", encoding="utf-8")

    fake_run, calls = _make_editing_fake_adapter(_write)

    result = run_task(
        "a task the developer will reject",
        config=config,
        repo_root=git_repo,
        approve=lambda diff: False,
        worktrees_dir=tmp_path / "worktrees",
        runlog_dir=tmp_path / "runlog",
        adapter_run=fake_run,
        host=host,
        port=port,
    )

    assert len(calls) == 1
    assert result.outcome == RUN_OUTCOME_REJECTED
    assert result.merge_result is not None
    assert result.merge_result.merged is False
    # repo_root's real working tree is untouched.
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "hello\n"

    loaded = load_run(result.run_id, tmp_path / "runlog")
    assert loaded.outcome == RUN_OUTCOME_REJECTED

    # The worktree is auto-removed on a developer rejection too (P1 fix):
    # gone from disk AND from git's own worktree bookkeeping.
    assert not result.worktree.path.exists()
    worktree_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(result.worktree.path) not in worktree_list


# --- circuit-breaker timeout: saved record, worktree left for U8 cleanup ----


def test_circuit_breaker_timeout_produces_saved_record_and_leaves_worktree(
    git_repo, tmp_path, fake_daemon, config
):
    """Covers the plan's third U13 test scenario: a circuit-breaker timeout
    produces a saved record showing the trip, with the worktree left for
    U8's cleanup rather than silently discarded."""
    host, port = fake_daemon

    def _timeout_fake_run(adapter, *, worktree, manifest, config_path, task, timeout=None):
        # `run_under_circuit_breaker()` catches `subprocess.TimeoutExpired`
        # raised out of the wrapped call and re-raises it as
        # `CircuitBreakerTimeoutError` -- this is the real mechanism
        # (circuit_breaker.py), not re-implemented here.
        raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=timeout or 0)

    def _approve_should_never_be_called(diff: str) -> bool:
        raise AssertionError("approve() must not be called after a circuit-breaker trip")

    result = run_task(
        "a task that will time out",
        config=config,
        repo_root=git_repo,
        approve=_approve_should_never_be_called,
        worktrees_dir=tmp_path / "worktrees",
        runlog_dir=tmp_path / "runlog",
        adapter_run=_timeout_fake_run,
        host=host,
        port=port,
    )

    assert result.outcome == RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT
    assert result.merge_result is None
    assert result.worktree is not None

    # The worktree is left in place -- U8's cleanup path handles it, not a
    # silent discard here.
    assert result.worktree.path.exists()
    worktree_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(result.worktree.path) in worktree_list

    loaded = load_run(result.run_id, tmp_path / "runlog")
    assert loaded.outcome == RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT
    assert len(loaded.circuit_breaker_events) == 1


# --- P3 fix: a hang during worktree creation (steps 1-3) is also caught ----


def test_hang_during_worktree_creation_is_caught_by_the_circuit_breaker(
    git_repo, tmp_path, fake_daemon, adapter_config, monkeypatch
):
    """P3 fix (U13/U14 Tier 2 review): previously, worktree creation/the R21
    snapshot/MCP manifest generation (steps 1-3) ran with NO timeout
    coverage at all -- only the adapter invocation (step 4) was wrapped by
    the circuit breaker, so a `git` lock-contention hang during worktree
    creation could hang the WHOLE `josu run` invocation indefinitely.

    Proven with a REAL slow subprocess standing in for a wedged `git` call
    (mirroring `test_circuit_breaker.py`'s own "a real subprocess that
    would sleep past the budget" convention for proving
    `run_under_circuit_breaker()`'s actual timeout mechanism, not a
    hand-raised exception standing in for it) -- `worktree.create_worktree`
    is patched to run a real `python -c "time.sleep(5)"` subprocess with the
    circuit breaker's own tiny remaining budget as its `timeout=`, so a REAL
    `subprocess.TimeoutExpired` fires well before the fake 5s sleep
    completes, exactly as a genuinely wedged `git` call inside
    `worktree.py`'s own `_run_git()` would produce now that `timeout` is
    threaded through it."""
    host, port = fake_daemon

    def _hanging_create_worktree(
        repo_root, worktrees_dir, *, name=None, task_description=None, timeout=None
    ):
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=timeout,
            check=True,
        )
        raise AssertionError("the fake slow subprocess must time out before reaching this line")

    monkeypatch.setattr("josu.orchestrator.run.create_worktree", _hanging_create_worktree)

    def _adapter_should_never_run(adapter, *, worktree, manifest, config_path, task, timeout=None):
        raise AssertionError(
            "the adapter must never run -- the circuit breaker must trip during the "
            "worktree-creation hang, before step 4 is ever reached"
        )

    def _approve_should_never_be_called(diff: str) -> bool:
        raise AssertionError("approve() must not be called after a circuit-breaker trip")

    tiny_timeout_config = JosuConfig(
        path=tmp_path / "josu.toml",
        delegate=DelegateConfig(),
        chains=ChainsConfig(),
        orchestrator=OrchestratorConfig(adapters=[adapter_config]),
        wall_clock_timeout_seconds=0.2,
    )

    start = time.monotonic()
    result = run_task(
        "a task whose worktree creation hangs",
        config=tiny_timeout_config,
        repo_root=git_repo,
        approve=_approve_should_never_be_called,
        worktrees_dir=tmp_path / "worktrees",
        runlog_dir=tmp_path / "runlog",
        adapter_run=_adapter_should_never_run,
        host=host,
        port=port,
    )
    elapsed = time.monotonic() - start

    # Caught well short of the fake call's full 5s sleep -- the whole
    # invocation returned, it did not hang.
    assert elapsed < 5.0

    assert result.outcome == RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT
    assert result.merge_result is None
    # `create_worktree()` never actually returned a `Worktree` (it hung, was
    # killed, and the timeout propagated) -- nothing to leave behind for U8
    # cleanup from THIS run's own bookkeeping in this case.
    assert result.worktree is None

    loaded = load_run(result.run_id, tmp_path / "runlog")
    assert loaded.outcome == RUN_OUTCOME_CIRCUIT_BREAKER_TIMEOUT
    assert len(loaded.circuit_breaker_events) == 1


# --- successful merge triggers exactly the changed-path reindex (R14) -------


def test_successful_merge_triggers_exactly_the_changed_path_reindex(
    git_repo, tmp_path, config
):
    """Covers the plan's fourth U13 test scenario: a successful merge
    triggers exactly the changed-path reindex, not a full rebuild.

    Uses a real running daemon (against a fixture gortex HTTP server, not
    `fake_daemon`'s inert TCP listener) so the assertion can inspect what
    `/graph/internal/reindex` actually received -- proving the reindex call
    reaches the daemon's live engine with exactly the changed-file list,
    not a full-tree rescan or a throwaway engine nothing else can see."""
    import json as json_module

    from josu.daemon import create_app
    from josu.graph.gortex_process import GortexProcess

    (git_repo / "unrelated.py").write_text(
        "def unrelated_function():\n    return 'untouched'\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add unrelated.py"], cwd=git_repo, check=True)

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received_reindex_calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json_module.loads(self.rfile.read(length)) if length else {}
            if self.path == "/v1/tools/reindex_repository":
                received_reindex_calls.append(body)
            payload = json_module.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    gortex_httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    gortex_thread = threading.Thread(target=gortex_httpd.serve_forever, daemon=True)
    gortex_thread.start()
    fake_gortex_process = GortexProcess(
        host="127.0.0.1", port=gortex_httpd.server_port, popen=None
    )

    app = create_app(target=git_repo, config=config, gortex_process=fake_gortex_process)

    def _write(worktree):
        (worktree.path / "new_module.py").write_text(
            "def brand_new_function():\n    pass\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "new_module.py"], cwd=worktree.path, check=True)

    fake_run, _calls = _make_editing_fake_adapter(_write)

    daemon_port = _free_port()
    try:
        with _daemon_thread(app, "127.0.0.1", daemon_port):
            result = run_task(
                "add a new module",
                config=config,
                repo_root=git_repo,
                approve=lambda diff: True,
                worktrees_dir=tmp_path / "worktrees",
                runlog_dir=tmp_path / "runlog",
                adapter_run=fake_run,
                host="127.0.0.1",
                port=daemon_port,
            )
    finally:
        gortex_httpd.shutdown()
        gortex_thread.join()

    assert result.outcome == RUN_OUTCOME_MERGED
    assert result.reindex_result is not None
    reindexed = [Path(p).name for p in result.reindex_result.reindexed_files]
    assert reindexed == ["new_module.py"]
    assert result.reindex_result.engine_error is None

    # The daemon's live engine received exactly the changed-path reindex --
    # not unrelated.py, not README.md, not a full rebuild.
    assert len(received_reindex_calls) == 1
    reindexed_paths = [Path(p).name for p in received_reindex_calls[0]["paths"]]
    assert reindexed_paths == ["new_module.py"]

    loaded = load_run(result.run_id, tmp_path / "runlog")
    assert loaded.reindexed_files == result.reindex_result.reindexed_files


# --- R21: snapshot timing -- concurrent edit made WHILE the adapter runs ----


def test_concurrent_edit_made_while_the_adapter_is_running_is_caught_as_diverged(
    git_repo, tmp_path, fake_daemon, config
):
    """Covers the plan's fifth U13 test scenario, and the whole reason this
    unit's composition order is load-bearing: a REAL concurrent edit to
    `repo_root`, made on a separate thread WHILE the (fake) adapter is
    "running" -- not before it starts -- is still caught as diverged at
    merge time.

    This proves `snapshot_repo()` was captured BEFORE the adapter ran, not
    after: if `run_task()` captured the snapshot any later (e.g. after the
    adapter invocation instead of immediately following worktree creation),
    this concurrent edit would already be baked into the "before" baseline
    by the time the snapshot was taken, `find_diverged_paths()` would find
    no difference, and the merge would silently succeed instead of
    aborting -- exactly the regression this test guards against.

    Uses real `threading.Event`s (not a `time.sleep` guess) to force actual
    temporal overlap: the fake adapter blocks until the concurrent editor
    thread has ACTUALLY written to `repo_root` on disk, so the edit
    provably lands while the adapter call is still in progress.
    """
    host, port = fake_daemon

    adapter_started = threading.Event()
    concurrent_edit_done = threading.Event()

    def _fake_run(adapter, *, worktree, manifest, config_path, task, timeout=None):
        adapter_started.set()
        # Block until the concurrent edit has actually landed on disk in
        # repo_root -- proves the edit happens DURING this call, not before
        # or after it.
        assert concurrent_edit_done.wait(timeout=5), "concurrent edit never landed"
        (worktree.path / "README.md").write_text("claude code's version\n", encoding="utf-8")
        return _fake_run_result(worktree)

    def _concurrent_editor():
        assert adapter_started.wait(timeout=5), "adapter never started"
        (git_repo / "README.md").write_text("developer's concurrent edit\n", encoding="utf-8")
        concurrent_edit_done.set()

    editor_thread = threading.Thread(target=_concurrent_editor)
    editor_thread.start()

    try:
        result = run_task(
            "a task racing a concurrent edit",
            config=config,
            repo_root=git_repo,
            approve=lambda diff: True,
            worktrees_dir=tmp_path / "worktrees",
            runlog_dir=tmp_path / "runlog",
            adapter_run=_fake_run,
            host=host,
            port=port,
        )
    finally:
        editor_thread.join(timeout=5)

    assert result.outcome == RUN_OUTCOME_DIVERGED
    assert result.merge_result is None
    assert isinstance(result.error, Exception)
    assert "README.md" in getattr(result.error, "diverged_paths", [])

    # Aborted cleanly: repo_root keeps the developer's own concurrent edit,
    # never overwritten.
    assert (
        git_repo / "README.md"
    ).read_text(encoding="utf-8") == "developer's concurrent edit\n"

    loaded = load_run(result.run_id, tmp_path / "runlog")
    assert loaded.outcome == RUN_OUTCOME_DIVERGED
    assert "README.md" in loaded.diverged_paths


# --- no daemon reachable: a clear CLI-usable error, not a stack trace -------


def test_no_daemon_reachable_raises_a_clear_actionable_error(git_repo, tmp_path, config):
    """Covers the plan's sixth U13 test scenario: running with no daemon
    reachable produces a clear error, not a stack trace -- and is checked
    early, before any worktree/git work."""
    # Bind an ephemeral port, then close it immediately -- almost certainly
    # nothing is listening on it a moment later, giving a real connection
    # refusal rather than a mocked one.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    _host, unused_port = probe.getsockname()
    probe.close()

    def _approve_should_never_be_called(diff: str) -> bool:
        raise AssertionError("approve() must not be called when the daemon is unreachable")

    with pytest.raises(DaemonNotReachableError) as exc_info:
        run_task(
            "a task that should never run",
            config=config,
            repo_root=git_repo,
            approve=_approve_should_never_be_called,
            worktrees_dir=tmp_path / "worktrees",
            runlog_dir=tmp_path / "runlog",
            host="127.0.0.1",
            port=unused_port,
        )

    assert "start it with" in str(exc_info.value)
    assert "josu daemon start" in str(exc_info.value)

    # No worktree/git work happened -- checked before any of that, and no
    # run-log record needed to be saved either.
    assert not (tmp_path / "worktrees").exists()
    assert not (tmp_path / "runlog").exists()


def test_no_usable_adapter_configured_raises_a_clear_actionable_error(
    git_repo, tmp_path, fake_daemon
):
    """A `josu.toml` with no matching, attested `[[orchestrator.adapters]]`
    entry also fails clearly, not with a stack trace or an attempt to
    invoke a nonexistent adapter."""
    host, port = fake_daemon
    empty_config = JosuConfig(
        path=tmp_path / "josu.toml",
        delegate=DelegateConfig(),
        chains=ChainsConfig(),
        orchestrator=OrchestratorConfig(adapters=[]),
    )

    with pytest.raises(NoUsableAdapterError):
        run_task(
            "a task with nothing configured to run it",
            config=empty_config,
            repo_root=git_repo,
            approve=lambda diff: True,
            worktrees_dir=tmp_path / "worktrees",
            runlog_dir=tmp_path / "runlog",
            host=host,
            port=port,
        )


# --- an unexpected exception still saves a record, now WITH diagnostic detail -


def test_unexpected_exception_from_the_adapter_saves_an_error_record_with_diagnostic_detail(
    git_repo, tmp_path, fake_daemon, config
):
    """P1 fix: `RUN_OUTCOME_ERROR` previously saved with no diagnostic
    detail at all. An adapter raising something unexpected (not
    `CircuitBreakerTimeoutError`/`DivergedWorkingTreeError`, both handled
    specially) is still re-raised out of `run_task()` (unchanged, existing
    behavior -- the `finally` block always saves a record first), but the
    saved `RunRecord` now carries `error_class`/`error_message` describing
    exactly what went wrong, not just a bare `outcome: error`."""
    host, port = fake_daemon

    def _exploding_adapter(adapter, *, worktree, manifest, config_path, task, timeout=None):
        raise RuntimeError("simulated daemon crash mid-run")

    with pytest.raises(RuntimeError, match="simulated daemon crash mid-run"):
        run_task(
            "a task whose adapter blows up unexpectedly",
            config=config,
            repo_root=git_repo,
            approve=lambda diff: True,
            worktrees_dir=tmp_path / "worktrees",
            runlog_dir=tmp_path / "runlog",
            adapter_run=_exploding_adapter,
            host=host,
            port=port,
        )

    # The record was still saved (the `finally` block always runs) --
    # find it by scanning the runlog dir directly, since `run_task()`
    # raised before returning a `RunTaskResult` with a `run_id` to look up.
    from josu.observability.runlog import RUN_OUTCOME_ERROR, list_runs

    run_ids = list_runs(tmp_path / "runlog")
    assert len(run_ids) == 1
    loaded = load_run(run_ids[0], tmp_path / "runlog")

    assert loaded.outcome == RUN_OUTCOME_ERROR
    assert loaded.error_class == "RuntimeError"
    assert loaded.error_message == "simulated daemon crash mid-run"
