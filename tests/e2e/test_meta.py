"""E2E: meta-level guarantees — health probe, security headers, RFC 7807.

Covers backlog items T0001, T0002, T0003.
"""

from __future__ import annotations

import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0001_health_probe_returns_ok(client: httpx.AsyncClient) -> None:
    """T0001 — GET /v1/health returns 200 with status=ok and non-null version."""
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]


_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-resource-policy": "same-origin",
}


@pytest.mark.asyncio
async def test_t0002_security_headers_present_on_health(
    client: httpx.AsyncClient,
) -> None:
    """T0002 — security middleware sets four headers on every response."""
    resp = await client.get("/v1/health")
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"missing/incorrect header {name!r}: expected {expected!r}, "
            f"got {actual!r}"
        )


@pytest.mark.asyncio
async def test_t0002_security_headers_present_on_404(
    client: httpx.AsyncClient,
) -> None:
    """T0002 — security headers must also appear on error responses."""
    resp = await client.get(f"/v1/agents/{uuid.uuid4()}")
    assert resp.status_code == 404
    for name, expected in _SECURITY_HEADERS.items():
        assert resp.headers.get(name) == expected, name


@pytest.mark.asyncio
async def test_t0003_rfc7807_404_envelope(client: httpx.AsyncClient) -> None:
    """T0003 — 404 body matches RFC 7807 with the documented slug."""
    missing_id = f"does-not-exist-{uuid.uuid4().hex}"
    resp = await client.get(f"/v1/agents/{missing_id}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    # Required fields per the spec's §3 error envelope.
    for key in ("type", "title", "status", "detail", "instance"):
        assert key in body, f"missing field {key!r} in error body: {body!r}"
    assert body["status"] == 404
    assert body["type"] == "/errors/not-found"
    # instance should echo the request path.
    assert body["instance"].endswith(f"/v1/agents/{missing_id}")
