"""E2E: error-envelope guarantees per §3 of the app spec.

Covers backlog items T0007 (422 validation), T0008 (409 conflict),
T0009 (DELETE idempotency).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


def _toolset_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 4},
    }


@pytest.mark.asyncio
async def test_t0007_invalid_llm_provider_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0007 — malformed config body yields 422 with /errors/validation."""
    # Provider says 'anthropic' but config shape is for a different
    # provider (missing required `api_key`, has a bogus key). This must
    # fail the discriminated-union validation.
    bad = {
        "id": f"llm-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "x", "context_length": 1024}],
        "config": {"wrong_field": "nope"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=bad)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error"
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_t0008_duplicate_toolset_id_returns_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0008 — POSTing the same id twice yields 409 with /errors/conflict."""
    entity_id = f"ts-dup-{unique_suffix}"
    body = _toolset_body(entity_id)
    first = await client.post("/v1/toolsets", json=body)
    assert first.status_code == 201, first.text
    try:
        dup = await client.post("/v1/toolsets", json=body)
        assert dup.status_code == 409, dup.text
        envelope = dup.json()
        assert envelope["type"] == "/errors/conflict"
        assert envelope["status"] == 409
    finally:
        await client.delete(f"/v1/toolsets/{entity_id}")


@pytest.mark.asyncio
async def test_t0097_llm_provider_empty_models_rejected_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0097 — `LLMProvider.models` has `min_length=1`. POSTing with
    `models: []` must yield 422 `/errors/validation-error`, with a
    detail mentioning the constraint."""
    body = {
        "id": f"llm-empty-{unique_suffix}",
        "provider": "anthropic",
        "models": [],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope
    assert envelope["status"] == 422


@pytest.mark.asyncio
async def test_t0103_parallel_create_same_id_yields_201_and_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0103 — race two POSTs of the same Toolset id concurrently.
    Exactly one must win 201; the other must lose 409 with the
    documented `/errors/conflict` slug.

    NB: there's a separate cold-start concurrency bug — when the
    backing table doesn't exist yet, two concurrent CREATE-TABLE
    operations race on the Postgres `pg_type` catalog, producing
    502 `/errors/provider-error` instead of a clean 409. To pin
    the INSERT-level uniqueness contract specifically, this test
    first warms the table by creating + deleting a sentinel row
    sequentially. After that, the race is purely on INSERT and the
    documented 201/409 split holds.
    """
    # Warm-up: ensure the toolset table exists so the race is
    # purely on the row-level unique-key path.
    warmup_id = f"ts-warmup-{unique_suffix}"
    warmup = await client.post(
        "/v1/toolsets", json=_toolset_body(warmup_id),
    )
    assert warmup.status_code == 201, warmup.text
    await client.delete(f"/v1/toolsets/{warmup_id}")

    entity_id = f"ts-race-{unique_suffix}"
    body = _toolset_body(entity_id)
    try:
        a, b = await asyncio.gather(
            client.post("/v1/toolsets", json=body),
            client.post("/v1/toolsets", json=body),
            return_exceptions=False,
        )
        statuses = sorted([a.status_code, b.status_code])
        assert statuses == [201, 409], (
            f"expected one winner (201) and one loser (409), got "
            f"{a.status_code} + {b.status_code}: {a.text} / {b.text}"
        )
        loser = a if a.status_code == 409 else b
        assert loser.json()["type"] == "/errors/conflict", loser.json()
        assert loser.json()["status"] == 409
    finally:
        await client.delete(f"/v1/toolsets/{entity_id}")


@pytest.mark.asyncio
async def test_t0009_delete_on_missing_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0009 — DELETE on an already-deleted row returns 404 with the
    /errors/not-found envelope.

    NB: the original backlog wording said DELETE was idempotent (returned
    204 on missing). The actual CRUD contract in matrix/api/routers/_crud.py
    is "delete, 404 on miss". This test asserts the real behaviour and
    catches any future regression that would silently broaden DELETE to
    return 204 for missing rows.
    """
    entity_id = f"llm-idem-{unique_suffix}"
    create = await client.post("/v1/llm_providers", json=_llm_body(entity_id))
    assert create.status_code == 201, create.text

    first_delete = await client.delete(f"/v1/llm_providers/{entity_id}")
    assert first_delete.status_code == 204

    second_delete = await client.delete(f"/v1/llm_providers/{entity_id}")
    assert second_delete.status_code == 404, (
        f"second DELETE on missing row: expected 404, got "
        f"{second_delete.status_code}: {second_delete.text}"
    )
    body = second_delete.json()
    assert body["type"] == "/errors/not-found"
    assert body["status"] == 404


# ============================================================================
# T0172 — PUT with body.id ≠ path id returns 409 /errors/conflict
# ============================================================================


@pytest.mark.asyncio
async def test_t0172_put_with_mismatched_body_id_returns_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0172 — Spec §3 lists "mismatched body id" as a 409 trigger.
    Verify against LLMProvider (a generator-CRUD entity): PUT to
    /v1/llm_providers/<path-id> with body.id=<different-id> must
    surface 409 /errors/conflict.
    """
    path_id = f"llm-mm-path-{unique_suffix}"
    body_id = f"llm-mm-body-{unique_suffix}"

    # Create the row at path_id so the PUT target exists (otherwise the
    # mismatch could be masked by a 404 from the missing-row path).
    created = await client.post("/v1/llm_providers", json=_llm_body(path_id))
    assert created.status_code == 201, created.text
    try:
        mismatched = _llm_body(body_id)
        resp = await client.put(
            f"/v1/llm_providers/{path_id}", json=mismatched,
        )
        assert resp.status_code == 409, (
            f"expected 409 for mismatched id, got {resp.status_code}: "
            f"{resp.text}"
        )
        envelope = resp.json()
        assert envelope["type"] == "/errors/conflict", envelope
        assert envelope["status"] == 409
    finally:
        await client.delete(f"/v1/llm_providers/{path_id}")


# ============================================================================
# T0183 — POST entity with empty `{}` body returns clean 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0183_post_entity_with_empty_body_returns_clean_422(
    client: httpx.AsyncClient,
) -> None:
    """T0183 — POSTing `{}` to /v1/llm_providers fails Pydantic validation
    (the required fields are absent). Pin 422 + /errors/validation-error,
    never 5xx.
    """
    resp = await client.post("/v1/llm_providers", json={})
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope
    assert envelope["status"] == 422


# ============================================================================
# T0184 — POST entity with malformed JSON body returns clean 4xx envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0184_post_entity_with_malformed_json_body_clean_4xx(
    client: httpx.AsyncClient,
) -> None:
    """T0184 — Sending a non-JSON body to a JSON route. The middleware
    must respond with 4xx (typically 400 /errors/bad-request or 422
    /errors/validation-error) carrying the RFC 7807 envelope shape.
    No 500 leak; the documented `instance` field present.
    """
    resp = await client.post(
        "/v1/llm_providers",
        content=b"this is not json {{",
        headers={"content-type": "application/json"},
    )
    assert 400 <= resp.status_code < 500, resp.text
    envelope = resp.json()
    # RFC 7807 envelope shape
    for key in ("type", "title", "status", "detail", "instance"):
        assert key in envelope, f"missing key {key!r}: {envelope!r}"
    assert envelope["type"].startswith("/errors/"), envelope
    assert envelope["type"] != "/errors/internal", envelope


# ============================================================================
# T0185 — PATCH on a CRUD entity returns 405 with `Allow` header
# ============================================================================


@pytest.mark.asyncio
async def test_t0185_patch_on_crud_entity_returns_405_with_allow_header(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0185 — PATCH is not a documented verb on the CRUD generator
    (spec §5 lists POST/GET/PUT/DELETE). The router must respond with
    405 Method Not Allowed and include an `Allow` header signalling
    that other verbs ARE supported on this path.

    NB: FastAPI's default 405 response populates Allow with the verbs
    actually mounted at that path. In practice the header may only
    list one of the available verbs (e.g. just "GET") rather than
    the full set — the documented contract is just "405 with non-empty
    Allow". The status code is the load-bearing pin; pin Allow only as
    non-empty.

    Tests against a real row so the 405 isn't masked by a 404 from
    the missing-row path.
    """
    entity_id = f"llm-t0185-{unique_suffix}"
    created = await client.post(
        "/v1/llm_providers", json=_llm_body(entity_id),
    )
    assert created.status_code == 201, created.text
    try:
        resp = await client.request(
            "PATCH",
            f"/v1/llm_providers/{entity_id}",
            json={"id": entity_id},
        )
        assert resp.status_code == 405, resp.text
        allow = resp.headers.get("allow", "")
        assert allow, (
            f"405 response should set a non-empty Allow header; got {allow!r}"
        )
        # GET must be listed — it's the most basic instance-endpoint verb
        assert "GET" in allow.upper(), (
            f"Allow header {allow!r} should include GET"
        )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0209 — POST with Content-Type: text/plain on a JSON endpoint returns 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0209_post_with_wrong_content_type_returns_clean_4xx(
    client: httpx.AsyncClient,
) -> None:
    """T0209 — POST to a JSON endpoint with `Content-Type: text/plain`
    must produce a clean 4xx envelope (typically 422 from Pydantic's
    "expected json"). T0184 covered malformed JSON body; this covers
    the wrong-content-type case where the body itself is even string
    text, not JSON.

    The hard contract: no 500 leak, RFC 7807 envelope present.
    """
    body = '{"id": "test", "provider": "anthropic"}'
    resp = await client.post(
        "/v1/llm_providers",
        content=body.encode("utf-8"),
        headers={"content-type": "text/plain"},
    )
    assert 400 <= resp.status_code < 500, resp.text
    envelope = resp.json()
    assert envelope["type"].startswith("/errors/"), envelope
    assert envelope["type"] != "/errors/internal", envelope
    # RFC 7807 shape sanity
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, f"missing key {key!r}: {envelope!r}"


# ============================================================================
# T0280 — DELETE on /v1/llm_providers list endpoint returns 405 with Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0280_delete_on_llm_providers_list_endpoint_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0280 — DELETE on the entity-collection path (no `{id}`) is not
    a documented verb. Per spec §5, the generator mounts POST/GET on
    the collection path and only DELETE on `/{id}`. Pin: 405 with
    non-empty Allow header listing the actual collection-level verbs.
    """
    resp = await client.request("DELETE", "/v1/llm_providers")
    assert resp.status_code == 405, resp.text
    allow = resp.headers.get("allow", "")
    assert allow, f"405 response should set Allow header; got {allow!r}"
    # Collection endpoint supports GET (list) and POST (create) at
    # minimum — Allow must mention at least one of those
    allow_upper = allow.upper()
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"Allow header {allow!r} should mention GET or POST"
    )


# ============================================================================
# T0281 — PATCH on /v1/toolsets list endpoint returns 405 with non-empty Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0281_patch_on_toolsets_list_endpoint_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0281 — PATCH on the list endpoint is not documented. Mirror
    of T0185 (which covered PATCH on the instance endpoint) for the
    collection-list path.
    """
    resp = await client.request("PATCH", "/v1/toolsets", json={})
    assert resp.status_code == 405, resp.text
    allow = resp.headers.get("allow", "")
    assert allow, f"405 response should set Allow header; got {allow!r}"
    allow_upper = allow.upper()
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"Allow header {allow!r} should mention GET or POST"
    )
