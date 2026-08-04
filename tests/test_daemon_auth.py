"""Tests for the daemon's shared-secret auth (`daemon_auth.py`)."""

from __future__ import annotations

import stat

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from josu.daemon_auth import (
    DaemonAuthMiddleware,
    load_or_create_daemon_token,
    resolve_daemon_token_path,
)


def test_load_or_create_daemon_token_creates_with_restrictive_permissions(tmp_path):
    path = resolve_daemon_token_path(tmp_path)
    token = load_or_create_daemon_token(path)
    assert token
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_load_or_create_daemon_token_is_idempotent(tmp_path):
    path = resolve_daemon_token_path(tmp_path)
    first = load_or_create_daemon_token(path)
    second = load_or_create_daemon_token(path)
    assert first == second


async def _handler(request):
    return PlainTextResponse("ok")


def _build_app(token: str) -> DaemonAuthMiddleware:
    app = Starlette(routes=[Route("/thing", endpoint=_handler)])
    return DaemonAuthMiddleware(app, token)


@pytest.mark.asyncio
async def test_request_with_no_token_is_rejected():
    app = _build_app("secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/thing")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_request_with_wrong_token_is_rejected():
    app = _build_app("secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/thing?token=wrong")
        assert response.status_code == 401
        response = await client.get(
            "/thing", headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_request_with_correct_query_token_is_allowed():
    app = _build_app("secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/thing?token=secret-token")
    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_request_with_correct_bearer_header_is_allowed():
    app = _build_app("secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/thing", headers={"Authorization": "Bearer secret-token"}
        )
    assert response.status_code == 200
