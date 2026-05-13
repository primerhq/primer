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


# ============================================================================
# T0181 — security headers present on 201 create response
# ============================================================================


def _toolset_body(entity_id: str) -> dict:
    """Minimal valid Toolset body for the create probe."""
    return {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }


@pytest.mark.asyncio
async def test_t0181_security_headers_present_on_201_create(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0181 — POST creating an entity returns the four security headers
    documented in spec §2. Extends T0002 (GET + 404) to the create
    response path so middleware regressions on POST are caught.
    """
    entity_id = f"ts-t0181-{unique_suffix}"
    resp = await client.post("/v1/toolsets", json=_toolset_body(entity_id))
    assert resp.status_code == 201, resp.text
    try:
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            assert actual == expected, (
                f"201 create response missing/incorrect header {name!r}: "
                f"expected {expected!r}, got {actual!r}"
            )
    finally:
        await client.delete(f"/v1/toolsets/{entity_id}")


# ============================================================================
# T0182 — security headers present on 422 validation error
# ============================================================================


@pytest.mark.asyncio
async def test_t0182_security_headers_present_on_422_validation_error(
    client: httpx.AsyncClient,
) -> None:
    """T0182 — a body that fails Pydantic validation still carries all
    four security headers. Extends T0002 to the 422 path so an
    exception handler that bypasses the middleware would be caught.
    """
    # POST to /v1/llm_providers with a missing required field → 422
    resp = await client.post(
        "/v1/llm_providers",
        json={"id": "irrelevant", "provider": "anthropic"},  # missing models
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["type"] == "/errors/validation-error"
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"422 error response missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )
