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

from josu.config.chains import ChainsConfig, DelegationChain
from josu.config.delegate import DelegateCandidate
from josu.delegate.client import DelegateUnreachableError
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
async def test_candidates_shape_never_touches_the_daemons_real_config():
    """A `candidates`-shaped request resolves entirely from the candidates
    given in the body -- even though the daemon's real `chains_config`/
    `registry` (passed to `build_delegate_internal_route`) has nothing that
    would resolve this task, proving the real config is never consulted for
    this request shape."""
    real_registry = {
        "unrelated": DelegateCandidate(name="unrelated", endpoint="http://example.invalid", local=True, model="m")
    }
    real_chains_config = ChainsConfig(chains=[], allow_remote=True)  # nothing resolves here
    fake = FakeDelayedClient(result="from-candidate")
    given_candidate = DelegateCandidate(name="given", endpoint="http://example.invalid", local=True, model="m")
    app, _queue = _app_and_queue(
        chains_config=real_chains_config,
        registry=real_registry,
        client_factory=lambda c: fake,
    )
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "check", "candidates": [given_candidate.model_dump()]},
        )
    assert response.status_code == 200
    assert response.json()["result"] == "from-candidate"


@pytest.mark.asyncio
async def test_candidates_shape_defensively_drops_non_local_candidates():
    """R39's server-side belt-and-suspenders: even if a `candidates` list
    (which should already be local-only by the time a well-behaved caller
    builds it) contains a remote candidate, the handler filters it out
    before ever calling `execute_chain` -- proven by a client that raises if
    the remote candidate is ever contacted, with only the local one wired
    to succeed."""
    local_fake = FakeDelayedClient(result="local-won")
    exploding = ExplodingClient()

    def factory(candidate):
        return exploding if candidate.name == "remote-bad" else local_fake

    app, _queue = _app_and_queue(client_factory=factory)
    remote = DelegateCandidate(name="remote-bad", endpoint="https://api.example.invalid", local=False, model="m")
    local = DelegateCandidate(name="local-good", endpoint="http://x", local=True, model="m")
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "check", "candidates": [remote.model_dump(), local.model_dump()]},
        )
    assert response.status_code == 200
    assert response.json()["result"] == "local-won"


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
async def test_missing_both_task_type_and_candidates_is_a_structured_400():
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(DELEGATE_INTERNAL_PATH, json={"task": "x"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_both_task_type_and_candidates_given_is_a_structured_400():
    candidate = DelegateCandidate(name="c", endpoint="http://x", local=True, model="m")
    app, _queue = _app_and_queue()
    async with _client(app) as client:
        response = await client.post(
            DELEGATE_INTERNAL_PATH,
            json={"task": "x", "task_type": TASK_TYPE, "candidates": [candidate.model_dump()]},
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
    app, _queue = _app_and_queue(client_factory=lambda c: fake)

    local_candidate = DelegateCandidate(name="c2", endpoint="http://x", local=True, model="m")

    async with _client(app) as client:
        responses = await asyncio.gather(
            client.post(DELEGATE_INTERNAL_PATH, json={"task_type": TASK_TYPE, "task": "a"}),
            client.post(
                DELEGATE_INTERNAL_PATH,
                json={"task": "b", "candidates": [local_candidate.model_dump()]},
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

    mcp_server = build_server(
        chains_config=chains_config,
        registry=registry,
        client_factory=lambda c: fake,
        queue=shared_queue,
    )
    route = build_delegate_internal_route(
        queue=shared_queue,
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
