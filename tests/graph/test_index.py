"""Tests for incremental re-indexing triggered by commit/save/merge events
(U10, R14).

Rewritten for the graphify-to-gortex swap: `index.py`'s trigger functions
are now `async def` and route through the daemon's live engine via
`graph/internal_api.py`'s internal HTTP route (doc-review bug fix), rather
than mutating a local `GraphifyEngine` directly. Real `git` subprocess
calls against real temp repos throughout (unchanged); a real running daemon
(`daemon.py`) backed by a fixture HTTP server standing in for gortex --
assertions check what that fixture server actually received
(`/v1/tools/reindex_repository`'s `paths` argument), proving the exact
bounded changed-file set reaches the daemon's live engine, matching this
repo's integration-first testing convention.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio
import uvicorn

from josu.config import JosuConfig
from josu.config.chains import ChainsConfig
from josu.config.delegate import DelegateConfig
from josu.config.orchestrator import OrchestratorConfig
from josu.daemon import create_app
from josu.daemon_auth import resolve_daemon_token
from josu.graph.gortex_process import GortexProcess
from josu.graph.index import (
    ReindexResult,
    reindex_on_commit,
    reindex_on_merge,
    reindex_on_save,
)
from josu.orchestrator.merge import merge, snapshot_repo
from josu.orchestrator.worktree import create_worktree
from tests.conftest import free_port as _free_port


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo seeded with two source files, mirroring
    `tests/orchestrator/conftest.py`'s `git_repo` fixture (not reused
    directly -- this unit's file scope is `index.py` + `test_index.py`
    only, so the fixture is defined locally instead of introducing a new
    shared `tests/graph/conftest.py`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8"
    )
    (repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n",
        encoding="utf-8",
    )
    (repo / "unrelated.py").write_text(
        "def unrelated_function():\n    return 'untouched'\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)
    return repo


def _commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


@pytest.fixture
def fake_gortex_server():
    """A real HTTP server standing in for gortex, recording every
    `/v1/tools/reindex_repository` call it receives (the `paths` argument
    is what proves the bounded changed-file set)."""
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
            if self.path == "/v1/tools/reindex_repository":
                calls.append(body)
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield GortexProcess(host="127.0.0.1", port=httpd.server_port, popen=None), calls

    httpd.shutdown()
    thread.join()


@pytest.fixture
def config(tmp_path):
    return JosuConfig(
        path=tmp_path / "josu.toml",
        delegate=DelegateConfig(),
        chains=ChainsConfig(),
        orchestrator=OrchestratorConfig(),
    )


@pytest_asyncio.fixture
async def running_daemon(tmp_path, git_repo, config, fake_gortex_server):
    gortex_process, reindex_calls = fake_gortex_server
    port = _free_port()
    app = create_app(target=git_repo, config=config, gortex_process=gortex_process)
    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    token = resolve_daemon_token(config.path)
    yield f"http://127.0.0.1:{port}", token, reindex_calls
    server.should_exit = True
    await task


# --- Trigger #1: commit event --------------------------------------------


@pytest.mark.asyncio
async def test_single_file_commit_reindexes_only_that_file(running_daemon, git_repo):
    """Covers the plan's first U10 test scenario: a single-file commit
    re-indexes only that file."""
    base_url, token, reindex_calls = running_daemon

    (git_repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hello there, {name}!'\n\n\ndef brand_new():\n    pass\n",
        encoding="utf-8",
    )
    _commit_all(git_repo, "edit helper.py")

    result = await reindex_on_commit(git_repo, base_url=base_url, token=token)

    assert result.reindexed_files == [str((git_repo / "helper.py").resolve())]
    assert result.pruned_files == []
    assert result.engine_error is None

    assert len(reindex_calls) == 1
    assert reindex_calls[0]["paths"] == [str((git_repo / "helper.py").resolve())]


@pytest.mark.asyncio
async def test_commit_touching_multiple_files_reindexes_exactly_those(running_daemon, git_repo):
    base_url, token, reindex_calls = running_daemon

    (git_repo / "helper.py").write_text(
        "def greet(name):\n    return f'Hiya, {name}!'\n", encoding="utf-8"
    )
    (git_repo / "new_module.py").write_text(
        "def brand_new_function():\n    return 42\n", encoding="utf-8"
    )
    _commit_all(git_repo, "edit two files")

    result = await reindex_on_commit(git_repo, base_url=base_url, token=token)

    assert sorted(result.reindexed_files) == sorted(
        str(p.resolve()) for p in [git_repo / "helper.py", git_repo / "new_module.py"]
    )
    assert sorted(reindex_calls[0]["paths"]) == sorted(result.reindexed_files)


@pytest.mark.asyncio
async def test_commit_with_only_non_code_files_still_notifies_the_daemon(running_daemon, git_repo):
    """Unlike the graphify-era implementation (which filtered to
    AST-extractable code files before ever reaching the engine), this
    module no longer does local file-type filtering -- gortex's own
    257-language indexer decides relevance, not josu (see Key Technical
    Decisions). A docs-only commit still reaches the daemon with the
    changed path; it's gortex's call whether anything indexable came of
    it."""
    base_url, token, reindex_calls = running_daemon

    (git_repo / "README.md").write_text("docs only\n", encoding="utf-8")
    _commit_all(git_repo, "docs-only commit")

    result = await reindex_on_commit(git_repo, base_url=base_url, token=token)

    assert result.reindexed_files == [str((git_repo / "README.md").resolve())]
    assert result.engine_error is None
    assert reindex_calls[0]["paths"] == [str((git_repo / "README.md").resolve())]


@pytest.mark.asyncio
async def test_commit_deleting_a_file_prunes_it(running_daemon, git_repo):
    base_url, token, reindex_calls = running_daemon

    (git_repo / "unrelated.py").unlink()
    _commit_all(git_repo, "delete unrelated.py")

    result = await reindex_on_commit(git_repo, base_url=base_url, token=token)

    assert result.pruned_files == [str((git_repo / "unrelated.py").resolve())]
    assert result.reindexed_files == []
    # A pure-deletion event still notifies the daemon, but with an empty
    # `paths` list -- nothing left on disk to re-extract.
    assert reindex_calls == [{"repo": str(git_repo), "paths": []}]


# --- Trigger #2: save event ------------------------------------------------


@pytest.mark.asyncio
async def test_save_event_reindexes_only_the_saved_file(running_daemon, git_repo):
    base_url, token, reindex_calls = running_daemon

    (git_repo / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n\n\n"
        "def saved_addition():\n    pass\n",
        encoding="utf-8",
    )

    result = await reindex_on_save(git_repo, git_repo / "main.py", base_url=base_url, token=token)

    assert result.reindexed_files == [str((git_repo / "main.py").resolve())]
    assert reindex_calls[0]["paths"] == [str((git_repo / "main.py").resolve())]


# --- Trigger #3: a completed merge (U5) -----------------------------------


@pytest.mark.asyncio
async def test_merged_diff_touching_three_files_reindexes_exactly_those(
    running_daemon, git_repo, tmp_path
):
    """Covers the plan's second U10 test scenario: a merged diff touching
    three files re-indexes exactly those three."""
    base_url, token, reindex_calls = running_daemon

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)

    (worktree.path / "helper.py").write_text(
        "def greet(name):\n    return f'Yo, {name}!'\n", encoding="utf-8"
    )
    (worktree.path / "main.py").write_text(
        "from helper import greet\n\n\ndef main():\n    print(greet('world'))\n\n\n"
        "def merged_addition():\n    pass\n",
        encoding="utf-8",
    )
    (worktree.path / "third_module.py").write_text(
        "def third_function():\n    pass\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "third_module.py"], cwd=worktree.path, check=True)

    merge_result = merge(worktree, snapshot, approved=True)
    assert merge_result.merged is True

    result = await reindex_on_merge(worktree, merge_result, base_url=base_url, token=token)

    expected = sorted(
        str(p.resolve())
        for p in [git_repo / "helper.py", git_repo / "main.py", git_repo / "third_module.py"]
    )
    assert sorted(result.reindexed_files) == expected
    assert sorted(reindex_calls[0]["paths"]) == expected


@pytest.mark.asyncio
async def test_rejected_merge_reindexes_nothing(running_daemon, git_repo, tmp_path):
    base_url, token, reindex_calls = running_daemon

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)
    (worktree.path / "helper.py").write_text("def greet(name):\n    pass\n", encoding="utf-8")

    merge_result = merge(worktree, snapshot, approved=False)
    assert merge_result.merged is False

    result = await reindex_on_merge(worktree, merge_result, base_url=base_url, token=token)

    assert result == ReindexResult()
    assert reindex_calls == []


@pytest.mark.asyncio
async def test_merge_deleting_a_file_prunes_it_from_the_graph(running_daemon, git_repo, tmp_path):
    base_url, token, reindex_calls = running_daemon

    worktree = create_worktree(git_repo, tmp_path / "worktrees")
    snapshot = snapshot_repo(git_repo, stash_ref=worktree.stash_ref)
    (worktree.path / "unrelated.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=worktree.path, check=True)

    merge_result = merge(worktree, snapshot, approved=True)
    assert merge_result.merged is True

    result = await reindex_on_merge(worktree, merge_result, base_url=base_url, token=token)

    assert result.pruned_files == [str((git_repo / "unrelated.py").resolve())]


# --- daemon-unreachable degrades gracefully, does not raise ----------------


@pytest.mark.asyncio
async def test_reindex_degrades_gracefully_when_daemon_unreachable(git_repo):
    """R13-style posture: a reindex trigger whose daemon call fails must
    report the failure via `ReindexResult.engine_error`, never raise and
    break the triggering commit/save/merge event."""
    (git_repo / "helper.py").write_text("def greet(name):\n    pass\n", encoding="utf-8")
    _commit_all(git_repo, "edit helper.py")

    result = await reindex_on_commit(
        git_repo, base_url="http://127.0.0.1:1", token="irrelevant"
    )

    assert result.engine_error is not None
    assert result.reindexed_files == [str((git_repo / "helper.py").resolve())]
