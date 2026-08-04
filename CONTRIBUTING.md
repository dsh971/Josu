# Contributing to josu

## Development setup

```bash
git clone <this-repo>
cd josu
uv sync --extra dev
```

`uv sync --extra dev` installs josu itself plus the `dev` extra (`pytest`, `pytest-asyncio`) into `.venv`. Python 3.12+ is required (see `.python-version`).

No real `gortex` binary or `claude` CLI is needed to develop or run the test suite — every test that would otherwise need one stands up a real local HTTP server (or a fake executable on `PATH`) as a stand-in. You only need those installed to run josu itself end-to-end; see the README's [Getting started](README.md#getting-started) section.

## Running tests

```bash
uv run python -m pytest tests/ -q
```

Scope to one area while iterating, e.g. `uv run python -m pytest tests/graph/ -q`.

### Testing conventions

- **Prefer a real fixture server over mocking `httpx`/the transport layer.** Most modules that talk over HTTP (gortex, the daemon's internal routes, delegate candidates) are tested against a real `ThreadingHTTPServer` or `httpx.MockTransport`-backed client standing in for the real service — see `tests/graph/test_gortex.py` or `tests/delegate/test_daemon_client.py` for the pattern. This catches real serialization/timeout/status-code behavior that mocking the client library would paper over.
- **Prefer a hand-written, Protocol-conforming fake over a mock at a true in-process boundary.** `tests/conftest.py`'s `FakeGraphEngine` is the reference example — it implements the `GraphEngine` Protocol directly rather than patching methods on a mock object, so a Protocol change that breaks real implementations also breaks the fake.
- **Integration-first, not unit-first.** Where reasonable, write one test that exercises the real call chain (e.g. a real uvicorn server serving both MCP endpoints, connected to via the actual MCP SDK client) rather than only unit-testing each layer in isolation. `tests/test_daemon.py` is the canonical example.
- **Subprocess-spawning code gets a fake executable on `PATH`, not a mocked `subprocess.Popen`**, wherever a real (if trivial) process is feasible — see `tests/graph/test_gortex_process.py`'s `test_terminate_gortex_terminates_a_real_spawned_process`.
- Shared test fixtures (fake engines, free-port allocation, a background daemon-thread helper) live in `tests/conftest.py` — check there before writing a new one from scratch.

## Code conventions

- **Docstrings/comments explain the non-obvious "why," not the "what."** A hidden constraint, a subtle invariant, or the reason an approach was rejected earns a comment; restating what the code already says plainly does not. Avoid narrating *which review round* found an issue or *what a previous version did wrong* — that belongs in the commit message/PR description, not in a docstring that has to stay accurate long after the review is forgotten.
- **Subprocess calls are always an argv list (`shell=False`), never a shell string**, and are scoped to a per-module subcommand allowlist where the argv is partly caller-influenced (see `graph/index.py`'s `_GIT_INDEX_SUBCOMMANDS`, `orchestrator/worktree.py`'s equivalent). A subprocess spawned for a long-lived service (gortex) gets an explicit, minimal environment (`PATH`/`HOME` only) rather than inheriting the full process environment — see `graph/gortex_process.py`'s module docstring for why.
- **Credentials are always referenced by env-var name in config, never held as a raw value.** `config/delegate.py`'s `api_key_env: str | None` is the pattern — validated for *existence* eagerly at config-load time, resolved to a value lazily at call time, and never logged.
- **File writes that matter (tokens, MCP manifests) are atomic**, via `os.open(path, os.O_CREAT | os.O_EXCL, 0o600)` (or `O_TRUNC` when overwriting-in-place is intended) rather than write-then-chmod, closing the TOCTOU window where a file briefly exists with default permissions.
- **A degraded dependency (an unreachable graph engine, a daemon that's slow to respond) reports itself distinctly rather than crashing the caller** — see `GraphEngineUnavailableError`'s `reason` field and `ReindexResult.engine_error`. Prefer that shape over a bare exception when adding a new best-effort integration point.
- Match the existing per-package structure under `src/josu/` (`config/`, `delegate/`, `graph/`, `orchestrator/`, `proactive/`, `fallback/`, `observability/`, `models/`) — a new concern usually belongs inside one of these, not at the top level.

## Commit messages

Conventional Commits prefixes (`feat:`, `fix:`, `refactor:`, `docs:`), imperative mood, one logical change per commit. Look at `git log` for the established tone — commit messages here tend to name *what broke or shipped and why it mattered*, not just restate the diff.

## Submitting changes

1. Branch off `main`.
2. Keep the test suite green (`uv run python -m pytest tests/ -q`) before opening a PR.
3. Describe the change's motivation in the PR description, not just its shape — if it's a product/architecture decision, check whether `docs/brainstorms/` or `docs/plans/` should be updated alongside the code so those documents stay trustworthy.
4. Expect review to look at correctness, security (this project handles credentials, subprocess invocation, and an internal HTTP surface — see the Code conventions above), reliability (timeouts, error propagation, resource cleanup), and test coverage, in addition to the change's stated goal.
