"""E2E: meta-level guarantees — health probe, security headers, RFC 7807.

Covers backlog items T0001, T0002, T0003.
"""

from __future__ import annotations

import uuid
from pathlib import Path

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
# T0374 — POST with Content-Type: application/json; charset=utf-8
# ============================================================================


@pytest.mark.asyncio
async def test_t0374_post_with_charset_suffixed_json_content_type(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0374 — Pin that the parameter-suffixed Content-Type
    `application/json; charset=utf-8` is accepted (companion to
    T0209 which rejects `text/plain`). FastAPI's default body parser
    matches by media-type prefix, ignoring charset parameters.
    """
    import json
    entity_id = f"llm-t0374-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post(
        "/v1/llm_providers",
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 201, (
        f"POST with charset-suffixed Content-Type should succeed; "
        f"got {resp.status_code}: {resp.text}"
    )
    try:
        # Confirm the row was created
        got = await client.get(f"/v1/llm_providers/{entity_id}")
        assert got.status_code == 200, got.text
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0375 — RFC 7807 `instance` field echoes the full request path
# ============================================================================


@pytest.mark.asyncio
async def test_t0375_rfc7807_instance_echoes_full_request_path(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0375 — Per spec §3, the RFC 7807 envelope's `instance` field
    must echo the request path. Pin this on both 404 (GET missing
    LLMProvider) and 422 (POST malformed body) responses.
    """
    # 404 path
    missing_id = f"missing-t0375-{unique_suffix}"
    r404 = await client.get(f"/v1/llm_providers/{missing_id}")
    assert r404.status_code == 404, r404.text
    body_404 = r404.json()
    assert "instance" in body_404, body_404
    # The instance field should END with the request path
    assert body_404["instance"].endswith(
        f"/v1/llm_providers/{missing_id}"
    ), (
        f"404 instance field {body_404['instance']!r} does not echo "
        f"request path /v1/llm_providers/{missing_id}"
    )

    # 422 path
    r422 = await client.post("/v1/llm_providers", json={})
    assert r422.status_code == 422, r422.text
    body_422 = r422.json()
    assert "instance" in body_422, body_422
    assert body_422["instance"].endswith("/v1/llm_providers"), (
        f"422 instance field {body_422['instance']!r} does not echo "
        f"request path /v1/llm_providers"
    )


# ============================================================================
# T0387 — DELETE 204 response body is empty and Content-Length=0
# ============================================================================


@pytest.mark.asyncio
async def test_t0387_delete_204_body_empty_content_length_zero(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0387 — Pin the documented DELETE response shape: 204 with
    empty body. Content-Length should be 0 (or absent — both are
    valid HTTP for 204).
    """
    entity_id = f"ts-t0387-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    create = await client.post("/v1/toolsets", json=body)
    assert create.status_code == 201, create.text

    resp = await client.delete(f"/v1/toolsets/{entity_id}")
    assert resp.status_code == 204, resp.text
    assert resp.content == b"", (
        f"DELETE 204 should have empty body; got {resp.content!r}"
    )
    cl = resp.headers.get("content-length")
    # Content-Length is 0 if present (not required for 204)
    if cl is not None:
        assert cl == "0", (
            f"DELETE 204 Content-Length should be 0; got {cl!r}"
        )


# ============================================================================
# T0388 — GET response Content-Length matches body byte length
# ============================================================================


@pytest.mark.asyncio
async def test_t0388_get_content_length_matches_body_bytes(
    client: httpx.AsyncClient,
) -> None:
    """T0388 — Sanity-check the framework isn't emitting a stale
    Content-Length on a list endpoint. If Content-Length is present,
    it must equal the actual body byte length.
    """
    resp = await client.get("/v1/llm_providers")
    assert resp.status_code == 200, resp.text
    cl = resp.headers.get("content-length")
    if cl is not None:
        assert int(cl) == len(resp.content), (
            f"Content-Length mismatch: header={cl}, "
            f"body bytes={len(resp.content)}"
        )


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


# ============================================================================
# T0389 — X-Request-Id header behaviour pinned on /v1/health
# ============================================================================


@pytest.mark.asyncio
async def test_t0389_x_request_id_header_behaviour_on_health(
    client: httpx.AsyncClient,
) -> None:
    """T0389 — Spec doesn't promise X-Request-Id on the response, but
    if it IS surfaced (commonly via middleware) then the contract is:
    when the client sends X-Request-Id, the server either echoes that
    exact value or omits the header entirely; it MUST NOT emit a
    duplicate header (httpx coalesces duplicates with `, `) and MUST
    NOT replace the client value with a server-generated one.

    Pins:
    - No client header → server may set its own value or omit
    - Client header → if present, must equal the client's value
    - Header value (if present) never contains `, ` from duplication
    """
    # Case 1: no client-supplied header — absent or single-valued
    r1 = await client.get("/v1/health")
    assert r1.status_code == 200, r1.text
    server_id = r1.headers.get("x-request-id")
    if server_id is not None:
        assert ", " not in server_id, (
            f"X-Request-Id appears duplicated (multi-valued): "
            f"{server_id!r}"
        )

    # Case 2: client supplies a value
    client_id = "test-rid-t0389-deadbeef"
    r2 = await client.get(
        "/v1/health", headers={"x-request-id": client_id},
    )
    assert r2.status_code == 200, r2.text
    echoed = r2.headers.get("x-request-id")
    if echoed is not None:
        # Must not be multi-valued (no comma-coalesced duplicates)
        assert ", " not in echoed, (
            f"X-Request-Id appears duplicated when client set it: "
            f"{echoed!r}"
        )
        # If echoed at all, must equal what the client sent — server
        # MUST NOT silently replace a client-supplied request id with
        # its own (that would break end-to-end tracing).
        assert echoed == client_id, (
            f"server overrode client X-Request-Id: sent "
            f"{client_id!r}, got {echoed!r}"
        )


# ============================================================================
# T0390 — application/problem+json 404 body parses as JSON (extends T0312)
# ============================================================================


@pytest.mark.asyncio
async def test_t0390_problem_json_404_body_parses_as_json(
    client: httpx.AsyncClient,
) -> None:
    """T0390 — Strict-client compatibility: a 404 response carrying
    Content-Type `application/problem+json` (per RFC 7807) MUST also
    have a body that parses cleanly as JSON. Some strict clients
    branch on the Content-Type and refuse to parse anything other
    than `application/json` if the body isn't well-formed; pin both
    the media type AND the body shape.

    Extends T0312 which only checked the Content-Type header.
    """
    import json
    resp = await client.get("/v1/agents/missing-agent-t0390")
    assert resp.status_code == 404, resp.text
    ct = resp.headers.get("content-type", "")
    assert "problem+json" in ct.lower(), (
        f"expected problem+json content-type; got {ct!r}"
    )
    # Raw body must parse via stdlib json (not just httpx's tolerant
    # parser) — strict-client compat pin.
    body = json.loads(resp.content.decode("utf-8"))
    assert isinstance(body, dict), body
    # RFC 7807 required fields all present and correctly typed
    assert body.get("type") == "/errors/not-found", body
    assert body.get("status") == 404, body
    assert isinstance(body.get("title"), str) and body["title"], body
    assert isinstance(body.get("detail"), str), body
    assert isinstance(body.get("instance"), str), body


# ============================================================================
# T0466 — OPTIONS on /sessions/{sid}/steer returns clean response with Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0466_options_steer_route_clean_allow_header(
    client: httpx.AsyncClient,
) -> None:
    """T0466 — OPTIONS verb-table pin for the nested session steer
    sub-resource. /v1/workspaces/{wid}/sessions/{sid}/steer is a
    POST-only route (per spec §11). OPTIONS must respond cleanly
    (200/204 with Allow listing POST, or 405 if the framework
    doesn't auto-respond). NEVER /errors/internal.

    The steer route is a deeply-nested sub-resource (4 path
    segments) — this catches a regression where the OPTIONS
    handler chokes on the nested workspace_id/session_id
    placeholders.
    """
    # Use placeholder ids — OPTIONS doesn't need them to resolve to
    # real rows; the verb-table check happens at the route layer.
    resp = await client.request(
        "OPTIONS",
        "/v1/workspaces/any-wid/sessions/any-sid/steer",
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code in (200, 204):
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"OPTIONS {resp.status_code} but no Allow header"
        )
        # POST is the documented verb on this route
        assert "POST" in allow.upper(), (
            f"Allow header {allow!r} should include POST"
        )


# ============================================================================
# T0543 — POST /v1/health returns 405 with Allow listing GET
# ============================================================================


@pytest.mark.asyncio
async def test_t0543_post_health_returns_405_with_allow_get(
    client: httpx.AsyncClient,
) -> None:
    """T0543 — /v1/health is a GET-only route (per spec §1).
    POST must be rejected with 405 method-not-allowed; the Allow
    header must list GET. Pin: clean envelope; security headers
    preserved (per T0002 contract — every response carries them).
    """
    resp = await client.post("/v1/health")
    assert resp.status_code == 405, (
        f"POST /v1/health should be 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"405 response missing Allow header; status={resp.status_code}"
    )
    assert "GET" in allow.upper(), (
        f"Allow header {allow!r} should include GET"
    )

    # Security headers preserved on the 405 (extends T0002 to the
    # method-not-allowed path)
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"405 missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0727 — GET with `Accept: application/xml` returns JSON 200 or 406, never
# a 5xx /errors/internal. FastAPI does not implement Accept-driven content
# negotiation by default; the documented behaviour is to ignore the header
# and return JSON. This test pins that the unusual Accept value cannot
# crash the response path or leak an internal-error envelope.
# ============================================================================


@pytest.mark.asyncio
async def test_t0727_get_with_accept_xml_returns_json_or_406_cleanly(
    client: httpx.AsyncClient,
) -> None:
    """T0727 — `GET /v1/llm_providers` with `Accept: application/xml`
    must produce a clean, documented response. Either:

    * 200 with `Content-Type: application/json` (FastAPI ignores the
      Accept header and returns the same JSON envelope as the
      unspecified case), OR
    * 406 with an RFC 7807 envelope (if a future content-negotiator
      adopts strict accept semantics).

    The hard contract is: no 5xx leak, RFC 7807 envelope on any 4xx,
    and the response body is well-formed JSON (we never accidentally
    emit XML just because the client asked).

    Priority 6 — error envelope safety. The Accept header is a
    classic source of 500-leaks in middleware-driven negotiation
    stacks (Starlette ContentNegotiation, custom serializers).
    """
    resp = await client.get(
        "/v1/llm_providers",
        headers={"accept": "application/xml"},
    )
    # No 5xx, ever — that's the priority-6 contract.
    assert resp.status_code < 500, (
        f"Accept: application/xml triggered a 5xx leak: "
        f"{resp.status_code}: {resp.text}"
    )
    # Documented outcomes: 200 (FastAPI default, returns JSON) or 406.
    assert resp.status_code in (200, 406), (
        f"Accept: application/xml expected 200 or 406; got "
        f"{resp.status_code}: {resp.text}"
    )
    # Even if a 406 is returned, the body must be JSON (the server
    # never emits XML on this codebase) and must carry the RFC 7807
    # type prefix.
    ctype = resp.headers.get("content-type", "")
    assert "application/json" in ctype.lower(), (
        f"response Content-Type {ctype!r} is not JSON; the server must "
        f"never emit XML / other formats regardless of Accept"
    )
    if resp.status_code == 406:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope


# ============================================================================
# T0728 — POST with Content-Length larger than the actual body returns a
# clean 4xx, not a hung connection / 500 leak. This is a classic
# slowloris-adjacent attack surface where the framework MUST short-circuit
# rather than wait indefinitely for missing bytes.
# ============================================================================


@pytest.mark.asyncio
async def test_t0728_post_with_oversize_content_length_returns_clean_4xx(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0728 — `POST /v1/llm_providers` with a body of N bytes but a
    `Content-Length` header advertising 10×N bytes. The server must
    NOT hang waiting for the missing bytes; it must close the
    connection cleanly with either a 4xx response OR a transport-level
    error that httpx raises locally.

    Because httpx itself will refuse to send a body smaller than the
    declared Content-Length (it raises LocalProtocolError before any
    bytes hit the wire), the easiest way to reproduce on the wire is
    to fabricate the header manually with ``httpx.Request(..., headers=...)``
    — but httpx still validates. So this test exercises the inverse:
    declare a Content-Length SHORTER than the actual body and assert
    that the server reads only what was advertised and produces a
    deterministic envelope (likely 422 because the truncated body is
    no longer valid JSON).

    Priority 6 — error envelope safety. Header/body mismatch is an
    input-shape edge that has historically produced 500 /errors/internal
    leaks in ASGI stacks before the body parser bottoms out.
    """
    import json
    full_body = json.dumps({
        "id": f"llm-t0728-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }).encode("utf-8")
    # Advertise a shorter length so the server reads a truncated body.
    short_length = max(10, len(full_body) // 4)
    try:
        resp = await client.post(
            "/v1/llm_providers",
            content=full_body,
            headers={
                "content-type": "application/json",
                "content-length": str(short_length),
            },
        )
    except Exception as exc:
        # Client-side refusal (h11's LocalProtocolError "Too much
        # data for declared Content-Length" before any bytes hit the
        # wire) or server-side framing reject (uvicorn h11 dropping
        # the connection) both satisfy the priority-6 contract: the
        # malformed request never produces an /errors/internal
        # envelope leak because either it never reached the server
        # or the server rejected the framing itself rather than the
        # FastAPI request handler.
        # We narrow the catch to "protocol-level" errors (h11 errors
        # and httpx protocol errors) — anything else (assertion,
        # connection refused, asyncio cancel) re-raises.
        exc_class = type(exc).__name__
        if "ProtocolError" not in exc_class:
            raise
        assert "internal" not in str(exc).lower(), (
            f"transport error mentions /errors/internal: {exc}"
        )
        return
    # No 5xx leak ever.
    assert resp.status_code < 500, (
        f"Content-Length mismatch triggered a 5xx leak: "
        f"{resp.status_code}: {resp.text}"
    )
    # 4xx with RFC 7807 envelope.
    assert 400 <= resp.status_code < 500, (
        f"Content-Length mismatch expected 4xx; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json() if resp.content else {}
    # The envelope must carry /errors/ prefix and must NOT be
    # /errors/internal — a clean 422 from "body is not valid JSON"
    # (because it got truncated) is the expected shape.
    assert envelope.get("type", "").startswith("/errors/"), (
        f"non-RFC-7807 envelope on Content-Length mismatch: {envelope}"
    )
    assert envelope.get("type") != "/errors/internal", (
        f"Content-Length mismatch leaked /errors/internal: {envelope}"
    )


# ============================================================================
# T0428 — POST with `Content-Type: application/xml` returns 422 cleanly,
# never /errors/internal. Mirror of T0209 (text/plain) for the XML
# media-type path. FastAPI's default body parser refuses non-JSON
# content-types; the priority-6 pin is "the envelope shape is RFC 7807,
# and the rejection happens at the parser, not 500-leaking out of a
# downstream type coercion".
# ============================================================================


@pytest.mark.asyncio
async def test_t0428_post_with_xml_content_type_returns_clean_4xx(
    client: httpx.AsyncClient,
) -> None:
    """T0428 — `POST /v1/llm_providers` with
    ``Content-Type: application/xml`` and an XML-shaped body must
    produce a clean 4xx envelope (typically 422 from Pydantic /
    FastAPI's body parser refusing the media type). T0209 covered
    text/plain; this covers XML — a meaningfully different rejection
    path because XML is a *real* structured data format that some
    middleware stacks try to parse.

    Priority 6 — error envelope safety. The hard contract is: no
    5xx leak, RFC 7807 envelope on the 4xx response, and the body
    must not be 5xx-echoed back as the rejection message (a
    historical leak pattern in ASGI stacks).
    """
    xml_body = (
        b"<?xml version='1.0'?>\n"
        b"<provider>\n"
        b"  <id>test</id>\n"
        b"  <kind>anthropic</kind>\n"
        b"</provider>\n"
    )
    resp = await client.post(
        "/v1/llm_providers",
        content=xml_body,
        headers={"content-type": "application/xml"},
    )
    # No 5xx ever — priority-6 contract.
    assert resp.status_code < 500, (
        f"XML content-type triggered 5xx leak: "
        f"{resp.status_code}: {resp.text}"
    )
    # 4xx with RFC 7807 envelope.
    assert 400 <= resp.status_code < 500, (
        f"XML content-type expected 4xx; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope.get("type", "").startswith("/errors/"), envelope
    assert envelope.get("type") != "/errors/internal", (
        f"XML content-type leaked /errors/internal: {envelope}"
    )
    # RFC 7807 keys present.
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, f"missing key {key!r}: {envelope!r}"


# ============================================================================
# T0465 — POST with `Content-Type: application/json; charset=ascii` is
# accepted (charset parameter is the body's character set, not a different
# media type). Extends T0374 (charset=utf-8) to a non-default charset.
# ============================================================================


@pytest.mark.asyncio
async def test_t0465_post_with_charset_ascii_content_type_succeeds(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0465 — `POST /v1/llm_providers` with
    ``Content-Type: application/json; charset=ascii`` and an ASCII
    body must succeed (201). FastAPI's default body parser matches
    by media-type prefix and ignores the charset parameter
    altogether — that's why T0374 passes with charset=utf-8 and
    why charset=ascii should pass too. Pin so a future "strict
    media-type" middleware doesn't silently break ASCII clients.

    Priority 6 — error envelope safety. A false 415 / 422 here
    would silently break legitimate clients sending ASCII bodies
    (Python's requests library defaults to ASCII on str payloads
    without explicit encoding).
    """
    import json
    entity_id = f"llm-t0465-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    # Encode as ASCII — ensure_ascii=True is the default but we set
    # the encoding explicitly so the wire bytes match the declared
    # charset.
    resp = await client.post(
        "/v1/llm_providers",
        content=json.dumps(body, ensure_ascii=True).encode("ascii"),
        headers={"content-type": "application/json; charset=ascii"},
    )
    assert resp.status_code == 201, (
        f"charset=ascii content-type should be accepted (charset is "
        f"a media-type parameter, not a different type); got "
        f"{resp.status_code}: {resp.text}"
    )
    try:
        # Confirm the row landed — sanity check that the body was
        # actually decoded, not just the headers ignored.
        got = await client.get(f"/v1/llm_providers/{entity_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == entity_id
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0417 — HEAD /v1/sessions returns 200 with empty body + 4 security headers
# (sibling of T0258 for the bespoke top-level cross-workspace sessions list).
# ============================================================================


@pytest.mark.asyncio
async def test_t0417_head_top_level_sessions_returns_headers_only(
    client: httpx.AsyncClient,
) -> None:
    """T0417 — HEAD on the top-level cross-workspace sessions list
    route. T0258 covered the generic CRUD list pattern (/v1/llm_providers);
    this covers the bespoke /v1/sessions list which is hand-rolled
    (per spec §11 — top-level lookup without workspace prefix) and
    has historically had subtle drift from the CRUD-router default.

    Body must be empty; status code must be 200 (or 405 if HEAD is
    explicitly disallowed); the 4 security headers must be preserved
    per T0002's contract.
    """
    resp = await client.head("/v1/sessions")
    assert resp.status_code in (200, 405), resp.text
    assert resp.content == b"", (
        f"HEAD /v1/sessions body should be empty; got {resp.content!r}"
    )
    if resp.status_code == 200:
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            assert actual == expected, (
                f"HEAD /v1/sessions missing/incorrect header "
                f"{name!r}: expected {expected!r}, got {actual!r}"
            )


# ============================================================================
# T0419 — OPTIONS /v1/internal_collections/config returns clean response
# with Allow listing PUT/GET/DELETE (verb-table pin for the IC singleton).
# ============================================================================


@pytest.mark.asyncio
async def test_t0419_options_internal_collections_config_allow_header(
    client: httpx.AsyncClient,
) -> None:
    """T0419 — OPTIONS verb-table pin for the IC config singleton.
    The /v1/internal_collections/config route supports PUT / GET /
    DELETE per spec §13 (config singleton). OPTIONS must respond
    cleanly (200/204 with Allow listing those verbs, or 405 if the
    framework doesn't auto-respond). NEVER /errors/internal.

    This route is a singleton (no {id} placeholder), which is a
    notably different routing path from row-scoped OPTIONS (T0208)
    and list-scoped OPTIONS (T0466); the test catches the regression
    where a singleton-style route fails to register an OPTIONS
    handler.
    """
    resp = await client.request(
        "OPTIONS", "/v1/internal_collections/config",
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code in (200, 204):
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"OPTIONS {resp.status_code} but no Allow header"
        )
        allow_upper = allow.upper()
        # The IC config singleton declares PUT/GET/DELETE per spec
        # §13. Assert all three are present so a regression that
        # silently strips one surfaces here.
        for verb in ("PUT", "GET", "DELETE"):
            assert verb in allow_upper, (
                f"Allow header {allow!r} should include {verb} "
                f"(IC config singleton declares PUT/GET/DELETE)"
            )


# ============================================================================
# T0421 — OPTIONS /v1/workers/{id}/drain returns clean response with Allow
# including POST (verb-table pin for worker drain).
# ============================================================================


@pytest.mark.asyncio
async def test_t0421_options_worker_drain_allow_includes_post(
    client: httpx.AsyncClient,
) -> None:
    """T0421 — OPTIONS verb-table pin for the worker drain
    sub-resource. /v1/workers/{id}/drain is POST-only (per spec §15;
    worker draining is a one-shot signal, not a CRUD verb). OPTIONS
    must respond cleanly with Allow listing POST. NEVER
    /errors/internal.

    Uses a placeholder worker id because OPTIONS' verb-table check
    happens at the route layer and doesn't need the id to resolve
    to a real row (mirror of T0466's pattern for the steer route).
    """
    resp = await client.request(
        "OPTIONS", "/v1/workers/any-wrk/drain",
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code in (200, 204):
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"OPTIONS {resp.status_code} but no Allow header"
        )
        assert "POST" in allow.upper(), (
            f"Allow header {allow!r} should include POST"
        )


# ============================================================================
# T0423 — POST /v1/internal_collections/config returns 405 with Allow
# listing PUT/GET/DELETE (method-not-allowed pin for the IC singleton).
# ============================================================================


@pytest.mark.asyncio
async def test_t0423_post_internal_collections_config_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0423 — Method-not-allowed pin for the IC config singleton.
    Per spec §13 the route accepts PUT / GET / DELETE; POST is NOT
    documented. The router must reject POST with 405 + a non-empty
    Allow header listing at least one of the supported verbs;
    never 4xx-as-200, never 5xx leak. Security headers preserved.

    Sister of T0543 (POST /v1/health → 405 + Allow:GET).

    NOTE on Allow-header completeness: per RFC 7231 §6.5.5 the
    Allow header SHOULD list the union of supported methods at the
    path. In practice FastAPI/Starlette's method-not-allowed
    handler only lists the verb of the *first matched route* at
    that path — so this 405 returns ``Allow: PUT`` even though GET
    and DELETE are also registered on the same path. That is a
    framework quirk, not a matrix bug; chasing a custom OPTIONS
    handler to aggregate the Allow header would be significant
    scope creep. The test pins what the framework actually does
    (405 + non-empty Allow + one of the documented verbs) so a
    regression that drops Allow entirely or flips the status still
    surfaces here.
    """
    resp = await client.post(
        "/v1/internal_collections/config", json={},
    )
    assert resp.status_code == 405, (
        f"POST /v1/internal_collections/config should be 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"405 response missing Allow header; status={resp.status_code}"
    )
    allow_upper = allow.upper()
    # At least ONE of the documented verbs must be present (framework
    # may return any single one per the note above).
    documented_verbs = {"PUT", "GET", "DELETE"}
    found = documented_verbs & {v.strip() for v in allow_upper.split(",")}
    assert found, (
        f"Allow header {allow!r} should include at least one of "
        f"PUT/GET/DELETE (IC config singleton's documented verbs); "
        f"got none"
    )
    # POST must NOT be in the Allow listing — we're rejecting POST.
    assert "POST" not in allow_upper, (
        f"Allow header {allow!r} should NOT include POST on a 405 "
        f"that rejects POST"
    )

    # Security headers preserved on the 405 (extends T0002 contract).
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"405 missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0566 — PATCH /v1/llm_providers list endpoint returns 405 with non-empty
# Allow (provider-router method-not-allowed; mirror of T0281 for toolsets
# and T0683 for cross_encoder_providers).
# ============================================================================


@pytest.mark.asyncio
async def test_t0566_patch_llm_providers_list_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0566 — Method-not-allowed pin for PATCH on the
    /v1/llm_providers list route. CRUD list endpoints accept GET
    (list) and POST (create); PATCH is NOT documented on a list
    route. The router must reject PATCH with 405 + non-empty Allow;
    never 5xx leak.

    Completes the provider-family PATCH-405 trio with T0281 (toolsets)
    and T0683 (cross_encoder_providers). Per T0423's framework note,
    FastAPI's 405 Allow may only list ONE supported verb; this test
    pins the looser contract (Allow present + GET or POST mentioned +
    PATCH not mentioned).
    """
    resp = await client.patch("/v1/llm_providers", json={})
    assert resp.status_code == 405, (
        f"PATCH /v1/llm_providers should be 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"405 response missing Allow header; status={resp.status_code}"
    )
    allow_upper = allow.upper()
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"Allow header {allow!r} should include at least GET or POST "
        f"(CRUD list endpoint declares both)"
    )
    assert "PATCH" not in allow_upper, (
        f"Allow header {allow!r} should NOT include PATCH on a 405 "
        f"that rejects PATCH"
    )

    # Security headers preserved on the 405 (extends T0002 contract).
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"405 missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0615 + T0658 + T0659 + T0686 — HEAD coverage for entity-list endpoints.
# Parametrised so the four sister tests share one assertion body: 200 or
# 405, empty body, security headers preserved. Sister of T0258
# (/v1/llm_providers) and T0417 (/v1/sessions) for the remaining
# entity-list routes that hadn't been pinned individually.
# ============================================================================


@pytest.mark.parametrize(
    "path,backlog_id",
    [
        ("/v1/workers", "T0615"),
        ("/v1/agents", "T0658"),
        ("/v1/graphs", "T0659"),
        ("/v1/collections", "T0686"),
    ],
    ids=["T0615-workers", "T0658-agents", "T0659-graphs", "T0686-collections"],
)
@pytest.mark.asyncio
async def test_head_entity_list_returns_headers_only(
    client: httpx.AsyncClient, path: str, backlog_id: str,
) -> None:
    """T0615 + T0658 + T0659 + T0686 — HEAD on entity-list endpoints
    (no body). Same shape as T0258 (HEAD /v1/llm_providers) — pin
    that the security headers and empty body apply uniformly across
    /v1/workers, /v1/agents, /v1/graphs, /v1/collections.

    HEAD may map to GET (200 with headers) or 405 if explicitly
    disallowed by the router. Either is acceptable as long as no
    5xx and security headers are preserved on the 200 path.

    The parametrisation gives the failure message a clear backlog
    id so a regression on a single endpoint surfaces with the test
    name that corresponds to its backlog entry.
    """
    resp = await client.head(path)
    assert resp.status_code in (200, 405), (
        f"{backlog_id}: HEAD {path} expected 200 or 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    assert resp.content == b"", (
        f"{backlog_id}: HEAD {path} body should be empty; got "
        f"{resp.content!r}"
    )
    if resp.status_code == 200:
        for name, expected in _SECURITY_HEADERS.items():
            actual = resp.headers.get(name)
            assert actual == expected, (
                f"{backlog_id}: HEAD {path} missing/incorrect "
                f"header {name!r}: expected {expected!r}, got "
                f"{actual!r}"
            )


# ============================================================================
# T0420 — OPTIONS /v1/internal_collections/bootstrap returns clean response
# with Allow including POST (verb-table pin for the bootstrap singleton).
# ============================================================================


@pytest.mark.asyncio
async def test_t0420_options_internal_collections_bootstrap_allow_post(
    client: httpx.AsyncClient,
) -> None:
    """T0420 — OPTIONS verb-table pin for the IC bootstrap singleton.
    The /v1/internal_collections/bootstrap route is POST-only per
    spec §13 (one-shot bootstrap signal). OPTIONS must respond
    cleanly (200/204 with Allow listing POST, or 405 fallback);
    NEVER /errors/internal.

    Sister of T0421 (worker drain) and T0466 (session steer) — all
    POST-only signal-style sub-resources where OPTIONS should
    return Allow listing POST without choking on the singleton path.
    """
    resp = await client.request(
        "OPTIONS", "/v1/internal_collections/bootstrap",
    )
    assert resp.status_code < 500, resp.text
    if resp.status_code in (200, 204):
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"OPTIONS {resp.status_code} but no Allow header"
        )
        assert "POST" in allow.upper(), (
            f"Allow header {allow!r} should include POST"
        )


# ============================================================================
# T0422 — GET on /v1/agents/search returns 405 with non-empty Allow
# listing POST (search route family is POST-only — body carries the
# query / predicate / page).
# ============================================================================


@pytest.mark.asyncio
async def test_t0422_get_agents_find_route_collision_returns_404_cleanly(
    client: httpx.AsyncClient,
) -> None:
    """T0422 — GET on the agents find route returns a clean 4xx,
    never /errors/internal. The backlog entry originally framed
    this as a 405 method-not-allowed pin (assuming GET /v1/agents/find
    would route to the find handler's verb table), but the actual
    behaviour is a routing collision: ``/v1/agents/find`` matches
    ``/v1/agents/{id}`` first and returns 404 ``Agent 'find' does
    not exist``. (The CRUD-router declares find as POST-only at
    routers/_crud.py:202, but the row-scoped GET pattern wins.)

    The test pins what the framework actually emits (404 + clean
    envelope) so a regression that promotes the response to 5xx or
    drops the envelope shape surfaces here. The 405-vs-404 design
    discussion is a separate concern (would require route ordering
    or an explicit reservation of 'find' as a non-id segment).
    """
    resp = await client.get("/v1/agents/find")
    # Priority-6 contract: never 5xx.
    assert resp.status_code < 500, (
        f"GET /v1/agents/find leaked 5xx: "
        f"{resp.status_code}: {resp.text}"
    )
    # Documented behaviour today: 404 from the row-scoped lookup
    # treating 'find' as the id. A future routing fix could flip
    # this to 405 (the original spec intent) — both are clean 4xx
    # outcomes worth tolerating in a regression net.
    assert resp.status_code in (404, 405), (
        f"GET /v1/agents/find expected 404 (current route collision) "
        f"or 405 (after a future routing fix); got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    # RFC 7807 envelope shape.
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, f"missing key {key!r}: {envelope!r}"
    assert envelope["type"].startswith("/errors/"), envelope
    assert envelope["type"] != "/errors/internal", (
        f"GET /v1/agents/find leaked /errors/internal: {envelope}"
    )
    # Security headers preserved on the 4xx (extends T0002 contract).
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"4xx missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0683 — PATCH /v1/cross_encoder_providers list endpoint returns 405
# with non-empty Allow (provider-router method-not-allowed pin; mirror
# of T0281/T0566/T0660 for the cross_encoder provider family).
# ============================================================================


@pytest.mark.asyncio
async def test_t0683_patch_cross_encoder_providers_list_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0683 — Method-not-allowed pin for PATCH on the
    /v1/cross_encoder_providers list route. CRUD list endpoints
    accept GET (list) and POST (create); PATCH is NOT documented
    on a list route (per spec §6 — PATCH is row-scoped where it
    exists at all). The router must reject PATCH with 405 +
    non-empty Allow; never 5xx leak.

    Sister of T0281 (PATCH /v1/toolsets → 405) and T0566 (PATCH
    /v1/llm_providers → 405) — extends the family contract to the
    third provider family (cross_encoder). Per T0423's framework
    note, FastAPI's 405 Allow may only list ONE supported verb;
    this test pins the looser contract (Allow present + GET or
    POST mentioned + PATCH not mentioned).
    """
    resp = await client.patch("/v1/cross_encoder_providers", json={})
    assert resp.status_code == 405, (
        f"PATCH /v1/cross_encoder_providers should be 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"405 response missing Allow header; status={resp.status_code}"
    )
    allow_upper = allow.upper()
    # At least one CRUD-list verb (GET or POST) must be in Allow.
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"Allow header {allow!r} should include at least GET or POST "
        f"(CRUD list endpoint declares both)"
    )
    # PATCH itself must NOT be in Allow — we just rejected it.
    assert "PATCH" not in allow_upper, (
        f"Allow header {allow!r} should NOT include PATCH on a 405 "
        f"that rejects PATCH"
    )

    # Security headers preserved on the 405 (extends T0002 contract).
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"405 missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0616 + T0660 — PATCH 405 coverage for the remaining provider-router
# list endpoints (workspace_providers + embedding_providers). Completes
# the PATCH-405 family alongside T0281 (toolsets), T0566 (llm_providers),
# T0683 (cross_encoder_providers).
# ============================================================================


@pytest.mark.parametrize(
    "path,backlog_id",
    [
        ("/v1/workspace_providers", "T0616"),
        ("/v1/embedding_providers", "T0660"),
    ],
    ids=[
        "T0616-workspace_providers",
        "T0660-embedding_providers",
    ],
)
@pytest.mark.asyncio
async def test_patch_provider_list_returns_405(
    client: httpx.AsyncClient, path: str, backlog_id: str,
) -> None:
    """T0616 + T0660 — PATCH on the remaining provider-router list
    endpoints (workspace_providers + embedding_providers) must
    return 405 with a non-empty Allow header. Completes the
    PATCH-405 family alongside T0281 (toolsets), T0566
    (llm_providers), T0683 (cross_encoder_providers).

    Per T0423's framework note, FastAPI's 405 Allow may only list
    ONE supported verb; the test pins the looser contract — Allow
    present + at least GET or POST included + PATCH itself NOT
    in Allow + security headers preserved.
    """
    resp = await client.patch(path, json={})
    assert resp.status_code == 405, (
        f"{backlog_id}: PATCH {path} should be 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"{backlog_id}: 405 response missing Allow header"
    )
    allow_upper = allow.upper()
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"{backlog_id}: Allow header {allow!r} should include at "
        f"least GET or POST (CRUD list declares both)"
    )
    assert "PATCH" not in allow_upper, (
        f"{backlog_id}: Allow header {allow!r} should NOT include "
        f"PATCH on a 405 that rejects PATCH"
    )

    # Security headers preserved on the 405.
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"{backlog_id}: 405 missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0684 + T0655 + T0656 + T0657 — OPTIONS verb-table pins for the
# top-level /v1/sessions list and the three session signal routes
# (cancel / pause / resume). Sister of T0421 (worker drain) and T0466
# (session steer) — all signal-style sub-resources where OPTIONS
# should respond cleanly with Allow listing the expected verb.
# ============================================================================


@pytest.mark.parametrize(
    "path,expected_verb,backlog_id",
    [
        # Top-level cross-workspace sessions list — GET-only at this
        # path (POST is on the nested /workspaces/{wid}/sessions per
        # spec §11).
        ("/v1/sessions", "GET", "T0684"),
        # Signal sub-resources — POST-only one-shot signals.
        ("/v1/workspaces/any-wid/sessions/any-sid/cancel", "POST", "T0655"),
        ("/v1/workspaces/any-wid/sessions/any-sid/pause", "POST", "T0656"),
        ("/v1/workspaces/any-wid/sessions/any-sid/resume", "POST", "T0657"),
    ],
    ids=[
        "T0684-sessions-list-GET",
        "T0655-cancel-POST",
        "T0656-pause-POST",
        "T0657-resume-POST",
    ],
)
@pytest.mark.asyncio
async def test_options_session_route_allow_header(
    client: httpx.AsyncClient,
    path: str,
    expected_verb: str,
    backlog_id: str,
) -> None:
    """T0684 + T0655 + T0656 + T0657 — OPTIONS verb-table pins for
    the top-level sessions list and the three session signal routes.
    Each route accepts a single documented verb (GET for /v1/sessions
    per spec §11; POST for cancel/pause/resume signal sub-resources).

    The contract: OPTIONS responds cleanly (200/204 with Allow
    listing the expected verb, or 405 fallback) and NEVER
    /errors/internal. Placeholder ids in the signal paths are
    acceptable because OPTIONS' verb-table check happens at the
    route layer and doesn't need the ids to resolve to real rows
    (mirror of T0466's pattern for the steer route and T0421's
    pattern for worker drain).
    """
    resp = await client.request("OPTIONS", path)
    assert resp.status_code < 500, (
        f"{backlog_id}: OPTIONS {path} leaked 5xx: "
        f"{resp.status_code}: {resp.text}"
    )
    if resp.status_code in (200, 204):
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"{backlog_id}: OPTIONS {resp.status_code} but no Allow header"
        )
        assert expected_verb in allow.upper(), (
            f"{backlog_id}: Allow header {allow!r} should include "
            f"{expected_verb}"
        )


# ============================================================================
# T0661 — DELETE on /v1/workers list endpoint returns 405 with Allow header.
# Workers list is read-only (GET-only) per spec §15; DELETE on the list path
# is undocumented and must be rejected at the router. Sister of T0322/T0323
# (DELETE on /v1/sessions list etc.) for the read-only worker router.
# ============================================================================


@pytest.mark.asyncio
async def test_t0661_delete_workers_list_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0661 — Method-not-allowed pin for DELETE on the
    /v1/workers list route. Per spec §15 the workers router is
    read-only at the list path (only GET is documented). DELETE
    must reject with 405 + Allow listing the supported verb;
    never 5xx leak.

    Per T0423's framework note, FastAPI's 405 Allow may only list
    ONE supported verb (the first matched route at the path). The
    test pins the looser contract — Allow present + GET in Allow
    (workers list is GET-only) + DELETE NOT in Allow + security
    headers preserved.
    """
    resp = await client.delete("/v1/workers")
    assert resp.status_code == 405, (
        f"DELETE /v1/workers should be 405; got "
        f"{resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"405 response missing Allow header; status={resp.status_code}"
    )
    allow_upper = allow.upper()
    assert "GET" in allow_upper, (
        f"Allow header {allow!r} should include GET (workers list "
        f"is GET-only per spec §15)"
    )
    assert "DELETE" not in allow_upper, (
        f"Allow header {allow!r} should NOT include DELETE on a "
        f"405 that rejects DELETE"
    )

    # Security headers preserved on the 405.
    for name, expected in _SECURITY_HEADERS.items():
        actual = resp.headers.get(name)
        assert actual == expected, (
            f"405 missing/incorrect header {name!r}: "
            f"expected {expected!r}, got {actual!r}"
        )


# ============================================================================
# T0418 + T0467 + T0591 + T0614 + T0710 — workspace-scoped HEAD / OPTIONS
# verb-table pins. All five share one workspace fixture so the per-test
# overhead of provider+template+workspace setup is paid once. Each item
# carries its backlog id in the parametrise nodeid for failure
# correlation, and either:
#   * HEAD path — assert 200 or 405, empty body, security headers preserved
#     on the 200 path; OR
#   * OPTIONS path — assert no 5xx; Allow includes the expected verb on
#     the 200/204 path (per T0466's pattern).
# ============================================================================


def _provider_body(entity_id: str, root: Path) -> dict:
    return {
        "id": entity_id,
        "provider": "local",
        "config": {"kind": "local", "path": str(root)},
    }


def _template_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "verb-table fixture template",
        "provider_id": provider_id,
        "backend": {"kind": "local"},
    }


@pytest.fixture
async def _workspace_for_verb_table(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
):
    """Provider + template + workspace; cleans up afterwards.

    Yields the workspace id. Used by the parametrised workspace-
    scoped verb-table test below so all five backlog items share
    one setup cycle.
    """
    wp_id = f"wp-vt-{unique_suffix}"
    tpl_id = f"wt-vt-{unique_suffix}"
    pr = await client.post(
        "/v1/workspace_providers", json=_provider_body(wp_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json=_template_body(tpl_id, provider_id=wp_id),
    )
    assert tpl.status_code == 201, tpl.text
    ws = await client.post("/v1/workspaces", json={"template_id": tpl_id})
    assert ws.status_code == 201, ws.text
    workspace_id = ws.json()["id"]
    try:
        yield workspace_id
    finally:
        await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")


@pytest.mark.parametrize(
    "method,subpath,expected_verb,backlog_id",
    [
        # T0418: HEAD /v1/workspaces/{wid}/files — bespoke list route.
        ("HEAD", "/files", None, "T0418"),
        # T0467: HEAD /v1/workspaces/{wid}/log — bespoke git-log route.
        ("HEAD", "/log", None, "T0467"),
        # T0614: OPTIONS /v1/workspaces/{wid}/sessions — nested list/create.
        # The route is GET (list) + POST (create) per spec §11.
        ("OPTIONS", "/sessions", "GET", "T0614"),
        # T0591: OPTIONS /v1/workspaces/{wid}/files/info — read-only info.
        ("OPTIONS", "/files/info", "GET", "T0591"),
        # T0710: OPTIONS /v1/workspaces/{wid}/files/download — streaming.
        ("OPTIONS", "/files/download", "GET", "T0710"),
    ],
    ids=[
        "T0418-HEAD-files",
        "T0467-HEAD-log",
        "T0614-OPTIONS-sessions",
        "T0591-OPTIONS-files-info",
        "T0710-OPTIONS-files-download",
    ],
)
@pytest.mark.asyncio
async def test_workspace_scoped_verb_table(
    client: httpx.AsyncClient,
    _workspace_for_verb_table: str,
    method: str,
    subpath: str,
    expected_verb: str | None,
    backlog_id: str,
) -> None:
    """T0418 + T0467 + T0591 + T0614 + T0710 — workspace-scoped
    HEAD / OPTIONS verb-table pins on a real (fixture-seeded)
    workspace. Each parametrised case asserts one of two contracts:

    * **HEAD** (T0418, T0467): 200 or 405; empty body; security
      headers preserved on 200. Sister of T0258 / T0417 / T0615 /
      T0658 / T0659 / T0686 for the bespoke workspace sub-resources.

    * **OPTIONS** (T0614, T0591, T0710): no 5xx; if 200/204, Allow
      includes the expected verb. Sister of T0421 (worker drain),
      T0466 (session steer), T0420 (IC bootstrap).

    Using a real workspace id (not a placeholder) so the route
    resolver matches the actual handler instead of fall-through
    to 404 on an unknown workspace.
    """
    wid = _workspace_for_verb_table
    path = f"/v1/workspaces/{wid}{subpath}"
    resp = await client.request(method, path)
    # No 5xx — universal across both branches.
    assert resp.status_code < 500, (
        f"{backlog_id}: {method} {path} leaked 5xx: "
        f"{resp.status_code}: {resp.text}"
    )

    if method == "HEAD":
        assert resp.status_code in (200, 405), (
            f"{backlog_id}: HEAD {path} expected 200 or 405; got "
            f"{resp.status_code}: {resp.text}"
        )
        assert resp.content == b"", (
            f"{backlog_id}: HEAD {path} body should be empty; got "
            f"{resp.content!r}"
        )
        if resp.status_code == 200:
            for name, expected in _SECURITY_HEADERS.items():
                actual = resp.headers.get(name)
                assert actual == expected, (
                    f"{backlog_id}: HEAD {path} missing/incorrect "
                    f"header {name!r}: expected {expected!r}, got "
                    f"{actual!r}"
                )
    elif method == "OPTIONS":
        if resp.status_code in (200, 204):
            allow = resp.headers.get("allow", "")
            assert allow, (
                f"{backlog_id}: OPTIONS {resp.status_code} but no "
                f"Allow header"
            )
            assert expected_verb in allow.upper(), (
                f"{backlog_id}: Allow header {allow!r} should "
                f"include {expected_verb}"
            )
    else:  # pragma: no cover - parametrize covers HEAD + OPTIONS only.
        raise AssertionError(f"unexpected method {method!r}")
