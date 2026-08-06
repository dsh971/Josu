"""Tests for `cli.py`'s `_cmd_run` (`josu run`) -- its exception-handling
boundary (Tier 2 review fix for U13/U14) and its config-warning-printing
behavior (feat/cli-ease-of-use plan, U4).

Before the exception-handling fix, `_cmd_run` only caught
`DaemonNotReachableError` and `NoUsableAdapterError` around its
`run_task()` call -- any other exception `run_task()` could raise (e.g.
`MCPServerConnectionError` if the daemon crashes mid-run,
`GitAllowlistViolationError`, `ConfigPathStagedError`) propagated uncaught
all the way to `main()`, producing a raw Python traceback instead of this
file's standard clean `josu run: <message>` shape every other subcommand
uses on failure.

`run_task()` itself is monkeypatched at its lazy-import source
(`josu.orchestrator.run.run_task`, imported inside `_cmd_run`'s own body at
call time) -- these tests' only concern is `_cmd_run`'s own behavior, not
`run_task()`'s internals (already covered by `tests/orchestrator/test_run.py`).
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture
def fake_daemon():
    """A real, bound (but otherwise-inert) TCP listener standing in for the
    josu daemon -- `_cmd_run`'s reachability check just needs SOMETHING to
    accept a connection and answer an HTTP request. Mirrors
    `tests/orchestrator/test_run.py`'s own `fake_daemon` fixture."""
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


def _run_args(*, host: str, port: int, config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        task="do something",
        repo_root=None,
        config=str(config_path),
        graph_out_dir=None,
        adapter="claude_code",
        host=host,
        port=port,
    )


def test_generic_exception_from_run_task_produces_clean_cli_message_not_a_traceback(
    tmp_path, fake_daemon, monkeypatch, capsys
):
    """The P1 fix: an exception `run_task()` raises that ISN'T
    `DaemonNotReachableError`/`NoUsableAdapterError` (here,
    `adapters/claude_code.py`'s `MCPServerConnectionError` -- the review's
    own example of a daemon-crashes-mid-run failure) is caught by
    `_cmd_run`'s catch-all handler and rendered as the standard clean
    `josu run: <message>` line with a non-zero exit code, never an
    uncaught exception/raw traceback."""
    import josu.orchestrator.run as run_module
    from josu.cli import _cmd_run
    from josu.orchestrator.adapters.claude_code import MCPServerConnectionError

    def _fake_run_task(*args, **kwargs):
        raise MCPServerConnectionError("daemon crashed mid-run: connection reset")

    monkeypatch.setattr(run_module, "run_task", _fake_run_task)

    host, port = fake_daemon
    args = _run_args(host=host, port=port, config_path=tmp_path / "josu.toml")

    exit_code = _cmd_run(args)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "josu run: daemon crashed mid-run: connection reset" in captured.out
    # No raw traceback leaked to stdout/stderr -- the exception was caught,
    # not merely printed alongside an unhandled propagation.
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_generic_exception_from_run_task_is_not_confused_with_the_specific_handlers(
    tmp_path, fake_daemon, monkeypatch, capsys
):
    """A sanity check that the new catch-all doesn't shadow or otherwise
    interfere with the pre-existing, more specific `DaemonNotReachableError`
    handling `run_task()` itself already had -- still hit first, still
    produces its own message."""
    import josu.orchestrator.run as run_module
    from josu.cli import _cmd_run
    from josu.delegate.daemon_client import DaemonNotReachableError

    def _fake_run_task(*args, **kwargs):
        raise DaemonNotReachableError("127.0.0.1", 1)

    monkeypatch.setattr(run_module, "run_task", _fake_run_task)

    host, port = fake_daemon
    args = _run_args(host=host, port=port, config_path=tmp_path / "josu.toml")

    exit_code = _cmd_run(args)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "josu daemon not reachable" in captured.out


def test_cmd_run_prints_config_permission_warning(tmp_path, fake_daemon, monkeypatch, capsys):
    """feat/cli-ease-of-use plan (U4): `_cmd_run` loads config in-process
    (unlike `_cmd_delegate`, which never does -- see
    `test_cmd_delegate_prints_no_config_warning_even_when_misconfigured`
    below) and must surface `config.warnings` the same way the daemon
    does. Uses this file's own `fake_daemon` real-TCP-listener fixture for
    the reachability check, matching this repo's "prefer a real fixture
    server over mocking the transport layer" testing convention, rather
    than monkeypatching `check_daemon_reachable` directly."""
    import josu.orchestrator.run as run_module
    from josu.cli import _cmd_run

    def _fake_run_task(*args, **kwargs):
        raise RuntimeError("stub: run_task should not be reached by this warning-only test")

    monkeypatch.setattr(run_module, "run_task", _fake_run_task)

    config_path = tmp_path / "josu.toml"
    config_path.write_text(
        "[[delegate.candidates]]\n"
        'name = "local-ollama"\n'
        'endpoint = "http://localhost:11434/v1"\n'
        "local = true\n"
        'model = "qwen2.5-coder:7b"\n',
        encoding="utf-8",
    )
    os.chmod(config_path, 0o644)

    host, port = fake_daemon
    args = _run_args(host=host, port=port, config_path=config_path)

    _cmd_run(args)

    out = capsys.readouterr().out
    assert "josu run: warning:" in out
    assert "group/world-accessible" in out
