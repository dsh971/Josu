"""Gortex connection-target lifecycle: liveness probe only.

josu's daemon connects to gortex as a pure client -- it never installs,
spawns, or authenticates a gortex process itself. The target (host/port,
optionally an `api_key_env`-sourced credential) is declared in josu's config
(`config/graph_engines.py`'s `[[graph.engines]]`), and the user runs and
manages that gortex daemon themselves (`gortex daemon start --http-addr
<host:port> --tools facade-v1 [--http-auth-token <token>]`), the same way
they already install and run the hosted CLI agent josu drives.

This module's only remaining job is checking whether a configured target is
actually there (`check_gortex_reachable()`) -- there is no spawn/terminate
lifecycle to own anymore. `GortexProcess` is kept as the shared value both
`daemon.py` and `graph/gortex.py` pass around, but it's now just resolved
connection info, not a subprocess handle.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

import httpx

from josu.config.graph_engines import _LOOPBACK_HOSTS

# The version this plan's flags/tool-surface facts were verified against.
# Below this floor, josu's assumptions about gortex's CLI/tool surface are
# confirmed-broken (the pre-fix `--http-addr`-on-`mcp` invocation, etc.) --
# above the ceiling is untested-but-plausibly-fine, not confirmed-broken,
# so it only warns. Bump the ceiling (never the floor without re-verifying
# live) as newer versions are checked.
MIN_COMPATIBLE_GORTEX_VERSION = (0, 62, 0)
MAX_KNOWN_GORTEX_VERSION = (0, 62, 0)

_VERSION_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _minimal_subprocess_env() -> dict[str, str]:
    """PATH/HOME only -- the local `gortex version` check needs nothing
    else, and the daemon's ambient environment carries resolved delegate
    API credentials (`api_key_env`) that an unaudited third-party `gortex`
    binary on PATH has no reason to see."""
    return {
        key: value for key in ("PATH", "HOME") if (value := os.environ.get(key)) is not None
    }


class GortexProcessError(RuntimeError):
    """Raised for a gortex connection-target problem that should surface as
    a clear, actionable message rather than a raw traceback (e.g. at the
    `josu daemon start` CLI boundary)."""


@dataclass
class GortexProcess:
    """A gortex connection target's resolved host/port and, optionally, a
    bearer token resolved from the target's configured `api_key_env` (see
    `config/graph_engines.py`) -- see module docstring. No longer carries a
    subprocess handle; josu never owns a gortex process to terminate."""

    host: str
    port: int
    auth_token: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def check_gortex_reachable(
    host: str, port: int, *, auth_token: str | None = None, timeout: float = 5.0
) -> bool:
    """Probe `http://{host}:{port}/healthz`, validating the response as an
    authentic gortex liveness reply before treating it as a match.

    Unlike `delegate/daemon_client.py`'s `check_daemon_reachable()` (whose
    "any response, even a 404, proves a listener" bar is fine for a pure
    liveness check with no further consequence), this probe feeds a
    "usable as the graph engine for this session" decision -- a validated
    target gets real graph queries routed to it. Reusing *anything*
    answering that port on the bare-GET bar is too permissive for that; a
    non-matching response is treated as "not gortex" (a likely port
    conflict or stale config), not silently used.

    Returns `True` only for a 200 response with a JSON *object* body --
    gortex's exact `/healthz` schema is not pinned down further here (its
    real body carries `status`/`transport`/`spec` keys, confirmed live
    against v0.62.0, but this check stays intentionally loose rather than
    coupling to exact field names that might drift), so this stops short
    of checking for a gortex-specific field/signature. Returns `False`
    (never raises) for a non-matching response, a connection failure, or a
    timeout -- all three mean "no usable target here," which the caller
    treats identically.

    SECURITY (accepted residual risk, not fully closed): this is NOT
    authentication. Any local (or, since the target host is now
    user-configured and may be non-loopback, remote) process that happens
    to be listening on `port` and answers `/healthz` with a 200 and a JSON
    object (e.g. `{}`) passes this check and gets used as the daemon's
    graph engine for the whole session -- every subsequent `search`/
    `execute` call (carrying repository content and query text) is routed
    to it, and its responses flow back into the agent's context
    unfiltered. This is an active man-in-the-middle risk on the graph data
    path, not merely an eavesdropping one, if an attacker can win a
    port-squatting race (or, for a non-loopback target, sit on the
    network path) before this check runs. Closing it properly needs
    gortex itself to support a caller-supplied shared secret on `/healthz`
    (or an equivalent authenticated liveness signal) -- not implementable
    from this side alone without that. Revisit once gortex's own auth
    story is confirmed.
    """
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        response = httpx.get(
            f"http://{host}:{port}/healthz", headers=headers, timeout=timeout
        )
    except (httpx.TransportError, httpx.InvalidURL):
        return False
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(body, dict)


def check_gortex_version_compatible(
    host: str = "127.0.0.1", *, timeout: float = 5.0
) -> tuple[bool, str]:
    """Best-effort local `gortex version` check.

    No endpoint on gortex's own HTTP/tool surface reports its binary
    version -- confirmed live against the real `facade-v1` tool surface:
    `capabilities` reports only the tool-PRESET version ("facade-v1"),
    never the daemon's own semver, and no other domain/operation exposes
    it either. This falls back to shelling out to a LOCAL `gortex` binary,
    which only works when the configured target happens to be on the same
    machine as josu's daemon -- so this check is skipped entirely (treated
    as compatible, no detail) for a non-loopback `host`: a local binary is
    not evidence about a remote daemon's version in either direction.

    Returns `(compatible, detail)`. `compatible` is `True` whenever the
    check can't be run at all (binary absent locally -- plausible for a
    genuinely remote target, not itself evidence of incompatibility, or
    version output that doesn't parse), the target is non-loopback, or
    the local version is within range; `False` only when a LOCAL gortex
    binary positively reports a version below `MIN_COMPATIBLE_GORTEX_VERSION`
    for a loopback target. `detail` is a non-empty, human-readable
    explanation whenever `compatible` is `False`, or when the check ran
    but wants to surface a non-blocking note (e.g. a newer version than
    verified); empty otherwise.
    """
    if host not in _LOOPBACK_HOSTS:
        return True, ""
    try:
        result = subprocess.run(
            ["gortex", "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_minimal_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return True, ""

    match = _VERSION_PATTERN.search(result.stdout)
    if match is None:
        return True, ""

    version = tuple(int(part) for part in match.groups())
    if version < MIN_COMPATIBLE_GORTEX_VERSION:
        return False, (
            f"local gortex reports version {'.'.join(map(str, version))}, below the "
            f"minimum compatible version {'.'.join(map(str, MIN_COMPATIBLE_GORTEX_VERSION))} "
            "this integration was verified against -- upgrade gortex before pointing "
            "josu at it"
        )
    if version > MAX_KNOWN_GORTEX_VERSION:
        return True, (
            f"local gortex reports version {'.'.join(map(str, version))}, newer than the "
            f"last version this integration was verified against "
            f"({'.'.join(map(str, MAX_KNOWN_GORTEX_VERSION))}) -- proceeding, but gortex's "
            "CLI/tool surface has moved before and could again"
        )
    return True, ""


def check_gortex_tool_surface_capable(
    host: str, port: int, *, auth_token: str | None = None, timeout: float = 5.0
) -> tuple[bool, str]:
    """POST a harmless `search` call to the configured target and check
    for a `tool_blocked_by_mode` error -- confirms the target was actually
    started with the `--tools facade-v1` preset `GortexEngine` needs, not
    gortex's default `core` preset (which blocks `search` entirely).

    Returns `(capable, detail)`. `capable` is `False` for any failure --
    unreachable, a non-2xx response, a malformed body, or the specific
    `tool_blocked_by_mode` error -- with `detail` naming the exact fix in
    the `tool_blocked_by_mode` case (a target this check can't reach or
    parse isn't itself josu's to fix, so `detail` for those cases stays a
    plain description, not fix guidance for a problem outside josu's
    control).
    """
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        response = httpx.post(
            f"http://{host}:{port}/v1/tools/search",
            json={"query": "gortex-capability-probe", "limit": 1},
            headers=headers,
            timeout=timeout,
        )
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        return False, f"could not reach {host}:{port} to check tool-surface capability: {exc}"

    if response.status_code >= 400:
        return False, f"HTTP {response.status_code} probing tool-surface capability at {host}:{port}"

    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, f"tool-surface capability probe at {host}:{port} returned a non-JSON body"

    if not isinstance(body, dict):
        return False, f"tool-surface capability probe at {host}:{port} returned a non-object body"

    if body.get("isError"):
        content = body.get("content")
        text = (
            content[0].get("text", "")
            if isinstance(content, list) and content and isinstance(content[0], dict)
            else ""
        )
        if "tool_blocked_by_mode" in text:
            return False, (
                f"the gortex daemon at {host}:{port} is missing the required tool preset -- "
                "restart it with `--tools facade-v1`"
            )
        return False, f"tool-surface capability probe at {host}:{port} returned an error: {text or body}"

    return True, ""
