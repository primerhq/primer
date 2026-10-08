"""The app-wide request-body size limit (FS-05).

Every route used to buffer whatever body it was sent: a PUT of workspace
files, a service publish and an audio upload all read arbitrarily large
bodies into memory. ``BodySizeLimitMiddleware`` refuses a body over the
configured cap with a 413 problem+json, whether the client declares the
size up front (``Content-Length``) or streams it chunked; the two routes
that legitimately take big bodies carry their own, larger caps.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport

from primer.api.app import create_app
from primer.api.config import AppConfig, RequestLimitsConfig
from primer.api.errors import PROBLEM_JSON_MEDIA_TYPE, register_error_handlers
from primer.api.middleware.body_limit import BodySizeLimitMiddleware, body_limit_options


def _app(limits: RequestLimitsConfig) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, **body_limit_options(limits))
    register_error_handlers(app)

    @app.post("/v1/echo")
    async def echo(request: Request) -> dict:
        return {"n": len(await request.body())}

    @app.put("/v1/workspaces/{wid}/files")
    async def put_file(request: Request) -> dict:
        return {"n": len(await request.body())}

    @app.post("/v1/services/{sid}/versions")
    async def publish(request: Request) -> dict:
        return {"n": len(await request.body())}

    return app


_LIMITS = RequestLimitsConfig(
    max_body_bytes=100,
    workspace_file_max_body_bytes=1000,
    service_publish_max_body_bytes=500,
)


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_413(r: httpx.Response, limit: int) -> None:
    assert r.status_code == 413, r.text
    assert r.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    body = r.json()
    assert body["type"] == "/errors/payload-too-large"
    assert body["status"] == 413
    assert body["extensions"]["code"] == "payload_too_large"
    assert body["extensions"]["limit_bytes"] == limit


async def test_a_body_at_the_cap_is_accepted() -> None:
    async with await _client(_app(_LIMITS)) as c:
        r = await c.post("/v1/echo", content=b"x" * 100)
    assert r.status_code == 200, r.text
    assert r.json() == {"n": 100}


async def test_a_declared_content_length_over_the_cap_is_refused() -> None:
    async with await _client(_app(_LIMITS)) as c:
        r = await c.post("/v1/echo", content=b"x" * 101)
    _assert_413(r, 100)


async def test_a_streamed_body_over_the_cap_is_refused() -> None:
    """No Content-Length (chunked): the bytes actually received are counted."""

    async def chunks():
        for _ in range(5):
            yield b"x" * 40

    async with await _client(_app(_LIMITS)) as c:
        r = await c.post("/v1/echo", content=chunks())
    _assert_413(r, 100)


async def test_the_workspace_file_put_has_its_own_cap() -> None:
    async with await _client(_app(_LIMITS)) as c:
        ok = await c.put("/v1/workspaces/w1/files", content=b"x" * 900)
        big = await c.put("/v1/workspaces/w1/files", content=b"x" * 1001)
        # The override is the PUT only: a POST on the same path keeps the default cap.
        other = await c.post("/v1/workspaces/w1/files", content=b"x" * 900)
    assert ok.status_code == 200, ok.text
    _assert_413(big, 1000)
    _assert_413(other, 100)


async def test_the_service_publish_has_its_own_cap() -> None:
    async with await _client(_app(_LIMITS)) as c:
        ok = await c.post("/v1/services/s1/versions", content=b"x" * 400)
        big = await c.post("/v1/services/s1/versions", content=b"x" * 501)
    assert ok.status_code == 200, ok.text
    _assert_413(big, 500)


def test_the_defaults_are_sane_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = AppConfig().limits
    assert limits.max_body_bytes == 32 * 1024 * 1024
    assert limits.workspace_file_max_body_bytes == 128 * 1024 * 1024
    assert limits.service_publish_max_body_bytes == 128 * 1024 * 1024
    monkeypatch.setenv("PRIMER_LIMITS__MAX_BODY_BYTES", "1234")
    assert AppConfig().limits.max_body_bytes == 1234


async def test_create_app_installs_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production factory refuses an oversized body before any route runs."""
    monkeypatch.setattr("primer.api.app._install_jsx_bundle", lambda *a, **k: None)
    app = create_app(AppConfig(limits=RequestLimitsConfig(max_body_bytes=10)))
    async with await _client(app) as c:
        r = await c.post("/v1/collections/c1/import", content=b"x" * 11)
    _assert_413(r, 10)


async def test_create_test_app_installs_the_default_limit(app: FastAPI) -> None:
    """The ``app`` fixture is built by ``create_test_app``."""
    found = [m for m in app.user_middleware if m.cls is BodySizeLimitMiddleware]
    assert len(found) == 1
    assert found[0].kwargs == body_limit_options(RequestLimitsConfig())
