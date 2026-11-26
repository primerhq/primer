"""Review 2026-11-23 4.4 (webhook part): the 1 MB body limit was checked only AFTER reading the body.

``receive_webhook`` buffered whatever the app handed it (``await request.body()``,
up to the app-wide cap) and only then compared the length. The route's own
1 MB check now happens before the read: a declared ``Content-Length`` over
1 MB is refused without reading a single byte, and a streamed (chunked) body
is read only until it crosses 1 MB, then refused - never buffered in full.
These tests count the bytes the app actually pulls from ``receive`` through
the real middleware chain. Each test registers a user on its client (the
register response sets the session cookie, exactly like the shared
``client`` fixture), so the app-wide body cap is the authenticated 32 MiB
and the ROUTE's own 1 MB check is what must fire (an anonymous caller
would hit the middleware's 1 MiB cap first, which is covered by lane B).
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from httpx import ASGITransport

from primer.model.trigger import WebhookTriggerConfig
from primer.trigger.service import ServiceDeps, create_trigger

_OVER = 2 * 1024 * 1024  # 2 MiB: over the route's 1 MB cap, under the app-wide cap


class _ByteCountingApp:
    """Outermost ASGI wrapper: counts the bytes the app actually pulls from ``receive``."""

    def __init__(self, app) -> None:
        self.app = app
        self.delivered = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def counting_receive():
            message = await receive()
            if message["type"] == "http.request":
                self.delivered += len(message.get("body", b""))
            return message

        await self.app(scope, counting_receive, send)


class _ChunkedRedeliveryApp(_ByteCountingApp):
    """Reads the request body up front and re-serves it in fixed-size chunks
    (``more_body=True`` until the last piece): the shape a real chunked
    upload reaches the app with, so no single ``receive`` holds it all."""

    def __init__(self, app, chunk_bytes: int = 256 * 1024) -> None:
        super().__init__(app)
        self.chunk_bytes = chunk_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
        position = 0

        async def chunked_receive():
            nonlocal position
            if position >= len(body):
                return {"type": "http.request", "body": b"", "more_body": False}
            piece = body[position:position + self.chunk_bytes]
            position += len(piece)
            self.delivered += len(piece)
            return {
                "type": "http.request",
                "body": piece,
                "more_body": position < len(body),
            }

        await self.app(scope, chunked_receive, send)


async def _mk_trigger(fake_storage_provider, slug: str) -> str:
    trigger = await create_trigger(
        slug=slug, name="BC", description=None,
        config=WebhookTriggerConfig(), enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )
    return trigger.config.token


@pytest.mark.asyncio
async def test_an_over_limit_declared_body_is_refused_without_reading_a_byte(
    app, fake_storage_provider,
):
    token = await _mk_trigger(fake_storage_provider, "wh-bodycap-declared")
    wrapper = _ByteCountingApp(app)
    transport = ASGITransport(app=wrapper)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/v1/auth/register",
            json={"username": "bodycap-declared", "password": "bodypassword"},
        )
        wrapper.delivered = 0  # only count the webhook request, not the register
        body = b"x" * _OVER
        r = await c.post(f"/v1/webhooks/{token}", content=body)

    assert r.status_code == 413, r.text
    assert r.json()["extensions"]["code"] == "payload_too_large"
    assert wrapper.delivered == 0, (
        f"the route read {wrapper.delivered} bytes before refusing; the "
        "declared Content-Length must be checked before the read"
    )


@pytest.mark.asyncio
async def test_an_over_limit_chunked_body_is_refused_without_reading_it_all(
    app, fake_storage_provider,
):
    token = await _mk_trigger(fake_storage_provider, "wh-bodycap-chunked")
    wrapper = _ChunkedRedeliveryApp(app)
    transport = ASGITransport(app=wrapper)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/v1/auth/register",
            json={"username": "bodycap-chunked", "password": "bodypassword"},
        )
        wrapper.delivered = 0  # only count the webhook request, not the register
        body = b"y" * _OVER
        r = await c.post(f"/v1/webhooks/{token}", content=body)

    assert r.status_code == 413, r.text
    assert r.json()["extensions"]["code"] == "payload_too_large"
    assert wrapper.delivered < len(body), (
        f"the route buffered {wrapper.delivered} of {len(body)} bytes before "
        "refusing; a streamed body must be cut off at the cap, not read in full"
    )


@pytest.mark.asyncio
async def test_an_under_limit_body_still_delivers(
    app, fake_storage_provider,
):
    """The cap bounds the size; it never makes a legal body unreachable."""
    token = await _mk_trigger(fake_storage_provider, "wh-bodycap-legal")
    wrapper = _ChunkedRedeliveryApp(app)
    transport = ASGITransport(app=wrapper)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/v1/auth/register",
            json={"username": "bodycap-legal", "password": "bodypassword"},
        )
        wrapper.delivered = 0  # only count the webhook request, not the register
        body = b"z" * (900 * 1024)
        r = await c.post(f"/v1/webhooks/{token}", content=body)

    assert r.status_code == 202, r.text
    assert wrapper.delivered == len(body), "the legal body must be read in full"
