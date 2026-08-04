"""Shared request-reading guards for the daemon's internal POST routes
(`delegate/internal_api.py`, `graph/internal_api.py`).

Both routes need the identical bounded-body-read contract: reject an
oversized request before ever buffering it, require a real
`application/json` Content-Type, and decode the body as JSON -- this was
previously hand-copied into each route's own `handle()` closure (including
a prior standalone security fix, "close chunked-DoS gap, add Content-Type
check", applied once and not the other). Factored here so any future fix
to this logic happens in one place, and any future internal route gets the
guarding for free.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse


def error_response(error: str, detail: Any, *, status_code: int) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status_code)


async def read_bounded_json_body(
    request: Request, *, max_bytes: int
) -> dict[str, Any] | JSONResponse:
    """Read, size-bound, and JSON-decode `request`'s body.

    Returns the decoded body (a `dict`) on success, or a ready-to-return
    `JSONResponse` error on any guard failure -- callers should check
    `isinstance(result, JSONResponse)` and `return result` directly from
    their own route handler when it is.

    Guards, in order:
    1. Declared `Content-Length` checked FIRST, before ever reading the
       body -- an oversized request is rejected without buffering it into
       memory at all. Only a fast path for the common case where the
       header IS present and already tells us enough to reject early; NOT
       a substitute for the streaming cap below, since a declared size
       isn't guaranteed to be present (chunked transfer-encoding sends
       none at all) or honest.
    2. SECURITY (defense-in-depth, not a complete CSRF fix): requires a
       real `application/json` Content-Type. These routes are reachable by
       anything on the loopback interface, which includes a browser tab
       that got a victim to issue a request to `localhost` -- a "simple
       request" (no-CORS-preflight) form submission or `fetch()` call can
       send NO Content-Type, or a non-JSON one (`text/plain`,
       `multipart/form-data`, `application/x-www-form-urlencoded`) without
       triggering a CORS preflight, but CANNOT set an arbitrary
       Content-Type without one. Requiring `application/json` here means a
       browser attempting that attack either can't reach this branch at
       all, or must first trigger a preflight the browser will actually
       enforce. This does NOT fully close the CSRF surface on its own -- a
       real `application/json` XHR/fetch from a malicious page is still
       possible in some configurations (no CORS response headers are
       checked before the request lands here) -- it only raises the bar
       against the cheap, no-preflight "simple request" shape.
    3. The body is read INCREMENTALLY via `request.stream()`, never
       `request.body()` -- a request that declares no `Content-Length` at
       all (chunked transfer-encoding) must still never be fully buffered
       into memory before the size check runs. Aborts with the same 413
       the declared-length fast path above uses as soon as accumulated
       bytes exceed `max_bytes`, so an oversized chunked body is never
       held in memory in full, regardless of whether a (dishonest or
       absent) `Content-Length` header was present.
    """
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_bytes = int(declared_length)
        except ValueError:
            declared_bytes = None
        if declared_bytes is not None and declared_bytes > max_bytes:
            return error_response(
                "request_too_large",
                f"request body exceeds the maximum of {max_bytes} bytes",
                status_code=413,
            )

    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        return error_response(
            "unsupported_media_type", "Content-Type must be application/json", status_code=415
        )

    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            return error_response(
                "request_too_large",
                f"request body exceeds the maximum of {max_bytes} bytes",
                status_code=413,
            )
        chunks.append(chunk)
    body_bytes = b"".join(chunks)

    try:
        return json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return error_response("invalid_json", str(exc), status_code=400)
