"""Gortex subprocess lifecycle: spawn, health-check, orphan validation (U15).

Gortex runs in its standalone/embedded mode (`gortex mcp --index <path>
--no-daemon --server --http-addr ...`), as one long-lived subprocess josu's
daemon spawns and manages -- deliberately NOT gortex's daemon-tracked mode
(`gortex track`), whose built-in `fsnotify` watcher can't be turned off and
would silently reintroduce a second, uncontrolled reindex trigger alongside
josu's own commit/save/merge events. No `--watch` flag in the spawned argv
for the same reason -- josu's own event triggers (`graph/index.py`) are the
only thing that ever calls `GortexEngine.update()`.

Spawn conventions mirror `orchestrator/worktree.py`'s `_run_git()`: an argv
list (`shell=False`, never a shell string). Unlike a git subcommand
allowlist, there is exactly one binary this module ever spawns (`gortex`),
so the "allowlist" here is simply the hardcoded argv template itself.

Security: the spawned subprocess gets an explicit, minimal environment
(`PATH`/`HOME` only), never the daemon's full ambient environment. The
daemon is the one process that resolves remote-delegate API credentials
(`config/delegate.py`'s `api_key_env`) into its own environment at call
time (R31) -- gortex is an external, unaudited third-party binary with no
functional need for those variables, so default `subprocess.Popen`
environment inheritance would hand a compromised or malicious gortex build
a live credential-exfiltration path it has no reason to have.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_GORTEX_HOST = "127.0.0.1"
DEFAULT_GORTEX_PORT = 7411

# How long to wait for gortex to answer /healthz after spawning, before
# giving up and surfacing a clear daemon-start failure -- this bounds the
# subprocess-startup step, not gortex's own initial-index completion (see
# module docstring / plan Key Technical Decisions: daemon start blocks on
# /healthz only, never on full indexing).
DEFAULT_STARTUP_TIMEOUT_SECONDS = 15.0
_HEALTHZ_POLL_INTERVAL_SECONDS = 0.25

# How much of gortex's stderr to keep for a startup-failure error message --
# bounded so a verbose or runaway process can't grow this without limit.
_STDERR_TAIL_BYTES = 8_192


class GortexProcessError(RuntimeError):
    """Raised when spawning or health-checking the gortex subprocess fails
    -- mirrors `orchestrator/worktree.py`'s `WorktreeError` convention."""


@dataclass
class GortexProcess:
    """A spawned (or reused, if a validated survivor was already running)
    gortex subprocess. `popen` is `None` when this represents a REUSED
    process from a prior crash -- this process didn't spawn it and must
    never terminate a process it doesn't own (see `terminate_gortex()`)."""

    host: str
    port: int
    popen: subprocess.Popen[bytes] | None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _minimal_subprocess_env() -> dict[str, str]:
    """`PATH`/`HOME` only -- see module docstring's Security section. Not
    the daemon's full `os.environ`, which may carry resolved remote-delegate
    API credentials (R31) gortex has no functional need to see."""
    env: dict[str, str] = {}
    for key in ("PATH", "HOME"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def check_gortex_reachable(
    host: str, port: int, *, timeout: float = 5.0
) -> bool:
    """Probe `http://{host}:{port}/healthz`, validating the response as an
    authentic gortex liveness reply before treating it as a match.

    Unlike `delegate/daemon_client.py`'s `check_daemon_reachable()` (whose
    "any response, even a 404, proves a listener" bar is fine for a pure
    liveness check with no further consequence), this probe feeds a REUSE
    decision -- a validated survivor gets real graph queries routed to it
    (see `daemon.py`'s startup sequence). Reusing *anything* answering that
    port on the bare-GET bar is too permissive for that; a non-matching
    response is treated as "not gortex" (a likely port conflict), not
    silently reused.

    Returns `True` only for a 200 response with a JSON *object* body --
    gortex's exact `/healthz` schema is not pinned down further here
    (unconfirmed against the real binary at plan time; see plan Key
    Technical Decisions/Risks), so this stops short of checking for a
    gortex-specific field/signature. Returns `False` (never raises) for a
    non-matching response, a connection failure, or a timeout -- all three
    mean "no validated survivor here," which the caller treats identically.

    SECURITY (accepted residual risk, not fully closed): this is NOT
    authentication. Any local process that happens to be listening on
    `port` and answers `/healthz` with a 200 and a JSON object (e.g.
    `{}`) passes this check and gets reused as the daemon's graph engine
    for the whole session -- every subsequent `search`/`execute` call
    (carrying repository content and query text) is routed to it, and its
    responses flow back into the agent's context unfiltered. This is an
    active man-in-the-middle risk on the graph data path, not merely an
    eavesdropping one, if an attacker can win a port-squatting race before
    `josu daemon start` runs. Closing it properly needs gortex itself to
    support a caller-supplied shared secret on `/healthz` (or an
    equivalent authenticated liveness signal) -- not implementable from
    this side alone without that. Revisit once gortex's own auth story is
    confirmed.
    """
    try:
        response = httpx.get(f"http://{host}:{port}/healthz", timeout=timeout)
    except httpx.TransportError:
        return False
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(body, dict)


def spawn_gortex(
    root: Path,
    *,
    host: str = DEFAULT_GORTEX_HOST,
    port: int = DEFAULT_GORTEX_PORT,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> GortexProcess:
    """Spawn gortex in standalone/embedded mode over `root`, wait for
    `/healthz` to answer (bounded by `startup_timeout`), and return the
    handle.

    Before spawning, callers should already have checked
    `check_gortex_reachable()` against the same host/port -- this function
    always spawns a fresh subprocess; it does not itself check for or
    reuse a survivor (that decision belongs to `daemon.py`'s startup
    sequence, which owns the "surface a conflict, don't silently
    double-spawn" policy).

    Raises `GortexProcessError` if the `gortex` binary isn't on `PATH`
    (`FileNotFoundError` from `Popen`), if the process exits before
    `/healthz` ever answers, or if `startup_timeout` elapses first.
    """
    argv = [
        "gortex",
        "mcp",
        "--index",
        str(root.resolve()),
        "--no-daemon",
        "--server",
        "--http-addr",
        f"{host}:{port}",
    ]
    try:
        popen = subprocess.Popen(
            argv,
            shell=False,
            env=_minimal_subprocess_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GortexProcessError(
            "gortex executable not found on PATH -- install it "
            "(curl -fsSL https://get.gortex.dev | sh, or see "
            "https://github.com/zzet/gortex#installation) before starting the daemon"
        ) from exc

    # Drain stderr continuously on a background thread, into a bounded
    # tail buffer, for the whole life of the process -- not just read once
    # the poll loop below notices it exited. Without this, the OS pipe
    # buffer (a few tens of KB on most platforms) fills if gortex writes
    # more than that to stderr before /healthz ever answers (plausible for
    # a verbose startup/index-building log), gortex's own `write()` call
    # blocks, `popen.poll()` never reports an exit code because the
    # process is alive-but-stuck (not exited), and the whole startup_timeout
    # window burns on a gortex that was actually fine.
    stderr_tail = bytearray()

    def _drain_stderr() -> None:
        if popen.stderr is None:
            return
        for chunk in iter(lambda: popen.stderr.read(4096), b""):
            stderr_tail.extend(chunk)
            del stderr_tail[: max(0, len(stderr_tail) - _STDERR_TAIL_BYTES)]

    threading.Thread(target=_drain_stderr, daemon=True).start()

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        exit_code = popen.poll()
        if exit_code is not None:
            stderr = bytes(stderr_tail).decode("utf-8", errors="replace")
            raise GortexProcessError(
                f"gortex exited with code {exit_code} during startup: {stderr.strip()}"
            )
        if check_gortex_reachable(host, port, timeout=1.0):
            return GortexProcess(host=host, port=port, popen=popen)
        time.sleep(_HEALTHZ_POLL_INTERVAL_SECONDS)

    popen.terminate()
    raise GortexProcessError(
        f"gortex did not answer /healthz at {host}:{port} within {startup_timeout}s"
    )


def terminate_gortex(process: GortexProcess) -> None:
    """Terminate a gortex subprocess THIS process spawned -- a no-op for a
    reused survivor (`process.popen is None`), since a process this daemon
    didn't spawn is never this daemon's to kill (a non-clean exit leaves it
    for the next `daemon start`'s reuse/conflict check, per the plan's
    "reuse, not full supervision" v1 posture)."""
    if process.popen is None:
        return
    if process.popen.poll() is not None:
        return
    process.popen.terminate()
    try:
        process.popen.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.popen.kill()
