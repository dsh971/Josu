"""Shared-secret auth for the daemon's HTTP/SSE routes.

The daemon binds to loopback only by default, but loopback binding
authenticates nothing -- any other local process, or a malicious web page
open in the developer's browser (a well-documented vulnerability class
against other localhost-bound dev tools: Ollama, Jupyter, Docker Desktop),
can reach `/graph/sse`/`/delegate/sse`/the internal delegate route with no
credential at all, triggering delegate calls against the developer's
resolved remote-API credentials at their cost. A lightweight bearer token,
generated once and required on every route, closes that gap cheaply.

The token lives alongside `josu.toml` under the same XDG-style config
directory, `0600` -- the same trust level already established for the
config file itself (`config/__init__.py`'s module docstring). It is NOT a
replacement for a real multi-user auth system; it exists solely to
distinguish "a process that read this developer's own local config" from
"anything else that happened to be able to reach 127.0.0.1".
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DAEMON_TOKEN_FILENAME = "daemon.token"
_TOKEN_BYTES = 32


def resolve_daemon_token_path(config_dir: Path) -> Path:
    """`<config_dir>/daemon.token` -- a sibling of `josu.toml` under the
    same XDG-style directory `config/__init__.py`'s `resolve_config_path()`
    resolves."""
    return config_dir / DAEMON_TOKEN_FILENAME


def resolve_daemon_token(config_path: Path | None = None) -> str:
    """Convenience wrapper for CLI/hook call sites that just need "the
    token", without caring about the path plumbing: resolves the token path
    alongside `config_path` (or `config/__init__.py`'s own default XDG
    location if omitted) and loads/creates it. Safe to call even when the
    daemon hasn't started yet -- creates the file idempotently; the daemon
    itself will read the same file rather than generating a second one."""
    from josu.config import resolve_config_path

    resolved_config_path = config_path if config_path is not None else resolve_config_path()
    return load_or_create_daemon_token(resolve_daemon_token_path(resolved_config_path.parent))


def load_or_create_daemon_token(path: Path) -> str:
    """Read the existing token at `path`, or generate and persist a new one
    (`0600`, matching `config/__init__.py`'s own private-key-style
    permission convention for `josu.toml`) if none exists yet.

    Created atomically at `0600` -- `os.open(..., O_CREAT | O_EXCL, 0o600)`
    sets the restrictive mode as part of file creation itself, rather than
    a separate `write_text()` then `chmod()`. The write-then-chmod sequence
    an earlier version of this function used left a window (however brief)
    where the secret sat on disk at the process's default umask -- often
    group/world-readable -- on a multi-user machine, which is exactly the
    threat this module's own docstring names. A `FileExistsError` (another
    process won the race to create it first) falls back to reading
    whatever that process wrote, same as the plain "already exists" path.
    """
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return token


# The MCP SSE transport (`mcp.server.sse.SseServerTransport`) generates its
# own POST-message endpoint URL server-side (e.g.
# `/graph/messages/?session_id=<uuid>`), handed to the client only via the
# SSE stream itself -- our own token never rides along on that generated
# URL, and the MCP client library posts to it exactly as given. These
# paths are exempted from the token check below; they're independently
# protected by their own unguessable, freshly generated `session_id`,
# obtainable only by a client that already completed an authenticated SSE
# handshake (which DOES require the token) -- not a weaker boundary, a
# different one.
_SESSION_SCOPED_PATH_PREFIXES = ("/graph/messages/", "/delegate/messages/")


class DaemonAuthMiddleware:
    """Pure ASGI middleware (not `BaseHTTPMiddleware`, so it works for the
    SSE routes' long-lived streaming responses without buffering them)
    requiring the daemon's shared-secret token on every request, via either
    an `Authorization: Bearer <token>` header (the internal POST routes,
    reached by this codebase's own `httpx` clients, which can trivially set
    a header) or a `?token=` query parameter (the SSE routes' initial GET,
    reached by Claude Code's own MCP client via a bare URL --
    `mcp_manifest.py` embeds the token in the manifest's SSE URLs this way,
    since custom-header support for MCP's SSE server-config shape is not
    assumed). See `_SESSION_SCOPED_PATH_PREFIXES` for the one exemption.

    A request carrying neither, or the wrong token, gets a 401 JSON
    response and the app is never invoked -- constant-time compared via
    `secrets.compare_digest()` so response timing doesn't leak how many
    leading characters of a guessed token were correct.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if scope["path"].startswith(_SESSION_SCOPED_PATH_PREFIXES):
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        supplied = request.query_params.get("token")
        if supplied is None:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                supplied = auth_header[len("bearer ") :]

        if supplied is None or not secrets.compare_digest(supplied, self._token):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
