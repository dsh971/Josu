"""Shared fixtures for orchestrator tests (U4).

`fake_claude_bin` installs a hand-written fake `claude` executable onto
`PATH` -- the true external-process I/O boundary, matching this repo's
existing "real integration where possible, hand-written fakes at the true
I/O boundary" convention (see e.g. `tests/delegate/test_local_model.py`'s
hand-written `DelegateClient` fakes, `tests/test_daemon.py`'s real
uvicorn+SSE-client integration). `adapter.py`'s subprocess-spawning code
itself is never mocked -- only the actual `claude` binary it would spawn is
replaced, at the OS/PATH-lookup boundary, with a real (if trivial)
executable that a real `subprocess.run(shell=False)` call really launches.
"""

from __future__ import annotations

import os
import stat

import pytest

# Reads its behavior entirely from environment variables so each test can
# control it without rewriting the script -- FAKE_CLAUDE_ARGV_LOG (path to
# write the received argv to, as JSON -- proves exactly what argv the real
# subprocess boundary received), FAKE_CLAUDE_STDOUT_FILE (a file whose
# contents are echoed verbatim to stdout, i.e. a canned stream-json fixture),
# and FAKE_CLAUDE_EXIT_CODE (defaults to 0).
_FAKE_CLAUDE_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys


def main():
    argv_log = os.environ.get("FAKE_CLAUDE_ARGV_LOG")
    if argv_log:
        with open(argv_log, "w", encoding="utf-8") as f:
            json.dump(sys.argv, f)

    stdout_file = os.environ.get("FAKE_CLAUDE_STDOUT_FILE")
    if stdout_file and os.path.exists(stdout_file):
        with open(stdout_file, "r", encoding="utf-8") as f:
            sys.stdout.write(f.read())

    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0")))


if __name__ == "__main__":
    main()
"""


@pytest.fixture
def fake_claude_bin(tmp_path, monkeypatch):
    """Prepend a directory containing a fake, executable `claude` onto
    `PATH` and return its path. Tests configure its behavior via the
    `FAKE_CLAUDE_*` env vars described above (set with `monkeypatch.setenv`)
    before invoking anything that spawns `claude`."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script_path = bin_dir / "claude"
    script_path.write_text(_FAKE_CLAUDE_SCRIPT, encoding="utf-8")
    mode = script_path.stat().st_mode
    script_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return script_path


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo -- worktree.py's tests exercise real `git`
    subprocess calls, not a mocked git layer, matching this repo's
    integration-first convention."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=repo, check=True)
    return repo
