"""Tests for the internal delegate-routing HTTP endpoint (U14).

`build_delegate_internal_route()` is exercised directly over a real ASGI
transport (`httpx.AsyncClient(transport=httpx.ASGITransport(app=...))`) --
a real Starlette request/response round trip through real pydantic
validation and real `chain.execute_chain()`, with only the delegate CLIENT
(never the queue, the route, or `execute_chain()` itself) faked -- matching
this repo's "real integration over mocks, fakes only at the true I/O
boundary" convention (see `delegate/test_server.py`).
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.applications import Starlette
from starlette.requests import Request

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate.client import DelegateUnreachableError
from josu.delegate.cooldown import CandidateCooldownStore
from josu.delegate.internal_api import (
    DELEGATE_INTERNAL_PATH,
    MAX_BODY_BYTES,
    build_delegate_internal_route,
)
from josu.delegate.queue import DelegateQueue
from josu.delegate.server import build_server

TASK_TYPE = "file_summarization"


class FakeDelayedClient:
    """Fake `DelegateClient` recording call start/end times and (optionally)
    sleeping first, so tests can assert non-overlapping execution windows --
    mirrors `tests/delegate/test_queue.py`'s timing-based serialization
    proof, one layer up the stack."""

    def __init__(self, result="ok", caveats="", delay=0.0, calls=None):
        self._result = result
        self._caveats = caveats
        self._delay = delay
        self.calls = calls if calls is not None else []

    async def complete(self, *, model, messages, timeout):
        start = time.monotonic()
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append((start, time.monotonic()))
        return json.dumps({"result": self._result, "caveats": self._caveats})


class ExplodingClient:
    """A fake client that fails the test if ever called -- used to prove a
    candidate was never reached, stronger than merely inspecting code."""

    async def complete(self, *, model, messages, timeout):
        raise AssertionError("this candidate must never be contacted (R39)")


class CapturingClient:
    """Fake `DelegateClient` that records the exact `messages` payload
    `local_model.py`'s `delegate()` composed and passed to `complete()` --
    used to prove `scope_root`'s R13 file-context fallback actually fired,
    not just that the call succeeded."""

    def __init__(self, result="ok", caveats=""):
        self._result = result
        self._caveats = caveats
        self.received_messages: list[list[dict]] = []

    async def complete(self, *, model, messages, timeout):
        self.received_messages.append(messages)
        return json.dumps({"result": self._result, "caveats": self._caveats})


def _app_and_queue(*, chains_config=None, registry=None, client_factory=None, queue=None):
    queue = queue or DelegateQueue()
    chains_config = (
        chains_config
        if chains_config is not None
        else ChainsConfig(
            chains=[DelegationChain(task_type=TASK_TYPE, candidates=["c1"])],
            allow_remote=True,
        )
    )
    registry = (
        registry
        if registry is not None
        else {"c1": DelegateCandidate(name="c1", endpoint="http://example.invalid", local=True, model="m")}
    )
    route = build_delegate_internal_route(
        queue=queue,
        cooldown_store=CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30.0),
        chains_config=chains_config,
        registry=registry,
        client_factory=client_factory,
    )
    return Starlette(routes=[route]), queue


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal")


# --- happy paths -------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_type_shape_resolves_against_given_chains_config_and_registry():
    fake = FakeDelayedClient(result="summarized", caveats="none")
    app, _queue = _app_and_queue(client_factory=lambda c: fake)
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH, json={"task_type": TASK_TYPE, "task": "do it"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "summarized"
    assert body["caveats"] == "none"


@pytest.mark.asyncio
async def test_scope_with_path_triggers_r13_file_context_fallback_same_as_mcp_tool(tmp_path):
    """Simplify-pass regression test: `internal_api.py`'s handler must
    derive `scope_root` from `scope={"path": ...}` the same way
    `delegate/server.py`'s MCP tool (`call_tool()`) already does, so
    `local_model.py`'s R13 file-context fallback (`_fallback_file_context()`)
    fires identically whether a caller reaches `execute_chain()` through the
    MCP tool or through this HTTP route.

    Proven the same way the graph-miss fallback is proven elsewhere: no
    graph engine wired (so the graph lookup finds nothing), a real directory
    with files at `scope["path"]`, and the client-received `messages`
    payload actually containing those file paths in `context` -- not merely
    that the call succeeded.
    """
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")

    capturing = CapturingClient(result="scoped")
    app, _queue = _app_and_queue(client_factory=lambda c: capturing)

    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task_type": TASK_TYPE, "task": "do it", "scope": {"path": str(tmp_path)}},
        )

    assert response.status_code == 200
    assert len(capturing.received_messages) == 1
    user_content = json.loads(capturing.received_messages[0][1]["content"])
    assert user_content["context"] != []
    assert any(str(tmp_path / "a.py") == path for path in user_content["context"])


@pytest.mark.asyncio
async def test_candidates_shape_resolves_names_against_the_trusted_registry():
    """A `candidates`-shaped request now carries NAME strings, resolved
    against the daemon's own trusted `registry` (the same one passed to
    `build_delegate_internal_route()` at construction time) -- even though
    the daemon's real `chains_config` has no chain that would resolve this
    task type, proving the task-type resolution path is still bypassed
    (R39) while the candidate's actual endpoint/model identity now comes
    from the trusted registry, never the request body."""
    trusted_registry = {
        "given": DelegateCandidate(name="given", endpoint="http://example.invalid", local=True, model="m"),
    }
    real_chains_config = ChainsConfig(chains=[], allow_remote=True)  # nothing resolves here
    fake = FakeDelayedClient(result="from-candidate")
    app, _queue = _app_and_queue(
        chains_config=real_chains_config,
        registry=trusted_registry,
        client_factory=lambda c: fake,
    )
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "check", "candidates": ["given"]},
        )
    assert response.status_code == 200
    assert response.json()["result"] == "from-candidate"


@pytest.mark.asyncio
async def test_candidates_shape_rejects_full_candidate_objects_ssrf_fix():
    """SECURITY: the OLD request shape (a list of full `DelegateCandidate`
    objects, including a self-asserted `endpoint`/`api_key_env`/`local`)
    is no longer accepted at all -- `candidates` is now `list[str]`, so a
    request trying to smuggle `{"name": "x", "local": True, "endpoint":
    "http://attacker-controlled-host/collect", "api_key_env":
    "SOME_ENV_VAR_NAME"}` fails pydantic validation (400 invalid_request)
    instead of being trusted directly. This is the exact exploit shape the
    Tier 2 review flagged: without this fix, any local process could name
    an attacker endpoint, claim `local=True` to bypass R39's intent, and
    have `api_key_env` resolved and sent to that endpoint."""
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={
                "task": "check",
                "candidates": [
                    {
                        "name": "x",
                        "local": True,
                        "endpoint": "http://attacker-controlled-host/collect",
                        "api_key_env": "SOME_ENV_VAR_NAME",
                    }
                ],
            },
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_candidates_shape_excludes_a_name_that_is_remote_in_the_trusted_registry():
    """SECURITY (SSRF/credential-exfiltration fix): a `candidates` entry
    naming a real registry candidate that is actually `local=False` in the
    daemon's trusted config is excluded -- even though the request body
    itself never claims `local=True` anywhere (the field doesn't exist in
    the request shape at all anymore). Proven by wiring the remote
    candidate's client to raise if ever contacted, and its `endpoint`
    pointed at what would be an attacker-controlled host with an
    `api_key_env` naming a secret, to mirror the exact exploit scenario
    from the review -- none of it can be reached because `local=False` in
    the trusted registry excludes it regardless of what's requested."""
    exploding = ExplodingClient()
    local_fake = FakeDelayedClient(result="local-won")

    def factory(candidate):
        return exploding if candidate.name == "remote-bad" else local_fake

    trusted_registry = {
        "remote-bad": DelegateCandidate(
            name="remote-bad",
            endpoint="http://attacker-controlled-host/collect",
            api_key_env="SOME_ENV_VAR_NAME",
            local=False,
            model="m",
        ),
        "local-good": DelegateCandidate(name="local-good", endpoint="http://x", local=True, model="m"),
    }
    app, _queue = _app_and_queue(registry=trusted_registry, client_factory=factory)
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "check", "candidates": ["remote-bad", "local-good"]},
        )
    assert response.status_code == 200
    assert response.json()["result"] == "local-won"


@pytest.mark.asyncio
async def test_candidates_shape_excludes_an_unknown_name_without_crashing():
    """A `candidates` entry naming something absent from the trusted
    registry entirely is excluded gracefully -- mirroring
    `config/chains.py`'s `_lookup_candidates()` "unresolvable candidate is
    skipped, not an error" convention -- rather than crashing with a
    `KeyError` or otherwise raising."""
    local_fake = FakeDelayedClient(result="local-won")
    trusted_registry = {
        "local-good": DelegateCandidate(name="local-good", endpoint="http://x", local=True, model="m"),
    }
    app, _queue = _app_and_queue(registry=trusted_registry, client_factory=lambda c: local_fake)
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "check", "candidates": ["does-not-exist", "local-good"]},
        )
    assert response.status_code == 200
    assert response.json()["result"] == "local-won"


@pytest.mark.asyncio
async def test_candidates_shape_naming_only_remote_candidates_resolves_to_no_candidates_response():
    """[Testing gap 4a] A `candidates` request naming ONLY candidates that
    are present in the trusted registry but every one of them is
    `local=False` there must resolve to a structured "no candidates"
    (422) response -- not a crash, and not a silent success that somehow
    contacts a remote endpoint. Every named candidate is excluded by the
    same `local` filter `test_candidates_shape_excludes_a_name_that_is_
    remote_in_the_trusted_registry` proves for a mixed local/remote list;
    here NOTHING survives the filter, so the resulting throwaway
    `ChainsConfig`'s chain has an empty `candidates` list and `execute_chain()`
    must raise `NoCandidatesError`, which this route already translates to a
    422 for the `task_type` path -- this proves the SAME translation holds
    for the `candidates` path when it bottoms out empty."""
    exploding = ExplodingClient()
    trusted_registry = {
        "remote-one": DelegateCandidate(
            name="remote-one", endpoint="http://attacker-controlled-host/collect", local=False, model="m"
        ),
        "remote-two": DelegateCandidate(
            name="remote-two", endpoint="http://another-remote-host/collect", local=False, model="m"
        ),
    }
    app, _queue = _app_and_queue(registry=trusted_registry, client_factory=lambda c: exploding)
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "check", "candidates": ["remote-one", "remote-two"]},
        )
    assert response.status_code == 422
    assert response.json()["error"] == "no_candidates"


# --- errors -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_candidates_error_is_a_structured_422():
    app, _queue = _app_and_queue(chains_config=ChainsConfig(), registry={})
    async with _client(app) as client:
        response = await client.post(DELEGATE_INTERNAL_PATH, json={"task_type": "nope", "task": "x"})
    assert response.status_code == 422
    assert response.json()["error"] == "no_candidates"


@pytest.mark.asyncio
async def test_chain_exhausted_error_is_a_structured_502():
    class DeadClient:
        async def complete(self, *, model, messages, timeout):
            raise DelegateUnreachableError("dead")

    app, _queue = _app_and_queue(client_factory=lambda c: DeadClient())
    async with _client(app) as client:
        response = await client.post(DELEGATE_INTERNAL_PATH, json={"task_type": TASK_TYPE, "task": "x"})
    assert response.status_code == 502
    assert response.json()["error"] == "chain_exhausted"


@pytest.mark.asyncio
async def test_malformed_json_body_returns_structured_400_not_unhandled_exception():
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


@pytest.mark.asyncio
async def test_oversized_body_returns_structured_413_not_unhandled_exception():
    app, _queue = _app_and_queue()
    huge_task = "x" * (MAX_BODY_BYTES + 1000)
    async with _client(app) as client:
        response = await client.post(DELEGATE_INTERNAL_PATH, json={"task_type": TASK_TYPE, "task": huge_task})
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


@pytest.mark.asyncio
async def test_oversized_body_with_content_length_header_is_rejected_without_reading_body():
    """Simplify-pass regression test: an oversized request that DECLARES its
    size via `Content-Length` must be rejected before the handler ever calls
    `request.body()` -- proven by handing the handler a `Request` whose
    `receive` callable raises if invoked at all, so any attempt to buffer
    the body would fail the test rather than merely being slow."""
    route = build_delegate_internal_route(
        queue=DelegateQueue(),
        cooldown_store=CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30.0),
        chains_config=ChainsConfig(
            chains=[DelegationChain(task_type=TASK_TYPE, candidates=["c1"])],
            allow_remote=True,
        ),
        registry={"c1": DelegateCandidate(name="c1", endpoint="http://example.invalid", local=True, model="m")},
    )

    class ExplodingReceive:
        """An ASGI `receive` callable that fails the test if the handler
        ever tries to read the request body."""

        async def __call__(self):
            raise AssertionError(
                "request.body() must not be called when Content-Length already exceeds the cap"
            )

    oversized_length = MAX_BODY_BYTES + 1000
    scope = {
        "type": "http",
        "method": "POST",
        "path": DELEGATE_INTERNAL_PATH,
        "headers": [(b"content-length", str(oversized_length).encode())],
    }
    request = Request(scope, receive=ExplodingReceive())

    response = await route.endpoint(request)

    assert response.status_code == 413
    assert json.loads(bytes(response.body))["error"] == "request_too_large"


@pytest.mark.asyncio
async def test_chunked_request_with_no_content_length_exceeding_cap_is_rejected_without_buffering():
    """SECURITY (P1 DoS fix): a request with NO `Content-Length` header at
    all (the real shape of a chunked-transfer-encoding request, where the
    sender never declares a total size upfront) must be rejected as soon as
    the handler has accumulated more than `MAX_BODY_BYTES` -- proven here by
    a fake ASGI `receive` callable that tracks how many times it's invoked
    and how many bytes it has handed back, and asserts the handler stopped
    pulling more chunks (and thus never held the full oversized body in
    memory) once the cap was exceeded."""
    route = build_delegate_internal_route(
        queue=DelegateQueue(),
        cooldown_store=CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30.0),
        chains_config=ChainsConfig(
            chains=[DelegationChain(task_type=TASK_TYPE, candidates=["c1"])],
            allow_remote=True,
        ),
        registry={"c1": DelegateCandidate(name="c1", endpoint="http://example.invalid", local=True, model="m")},
    )

    class ChunkedReceive:
        """An ASGI `receive` callable simulating a chunked-transfer-encoding
        body with no declared total size: it hands back one 64KB chunk per
        call, `more_body=True` every time, and would happily keep going
        forever if asked -- standing in for an attacker streaming an
        unbounded body. Tracks every call so the test can assert the
        handler stopped asking for more well short of actually reading the
        whole (unbounded) stream."""

        CHUNK_SIZE = 64 * 1024

        def __init__(self) -> None:
            self.call_count = 0
            self.bytes_handed_back = 0

        async def __call__(self):
            self.call_count += 1
            self.bytes_handed_back += self.CHUNK_SIZE
            return {
                "type": "http.request",
                "body": b"x" * self.CHUNK_SIZE,
                "more_body": True,
            }

    receive = ChunkedReceive()
    scope = {
        "type": "http",
        "method": "POST",
        "path": DELEGATE_INTERNAL_PATH,
        # No content-length header at all -- the chunked-transfer shape.
        "headers": [(b"content-type", b"application/json")],
    }
    request = Request(scope, receive=receive)

    response = await route.endpoint(request)

    assert response.status_code == 413
    assert json.loads(bytes(response.body))["error"] == "request_too_large"
    # The handler must have stopped well before accumulating anywhere near
    # the full amount `ChunkedReceive` was willing to hand back -- proving
    # it aborted incrementally rather than buffering the whole (unbounded)
    # body first. A generous bound: enough calls to exceed MAX_BODY_BYTES,
    # never anywhere close to "however many it takes to hang forever".
    assert receive.bytes_handed_back < MAX_BODY_BYTES + (ChunkedReceive.CHUNK_SIZE * 2)
    assert receive.call_count < (MAX_BODY_BYTES // ChunkedReceive.CHUNK_SIZE) + 2


@pytest.mark.asyncio
async def test_missing_content_type_is_rejected_with_structured_415():
    """SECURITY (P1 CSRF-hardening fix): a request with NO `Content-Type`
    header at all -- the shape a browser's "simple request" (no CORS
    preflight) can send -- is rejected with 415, not processed as JSON."""
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            content=json.dumps({"task_type": TASK_TYPE, "task": "x"}).encode("utf-8"),
        )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_non_json_content_type_is_rejected_with_structured_415():
    """SECURITY: a non-JSON Content-Type (e.g. `text/plain`, another shape
    a browser's simple-request CSRF path can send without a preflight) is
    also rejected with 415."""
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            content=json.dumps({"task_type": TASK_TYPE, "task": "x"}).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_missing_both_task_type_and_candidates_is_a_structured_400():
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(DELEGATE_INTERNAL_PATH, json={"task": "x"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_both_task_type_and_candidates_given_is_a_structured_400():
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "x", "task_type": TASK_TYPE, "candidates": ["c1"]},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_timeout", [0, -1, 601])
async def test_invalid_timeout_is_rejected_with_a_structured_400(bad_timeout):
    """Code-review fix: an unbounded `timeout` let a caller weaponize the
    shared `cooldown_store` -- one request with a near-zero timeout against
    a multi-candidate chain could fail every candidate almost instantly and
    trip all of them into cooldown for every other caller. Non-positive and
    too-large values are now rejected before `execute_chain()` is ever
    called."""
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "x", "task_type": TASK_TYPE, "timeout": bad_timeout},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
async def test_non_finite_timeout_is_rejected_with_a_structured_400(literal):
    """`json.loads()` accepts bare `NaN`/`Infinity`/`-Infinity` literals by
    default (a non-standard Python extension), so this must be exercised
    via a raw request body -- httpx's own `json=` convenience parameter
    encodes with `allow_nan=False` and would raise client-side before the
    request is even sent, never reaching the server's own validation."""
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            content=b'{"task": "x", "task_type": "%s", "timeout": %s}'
            % (TASK_TYPE.encode(), literal),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


# --- shared-queue serialization proofs ----------------------------------------


@pytest.mark.asyncio
async def test_task_type_and_candidates_requests_are_serialized_through_one_queue():
    """The core U14 concurrency proof: a `task_type`-shaped request (the
    `josu delegate` case) and a `candidates`-shaped request (the
    commit-hook case) issued CONCURRENTLY against the same route/queue
    never overlap in execution -- proving both are served through the ONE
    shared `DelegateQueue`, not two independent instances racing."""
    calls: list[tuple[float, float]] = []
    fake = FakeDelayedClient(delay=0.1, calls=calls)
    # "c1" is the default registry's own local candidate (see
    # `_app_and_queue()`) -- reused here as the `candidates`-shape request's
    # NAME, since candidates are now looked up by name against the trusted
    # registry rather than carried as full objects in the request body.
    app, _queue = _app_and_queue(client_factory=lambda c: fake)

    async with _client(app) as client:
        responses = await asyncio.gather(
            client.post(DELEGATE_INTERNAL_PATH, json={"task_type": TASK_TYPE, "task": "a"}),
            client.post(
                DELEGATE_INTERNAL_PATH,
                json={"task": "b", "candidates": ["c1"]},
            ),
        )

    assert all(r.status_code == 200 for r in responses)
    assert len(calls) == 2
    (start_a, end_a), (start_b, end_b) = sorted(calls)
    assert start_b >= end_a


@pytest.mark.asyncio
async def test_mcp_tool_and_internal_route_share_one_lock_real_race():
    """Proves `build_server()`'s MCP tool (`delegate_to_local`) and
    `build_delegate_internal_route()`'s HTTP route, when constructed with
    the SAME `DelegateQueue` instance (as `daemon.py`'s `create_app()`
    does), genuinely serialize against each other -- a real concurrent race
    between an MCP call_tool session and an HTTP request, not a
    code-inspection claim."""
    calls: list[tuple[float, float]] = []
    fake = FakeDelayedClient(delay=0.1, calls=calls)
    shared_queue = DelegateQueue()
    chains_config = ChainsConfig(
        chains=[DelegationChain(task_type=TASK_TYPE, candidates=["c1"])],
        allow_remote=True,
    )
    registry = {"c1": DelegateCandidate(name="c1", endpoint="http://x", local=True, model="m")}

    shared_cooldown_store = CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30.0)
    mcp_server = build_server(
        chains_config=chains_config,
        registry=registry,
        client_factory=lambda c: fake,
        queue=shared_queue,
        cooldown_store=shared_cooldown_store,
    )
    route = build_delegate_internal_route(
        queue=shared_queue,
        cooldown_store=shared_cooldown_store,
        chains_config=chains_config,
        registry=registry,
        client_factory=lambda c: fake,
    )
    app = Starlette(routes=[route])

    async def _call_mcp_tool():
        async with create_connected_server_and_client_session(mcp_server) as session:
            return await session.call_tool("delegate_to_local", {"task": "mcp-call", "task_type": TASK_TYPE})

    async def _call_http():
        async with _client(app) as client:
            return await client.post(DELEGATE_INTERNAL_PATH, json={"task_type": TASK_TYPE, "task": "http-call"})

    _mcp_result, http_result = await asyncio.gather(_call_mcp_tool(), _call_http())

    assert http_result.status_code == 200
    assert len(calls) == 2
    (start_a, end_a), (start_b, end_b) = sorted(calls)
    assert start_b >= end_a
