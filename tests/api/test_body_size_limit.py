"""The app-wide request-body size limit (FS-05).

Every route used to buffer whatever body it was sent: a PUT of workspace
files, a service publish and an audio upload all read arbitrarily large
bodies into memory. ``BodySizeLimitMiddleware`` refuses a body over the
configured cap with a 413 problem+json, whether the client declares the
size up front (``Content-Length``) or streams it chunked; the two routes
that legitimately take big bodies carry their own, larger caps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport

from primer.api.app import create_app
from primer.api.config import AppConfig, RequestLimitsConfig
from primer.api.errors import PROBLEM_JSON_MEDIA_TYPE, register_error_handlers
from primer.api.middleware.body_limit import (
    BodySizeLimitMiddleware,
    _NO_AUTH_LAYER,
    body_limit_options,
)
from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.model.api_token import ApiToken
from primer.model.storage import OffsetPage
from primer.model.user import User


def _app(limits: RequestLimitsConfig) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, **body_limit_options(limits))
    register_error_handlers(app)

    @app.post("/v1/echo")
    async def echo(request: Request) -> dict:
        return {"n": len(await request.body())}

    @app.post("/v1/noread")
    async def noread() -> dict:
        return {"ok": True}

    app.mount("/v1/raw", _draining_asgi_app)

    @app.put("/v1/workspaces/{wid}/files")
    async def put_file(request: Request) -> dict:
        return {"n": len(await request.body())}

    @app.post("/v1/services/{sid}/versions")
    async def publish(request: Request) -> dict:
        return {"n": len(await request.body())}

    return app


async def _draining_asgi_app(scope, receive, send) -> None:
    """A bare ASGI app that drains the body itself, outside FastAPI's body parsing."""
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"read"})


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


async def test_the_declared_length_is_refused_even_when_the_route_never_reads_the_body() -> None:
    """The Content-Length check runs before the route: nothing depends on the route reading."""
    async with await _client(_app(_LIMITS)) as c:
        ok = await c.post("/v1/noread", content=b"x" * 100)
        big = await c.post("/v1/noread", content=b"x" * 101)
    assert ok.status_code == 200, ok.text
    _assert_413(big, 100)


async def _chunks(n: int, size: int):
    for _ in range(n):
        yield b"x" * size


async def test_a_mounted_asgi_app_reading_a_streamed_body_gets_a_413() -> None:
    async with await _client(_app(_LIMITS)) as c:
        ok = await c.post("/v1/raw/x", content=_chunks(2, 40))
        big = await c.post("/v1/raw/x", content=_chunks(5, 40))
    assert ok.status_code == 200 and ok.text == "read"
    _assert_413(big, 100)


async def test_the_backstop_answers_when_no_exception_handler_catches_the_refusal() -> None:
    """Wrapped round a bare ASGI app (no exception middleware at all), the middleware itself answers."""
    app = BodySizeLimitMiddleware(_draining_asgi_app, max_body_bytes=100)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        ok = await c.post("/anything", content=_chunks(2, 40))
        big = await c.post("/anything", content=_chunks(5, 40))
    assert ok.status_code == 200 and ok.text == "read"
    _assert_413(big, 100)


def test_every_route_cap_applies_with_and_without_a_trailing_slash() -> None:
    """CV-13: the middleware runs before routing, so `/files/` (which FastAPI would redirect to `/files`) must meet the same cap as `/files`."""
    from primer.api.config import RequestLimitsConfig
    from primer.api.middleware.body_limit import BodySizeLimitMiddleware, body_limit_options

    limits = RequestLimitsConfig(max_body_bytes=10_000_000, workspace_file_max_body_bytes=9_000_000, service_publish_max_body_bytes=8_000_000)
    mw = BodySizeLimitMiddleware(lambda *a: None, **body_limit_options(limits))
    for method, path in (
        ("PUT", "/v1/workspaces/w/files"),
        ("POST", "/v1/services/s/versions"),
        ("POST", "/v1/workspaces/w/sessions/s/canvas/events"),
        ("POST", "/v1/workspaces/w/sessions/s/canvas/ops"),
    ):
        # ``_NO_AUTH_LAYER`` keeps the historical caps: the comparison is about
        # the route's own cap, not the anonymous one.
        assert mw._limit_for(method, path + "/", _NO_AUTH_LAYER) == mw._limit_for(method, path, _NO_AUTH_LAYER), path
    assert mw._limit_for("PUT", "/v1/workspaces/w/files/", _NO_AUTH_LAYER) == 9_000_000
    assert mw._limit_for("PUT", "/v1/workspaces/w/files/x", _NO_AUTH_LAYER) == 10_000_000      # a longer path is still not the route


# --- Review 4.4 (fix round 1): the anonymous cap through the REAL
# AuthMiddleware, in the app's order ----------------------------------------
#
# The bare apps above install no auth layer, so their scopes carry no
# state and keep the historical caps (the tests above pin that). These
# run through the real test app: ``BodySizeLimitMiddleware`` inside the
# real ``AuthMiddleware``, which publishes a Starlette ``State``
# (attribute access) on ``scope["state"]``. The anonymous 413 test
# doubles as the drift canary: if the auth state shape or the middleware
# order changes so an anonymous request no longer reads as "auth ran, no
# user", the 413 becomes a 404/422 and the test fails.


_ANON_CAP = 1024 * 1024  # RequestLimitsConfig().anonymous_max_body_bytes
_FILES_PUT = "/v1/workspaces/w-1/files?path=t.txt"


def _valid_file_body(content_len: int) -> bytes:
    """A valid FileWriteBody JSON padded past the anonymous cap."""
    return json.dumps(
        {"encoding": "text", "content": "a" * (content_len - 30)},
    ).encode()


@pytest.mark.asyncio
async def test_anonymous_body_over_the_anonymous_cap_is_refused_before_the_route(
    raw_client,
) -> None:
    """The reviewer's repro: an anonymous PUT to the 128 MiB
    workspace-files route over the 1 MiB anonymous cap is refused at the
    cap, before the route reads a byte."""
    r = await raw_client.put(_FILES_PUT, content=b"x" * (_ANON_CAP + 1))
    _assert_413(r, _ANON_CAP)


@pytest.mark.asyncio
async def test_invalid_cookie_and_invalid_bearer_get_the_anonymous_cap(
    raw_client,
) -> None:
    over = b"x" * (_ANON_CAP + 1)
    for headers in (
        {"cookie": "primer_session=garbage"},
        {"authorization": "Bearer not-a-real-token"},
    ):
        r = await raw_client.put(_FILES_PUT, content=over, headers=headers)
        _assert_413(r, _ANON_CAP)


@pytest.mark.asyncio
async def test_anonymous_streamed_body_over_the_anonymous_cap_is_refused(
    raw_client,
) -> None:
    # Chunked (no Content-Length): the bytes actually received are counted.
    r = await raw_client.put(
        _FILES_PUT, content=_chunks(3, _ANON_CAP // 3 + 1),
    )
    _assert_413(r, _ANON_CAP)


@pytest.mark.asyncio
async def test_a_valid_cookie_keeps_the_route_caps(raw_client) -> None:
    """A signed-in caller keeps the route caps: the same over-anonymous-cap
    body passes the middleware and reaches the route (unknown workspace:
    404, not 413)."""
    resp = await raw_client.post(
        "/v1/auth/register",
        json={"username": "capuser", "password": "testpassword"},
    )
    assert resp.status_code == 200, resp.text
    r = await raw_client.put(
        _FILES_PUT,
        content=_valid_file_body(1_500_000),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 404, r.text  # workspace w-1 does not exist


@pytest.mark.asyncio
async def test_a_valid_bearer_keeps_the_route_caps(
    raw_client, fake_storage_provider,
) -> None:
    resp = await raw_client.post(
        "/v1/auth/register",
        json={"username": "bearuser", "password": "testpassword"},
    )
    assert resp.status_code == 200, resp.text
    user = (
        await fake_storage_provider.get_storage(User).list(OffsetPage(offset=0, length=10))
    ).items[0]
    plaintext = mint_plaintext()
    await fake_storage_provider.get_storage(ApiToken).create(
        ApiToken(
            id="at-cap",
            user_id=user.id,
            name="cap-test",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=[],
            created_at=datetime.now(timezone.utc),
        )
    )
    r = await raw_client.put(
        _FILES_PUT,
        content=_valid_file_body(1_500_000),
        headers={
            "authorization": f"Bearer {plaintext}",
            "content-type": "application/json",
        },
    )
    assert r.status_code == 404, r.text  # the body passed the caps; unknown workspace


def test_the_anonymous_cap_defaults_and_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = AppConfig().limits
    assert limits.anonymous_max_body_bytes == 1024 * 1024
    assert limits.max_body_bytes > limits.anonymous_max_body_bytes
    monkeypatch.setenv("PRIMER_LIMITS__ANONYMOUS_MAX_BODY_BYTES", "5555")
    assert AppConfig().limits.anonymous_max_body_bytes == 5555
    # The production wiring passes the cap through to the middleware.
    options = body_limit_options(RequestLimitsConfig(anonymous_max_body_bytes=77))
    assert options["anonymous_max_body_bytes"] == 77
