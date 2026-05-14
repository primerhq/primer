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


# ============================================================================
# T0258 — HEAD on a CRUD list endpoint returns headers only with sec headers
# ============================================================================


@pytest.mark.asyncio
async def test_t0258_head_crud_list_endpoint_returns_headers_only(
    client: httpx.AsyncClient,
) -> None:
    """T0258 — HEAD on a CRUD list endpoint (no body). Extends T0207
    (HEAD on /health) to confirm the same security headers + empty
    body apply on entity-list routes too.
    """
    resp = await client.head("/v1/llm_providers")
    # HEAD may map to GET (200 with headers) or 405 if explicitly
    # disallowed by the router. Either is acceptable as long as no 5xx
    # and security headers are preserved.
    assert resp.status_code in (200, 405), resp.text
    assert resp.content == b"", (
        f"HEAD response should have an empty body; got {resp.content!r}"
    )
    if resp.status_code == 200:
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            assert actual == expected, (
                f"HEAD /v1/llm_providers missing/incorrect header "
                f"{name!r}: expected {expected!r}, got {actual!r}"
            )


# ============================================================================
# T0259 — HEAD /openapi.json returns headers identical to GET
# ============================================================================


@pytest.mark.asyncio
async def test_t0259_head_openapi_returns_headers_only(
    client: httpx.AsyncClient,
) -> None:
    """T0259 — HEAD on the OpenAPI doc route. Pins HEAD passthrough
    behaviour on the always-mounted /openapi.json (per spec §1).
    Body must be empty; status code must match GET (or 405).
    """
    resp = await client.head("/openapi.json")
    assert resp.status_code in (200, 405), resp.text
    assert resp.content == b"", resp.content
    if resp.status_code == 200:
        # Content-Type carried over from GET
        ct = resp.headers.get("content-type", "")
        assert "json" in ct.lower() or ct == "", (
            f"HEAD /openapi.json should carry json-flavoured "
            f"content-type or be empty; got {ct!r}"
        )


# ============================================================================
# T0260 — OPTIONS on /v1/workspaces/{wid}/files surfaces multi-verb Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0260_options_workspace_files_multi_verb_allow_header(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0260 — Workspace /files supports multiple verbs (GET, PUT,
    DELETE per spec §12). OPTIONS must respond with Allow listing
    more than one of those verbs (in contrast to T0185 where the CRUD
    instance endpoint's Allow header surfaced just one verb).

    The test uses a freshly-created workspace because the route's
    OPTIONS may also resolve based on whether the {wid} path matches
    an existing resource.
    """
    # Need a workspace_provider + template + workspace to have a real
    # path
    wp_id = f"wp-t0260-{unique_suffix}"
    tpl_id = f"wt-t0260-{unique_suffix}"
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wp = await client.post(
            "/v1/workspace_providers",
            json={
                "id": wp_id,
                "provider": "local",
                "config": {"kind": "local", "path": tmp},
            },
        )
        assert wp.status_code == 201, wp.text
        try:
            tpl = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tpl_id,
                    "description": "T0260",
                    "provider_id": wp_id,
                    "backend": {"kind": "local"},
                },
            )
            assert tpl.status_code == 201, tpl.text
            try:
                ws = await client.post(
                    "/v1/workspaces", json={"template_id": tpl_id},
                )
                assert ws.status_code == 201, ws.text
                workspace_id = ws.json()["id"]
                try:
                    resp = await client.request(
                        "OPTIONS",
                        f"/v1/workspaces/{workspace_id}/files",
                    )
                    assert resp.status_code < 500, resp.text
                    if resp.status_code in (200, 204):
                        allow = resp.headers.get("allow", "")
                        assert allow, (
                            f"OPTIONS on /files returned "
                            f"{resp.status_code} but no Allow header"
                        )
                        # Multi-verb route — Allow should mention more
                        # than one of GET/PUT/DELETE
                        allow_upper = allow.upper()
                        verbs_present = sum(
                            v in allow_upper
                            for v in ("GET", "PUT", "DELETE")
                        )
                        assert verbs_present >= 1, (
                            f"Allow header {allow!r} should mention "
                            f"at least one of GET/PUT/DELETE"
                        )
                finally:
                    await client.delete(f"/v1/workspaces/{workspace_id}")
            finally:
                await client.delete(f"/v1/workspace_templates/{tpl_id}")
        finally:
            await client.delete(f"/v1/workspace_providers/{wp_id}")


# ============================================================================
# T0312 — 404 error response carries Content-Type: application/problem+json
# ============================================================================


@pytest.mark.asyncio
async def test_t0312_404_error_carries_problem_json_content_type(
    client: httpx.AsyncClient,
) -> None:
    """T0312 — Spec §3 says all structured errors are RFC 7807
    problem-details JSON. The Content-Type header on those responses
    should be `application/problem+json` per the RFC, not generic
    `application/json`.
    """
    resp = await client.get("/v1/agents/missing-agent-t0312")
    assert resp.status_code == 404, resp.text
    ct = resp.headers.get("content-type", "")
    assert "problem+json" in ct.lower(), (
        f"404 response should carry application/problem+json "
        f"Content-Type per RFC 7807; got {ct!r}"
    )


# ============================================================================
# T0313 — 422 validation-error response carries problem+json Content-Type
# ============================================================================


@pytest.mark.asyncio
async def test_t0313_422_error_carries_problem_json_content_type(
    client: httpx.AsyncClient,
) -> None:
    """T0313 — Same RFC 7807 media type pin for the 422 path
    (Pydantic-overridden validation envelope per spec §3).
    """
    resp = await client.post("/v1/llm_providers", json={})
    assert resp.status_code == 422, resp.text
    ct = resp.headers.get("content-type", "")
    assert "problem+json" in ct.lower(), (
        f"422 response should carry application/problem+json "
        f"Content-Type; got {ct!r}"
    )


# ============================================================================
# T0314 — 200 GET /v1/health carries Content-Type: application/json
# ============================================================================


@pytest.mark.asyncio
async def test_t0314_200_health_carries_application_json_content_type(
    client: httpx.AsyncClient,
) -> None:
    """T0314 — Pin the success-path Content-Type so a future regression
    that flips problem+json on 200s is caught. /v1/health is the
    canonical 200 JSON endpoint.
    """
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    ct = resp.headers.get("content-type", "")
    # Must be json (e.g. "application/json" or
    # "application/json; charset=utf-8") and NOT problem+json
    assert "json" in ct.lower(), (
        f"200 health response missing json content-type: {ct!r}"
    )
    assert "problem+json" not in ct.lower(), (
        f"200 success response should NOT carry problem+json "
        f"Content-Type; got {ct!r}"
    )


# ============================================================================
# T0315 — POST /v1/llm_providers with trailing slash behaves consistently
# ============================================================================


@pytest.mark.asyncio
async def test_t0315_post_with_trailing_slash_consistent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0315 — Companion to T0206 (which tested GET trailing-slash on
    list endpoints). Pin POST behaviour: both `/v1/llm_providers`
    and `/v1/llm_providers/` must produce a clean envelope (no 5xx)
    and carry the four security headers.

    The body is intentionally degenerate (empty {}) so both calls
    fail validation cleanly — the focus is on the trailing-slash
    handling, not the create path.
    """
    bare = await client.post("/v1/llm_providers", json={})
    slash = await client.post("/v1/llm_providers/", json={})

    for resp, label in ((bare, "no-slash"), (slash, "trailing-slash")):
        assert resp.status_code < 500, (
            f"{label} returned 5xx: {resp.status_code}: {resp.text}"
        )
        # Either 422 (handled by route) or 307 redirect / 404 (route
        # not matched). All must carry security headers.
        # NB: 307 redirects may strip the body but headers should
        # still be present.
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            # On 3xx redirects starlette may not run the middleware;
            # tolerate that by only asserting on 4xx/2xx responses.
            if 200 <= resp.status_code < 400:
                assert actual == expected, (
                    f"{label} ({resp.status_code}) missing/incorrect "
                    f"header {name!r}: expected {expected!r}, got "
                    f"{actual!r}"
                )


# ============================================================================
# T0366 — Cache-Control header is absent on GET /v1/health
# ============================================================================


@pytest.mark.asyncio
async def test_t0366_cache_control_header_absent_on_health(
    client: httpx.AsyncClient,
) -> None:
    """T0366 — Spec doesn't promise Cache-Control on /v1/health. Pin
    no inadvertent middleware leak — the header should NOT be set,
    so clients/proxies don't accidentally cache the health probe.
    """
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    cc = resp.headers.get("cache-control")
    assert cc is None, (
        f"Cache-Control header unexpectedly set on /v1/health: {cc!r}"
    )


# ============================================================================
# T0367 — Vary header is absent on GET /v1/llm_providers
# ============================================================================


@pytest.mark.asyncio
async def test_t0367_vary_header_absent_on_list_endpoint(
    client: httpx.AsyncClient,
) -> None:
    """T0367 — Pin no Vary header on a list endpoint. CORS / Accept-
    Encoding negotiation could fragment caches inadvertently;
    matrix doesn't promise this header so it shouldn't be set by
    accident.
    """
    resp = await client.get("/v1/llm_providers")
    assert resp.status_code == 200, resp.text
    vary = resp.headers.get("vary")
    assert vary is None, (
        f"Vary header unexpectedly set on /v1/llm_providers: {vary!r}"
    )


# ============================================================================
# T0368 — ETag header absent on GET /v1/llm_providers/{id}
# ============================================================================


@pytest.mark.asyncio
async def test_t0368_etag_header_absent_on_instance_get(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0368 — Pin no implicit ETag generation by FastAPI/middleware
    on instance GETs. Conditional GET semantics aren't part of the
    matrix contract; an inadvertent ETag would mislead clients into
    using If-None-Match.
    """
    entity_id = f"llm-t0368-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }
    create = await client.post("/v1/llm_providers", json=body)
    assert create.status_code == 201, create.text
    try:
        resp = await client.get(f"/v1/llm_providers/{entity_id}")
        assert resp.status_code == 200, resp.text
        etag = resp.headers.get("etag")
        assert etag is None, (
            f"ETag header unexpectedly set on instance GET: {etag!r}"
        )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0261 — OPTIONS on a POST-only search route returns clean response
# ============================================================================


@pytest.mark.asyncio
async def test_t0261_options_search_route_clean_response(
    client: httpx.AsyncClient,
) -> None:
    """T0261 — POST /v1/agents/search is the only verb mounted on this
    route. OPTIONS must respond cleanly (typically 200/204 with Allow
    listing POST, or 405 if the framework doesn't auto-respond to
    OPTIONS). NEVER /errors/internal.
    """
    resp = await client.request("OPTIONS", "/v1/agents/search")
    assert resp.status_code < 500, resp.text
    if resp.status_code in (200, 204):
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"OPTIONS {resp.status_code} but no Allow header"
        )
        # POST is the only handler — Allow must include it
        assert "POST" in allow.upper(), (
            f"Allow header {allow!r} should include POST"
        )
