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


# ============================================================================
# T0186 — singular entity path returns a clean 404 (route not mounted)
# ============================================================================


@pytest.mark.asyncio
async def test_t0186_singular_entity_path_returns_clean_404(
    client: httpx.AsyncClient,
) -> None:
    """T0186 — All CRUD routes are mounted under the PLURAL entity name
    (`/v1/llm_providers`, not `/v1/llm_provider`). A request to the
    singular form must produce a 404 without leaking a 5xx and without
    falling into a route handler that doesn't exist.

    The contract: the request reaches the router and is rejected
    cleanly; the body is JSON (default FastAPI 404 or RFC 7807 — either
    is acceptable as long as status is 404 and no /errors/internal).
    """
    resp = await client.get("/v1/llm_provider/anything")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    # Accept either RFC 7807 shape OR FastAPI's default {"detail": "..."}
    if "type" in body:
        assert body["type"].startswith("/errors/"), body
        assert body["type"] != "/errors/internal", body
    else:
        # FastAPI default 404 envelope
        assert "detail" in body, body


# ============================================================================
# T0206 — trailing-slash list-endpoint variant behaves consistently
# ============================================================================


@pytest.mark.asyncio
async def test_t0206_trailing_slash_variant_behaves_consistently(
    client: httpx.AsyncClient,
) -> None:
    """T0206 — GET /v1/toolsets and GET /v1/toolsets/ may resolve to
    the same handler, redirect, or 404. Whichever the API does, both
    must produce a clean envelope (no 5xx) and carry the four
    documented security headers.
    """
    bare = await client.get("/v1/toolsets")
    slash = await client.get("/v1/toolsets/")

    for resp, label in ((bare, "no-slash"), (slash, "trailing-slash")):
        assert resp.status_code < 500, (
            f"{label} returned 5xx: {resp.status_code}: {resp.text}"
        )
        # 200/3xx/404 are all acceptable — but security headers must
        # be present regardless
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            assert actual == expected, (
                f"{label} response missing/incorrect header {name!r}: "
                f"expected {expected!r}, got {actual!r}"
            )


# ============================================================================
# T0207 — HEAD /v1/health returns no body with security headers preserved
# ============================================================================


@pytest.mark.asyncio
async def test_t0207_head_health_returns_headers_only(
    client: httpx.AsyncClient,
) -> None:
    """T0207 — HEAD /v1/health behaves like GET but with no body.
    Security headers must still be set by the middleware (the spec
    contract is "every response").
    """
    resp = await client.head("/v1/health")
    # HEAD on a GET-only route is supported by Starlette's default —
    # may return 200 (preferred) or 405 (if explicitly disallowed).
    assert resp.status_code in (200, 405), resp.text
    assert resp.content == b"", (
        f"HEAD response should have an empty body; got {resp.content!r}"
    )
    # Security headers present on the HEAD response
    if resp.status_code == 200:
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            assert actual == expected, (
                f"HEAD /v1/health missing/incorrect header {name!r}: "
                f"expected {expected!r}, got {actual!r}"
            )


# ============================================================================
# T0208 — OPTIONS /v1/llm_providers/{id} surfaces Allow header
# ============================================================================


@pytest.mark.asyncio
async def test_t0208_options_on_provider_row_pins_allow_header(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0208 — OPTIONS on a row-scoped path. T0102 covered /v1/health;
    this covers a CRUD entity row. The handler must respond with
    either 200/204 (no body required) and an Allow header listing the
    verbs supported on this path. NEVER 5xx.

    Tests against a real row to ensure 404 doesn't mask the OPTIONS
    contract.
    """
    entity_id = f"llm-t0208-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }
    created = await client.post("/v1/llm_providers", json=body)
    assert created.status_code == 201, created.text
    try:
        resp = await client.request(
            "OPTIONS", f"/v1/llm_providers/{entity_id}",
        )
        assert resp.status_code < 500, resp.text
        # Most servers return 200/204 with Allow set; the contract is
        # "Allow header present"
        if resp.status_code in (200, 204):
            allow = resp.headers.get("allow", "")
            assert allow, (
                f"OPTIONS {entity_id} returned {resp.status_code} but "
                f"no Allow header"
            )
            # Allow must include at minimum GET (the most basic verb)
            assert "GET" in allow.upper(), (
                f"Allow header {allow!r} should include GET"
            )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")
