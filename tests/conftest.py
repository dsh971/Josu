"""Shared test fixtures.

`FakeGraphEngine` is a hand-written fake implementing the `GraphEngine`
Protocol directly (`async def build/update/search/execute`) -- mirrors this
repo's existing convention of hand-written fakes at a Protocol boundary
(see `tests/delegate/test_local_model.py`'s `DelegateClient` fakes).
Used wherever a test needs "a working graph engine" to construct a
server/queue/daemon around, without exercising `GortexEngine`'s own HTTP
behavior (covered separately, against a real fixture HTTP server, in
`tests/graph/test_gortex.py`).

`free_port()`/`daemon_thread()` are shared by every test module that spins
up a real daemon (`tests/test_daemon.py`, `tests/orchestrator/test_run.py`,
`tests/graph/test_index.py`) -- previously hand-duplicated per file; this is
the one copy.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from contextlib import contextmanager
from pathlib import Path


class FakeGraphEngine:
    """Records `build`/`update` calls; `search`/`execute` return
    caller-configured canned results. `execute` raises `ValueError` for any
    operation name not present in `execute_results`, matching
    `GortexEngine`'s "unrecognized operation" behavior closely enough for
    tests that only need *a* graph engine, not gortex's specific wire
    format."""

    def __init__(
        self,
        *,
        search_results: list[dict] | None = None,
        execute_results: dict[str, dict] | None = None,
    ) -> None:
        self.search_results = search_results if search_results is not None else []
        self.execute_results = execute_results if execute_results is not None else {}
        self.build_calls: list[Path] = []
        self.update_calls: list[tuple[Path, list[Path]]] = []

    async def build(self, root: Path) -> None:
        self.build_calls.append(root)

    async def update(self, root: Path, changed_files: list[Path]) -> None:
        self.update_calls.append((root, list(changed_files)))

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        return self.search_results[:limit]

    async def execute(self, operation: str, params: dict) -> dict:
        if operation not in self.execute_results:
            raise ValueError(f"unknown operation: {operation}")
        return self.execute_results[operation]


def free_port() -> int:
    """A free ephemeral TCP port on 127.0.0.1, bound-then-released."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def daemon_thread(app, host: str, port: int):
    """Serve `app` via a real `uvicorn` server on its own background thread
    with its own event loop -- distinct from the current test's event loop
    (if any). Needed for callers that are themselves `asyncio.run()`-wrapped
    sync entry points (`cli.py`'s `_cmd_delegate`, `watchers.py`'s
    `_run_commit_hook_from_cli()`) -- calling `asyncio.run()` from inside an
    already-running event loop raises, so the daemon under test genuinely
    needs its own thread/loop, not just an `asyncio.create_task()` in the
    caller's loop (see `running_daemon`-style fixtures for that simpler
    case, used when the test itself is `async def`)."""
    ready = threading.Event()
    stop = threading.Event()
    errors: list[BaseException] = []

    def _run() -> None:
        async def _serve() -> None:
            import uvicorn

            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            serve_task = asyncio.ensure_future(server.serve())
            started = False
            for _ in range(200):
                if server.started:
                    started = True
                    break
                await asyncio.sleep(0.02)
            if not started:
                # Fixture bug fix (testing review): `ready.set()` used to run
                # unconditionally here regardless of `started`, which made the
                # caller's `if not ready.wait(timeout=10): raise ...` guard
                # below non-functional -- `ready` was always set well within
                # 10s even when the server never actually started, so a slow
                # or failed startup was silently treated as success. Recorded
                # as an error and surfaced via the `if errors: raise
                # errors[0]` check below instead.
                errors.append(
                    RuntimeError(
                        f"uvicorn server did not report started within timeout "
                        f"(host={host}, port={port})"
                    )
                )
                ready.set()
                server.should_exit = True
                await serve_task
                return
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.02)
            server.should_exit = True
            await serve_task

        try:
            asyncio.run(_serve())
        except BaseException as exc:  # pragma: no cover - surfaced via `errors`
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        stop.set()
        thread.join(timeout=5)
        raise RuntimeError("daemon thread failed to start in time")
    if errors:
        raise errors[0]
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)
