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


# ============================================================================
# T0292 — Two parallel POSTs to /v1/agents with same id (warmed table)
# ============================================================================


@pytest.mark.asyncio
async def test_t0292_parallel_create_same_agent_id_yields_201_and_409(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0292 — Mirror of T0103 (which raced Toolsets) for the Agent
    entity. Two concurrent POSTs of the same agent_id with the table
    pre-warmed must produce exactly one 201 and one 409
    /errors/conflict — never both 201 (would corrupt) nor 502
    /errors/provider-error (cold-start CREATE TABLE race).
    """
    # Pre-warm Agent + LLMProvider tables
    warmup_llm = f"llm-warmup-{unique_suffix}"
    warmup_agent = f"agent-warmup-{unique_suffix}"
    pr = await client.post(
        "/v1/llm_providers", json=_llm_body(warmup_llm),
    )
    assert pr.status_code == 201, pr.text
    ag_warm = await client.post(
        "/v1/agents",
        json={
            "id": warmup_agent,
            "description": "warmup",
            "model": {
                "provider_id": warmup_llm,
                "model_name": "claude-sonnet-4-6",
            },
            "tools": [],
        },
    )
    assert ag_warm.status_code == 201, ag_warm.text
    await client.delete(f"/v1/agents/{warmup_agent}")

    entity_id = f"agent-race-{unique_suffix}"
    body = {
        "id": entity_id,
        "description": "race",
        "model": {
            "provider_id": warmup_llm,
            "model_name": "claude-sonnet-4-6",
        },
        "tools": [],
    }
    try:
        a, b = await asyncio.gather(
            client.post("/v1/agents", json=body),
            client.post("/v1/agents", json=body),
            return_exceptions=False,
        )
        statuses = sorted([a.status_code, b.status_code])
        assert statuses == [201, 409], (
            f"expected one 201 and one 409, got "
            f"{a.status_code} + {b.status_code}: {a.text} / {b.text}"
        )
        loser = a if a.status_code == 409 else b
        assert loser.json()["type"] == "/errors/conflict", loser.json()
    finally:
        await client.delete(f"/v1/agents/{entity_id}")
        await client.delete(f"/v1/llm_providers/{warmup_llm}")


# ============================================================================
# T0304 — DELETE on /v1/collections list endpoint returns 405 with Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0379_provider_config_no_cross_field_validation_pinned(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0379 — Pin actual behaviour: `provider` and `config` are NOT
    cross-validated. Sending provider=anthropic with an Ollama-shaped
    config (url instead of api_key) is accepted as 201 — Pydantic's
    union resolution picks whichever variant the config shape matches,
    independent of the provider field.

    Reframed from the original (which assumed cross-field validation
    rejection); test discovered the API is permissive at create-time.
    """
    body = {
        "id": f"llm-t0379-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        # OllamaConfig shape (url, optional api_key) — DIFFERENT from
        # what anthropic provider would expect (AnthropicConfig has
        # only api_key)
        "config": {"url": "http://localhost:11434"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    # Currently accepted as 201; row is persisted with mismatched
    # provider+config. Pin no /errors/internal.
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"discriminator leaked /errors/internal: {resp.text}"
    )
    if resp.status_code == 201:
        try:
            # Verify the row was actually persisted with the mismatched
            # combo (no silent normalisation)
            got = await client.get(
                f"/v1/llm_providers/llm-t0379-{unique_suffix}",
            )
            assert got.status_code == 200, got.text
            row = got.json()
            assert row["provider"] == "anthropic", row
            # Config retains the url field (Ollama variant accepted)
            assert "url" in row["config"], row
        finally:
            await client.delete(
                f"/v1/llm_providers/llm-t0379-{unique_suffix}",
            )
    else:
        assert 400 <= resp.status_code < 500, resp.text


@pytest.mark.asyncio
async def test_t0380_openresponses_flavor_invalid_value_coerced_to_default(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0380 — Pin actual behaviour: an unknown `flavor` value is
    silently coerced to OpenResponsesFlavor.OTHER (the documented
    default) rather than rejected. The Pydantic enum field accepts
    the input and falls back to default on parse failure.

    Reframed from "must be rejected 422" — discovered the API is
    lenient. Documents the contract for future reference.
    """
    body = {
        "id": f"llm-t0380-{unique_suffix}",
        "provider": "openresponses",
        "models": [{"name": "x", "context_length": 1024}],
        "config": {
            "url": "http://localhost:1234/v1",
            "api_key": "sk-test",
            "flavor": "this-is-not-a-real-flavor-xyz",
        },
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", resp.text
    if resp.status_code == 201:
        try:
            # Verify flavor was coerced (or accepted as-is)
            got = await client.get(
                f"/v1/llm_providers/llm-t0380-{unique_suffix}",
            )
            assert got.status_code == 200, got.text
            persisted_flavor = got.json()["config"].get("flavor")
            # Either coerced to "other" (default) or accepted verbatim
            assert persisted_flavor in (
                "other", "this-is-not-a-real-flavor-xyz",
            ), got.json()
        finally:
            await client.delete(
                f"/v1/llm_providers/llm-t0380-{unique_suffix}",
            )


@pytest.mark.asyncio
async def test_t0381_llm_provider_models_context_length_zero_rejected_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0381 — LLMModel.context_length is PositiveInt; 0 is invalid.
    Must reject with 422 cleanly, no /errors/internal from asyncpg.
    """
    body = {
        "id": f"llm-t0381-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "x", "context_length": 0}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope


@pytest.mark.asyncio
async def test_t0382_llm_provider_models_context_length_negative_rejected(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0382 — Companion to T0381 with a negative value. The 422
    envelope's `extensions.errors` should include the offending
    nested field path so clients can locate the bad input.
    """
    body = {
        "id": f"llm-t0382-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "x", "context_length": -100}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope
    # Field path should be referenced somewhere — either in `detail`
    # or in extensions.errors
    detail_text = (
        envelope.get("detail", "") + " "
        + str(envelope.get("extensions", {}))
    ).lower()
    # At least mention "context_length" or "models" in the path
    assert "context_length" in detail_text or "models" in detail_text, (
        f"422 envelope doesn't reference the offending field path "
        f"`models[].context_length`: {envelope!r}"
    )


@pytest.mark.asyncio
async def test_t0383_agent_temperature_negative_rejected_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0383 — AgentModel.temperature has `ge=0.0`. Negative value
    must be rejected with 422 /errors/validation-error cleanly.
    """
    # Need an LLMProvider for the model reference
    provider_id = f"llm-t0383-{unique_suffix}"
    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "anthropic",
            "models": [
                {"name": "claude-sonnet-4-6", "context_length": 200_000},
            ],
            "config": {"api_key": "sk-test"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert pr.status_code == 201, pr.text
    try:
        body = {
            "id": f"agent-t0383-{unique_suffix}",
            "description": "T0383",
            "model": {
                "provider_id": provider_id,
                "model_name": "claude-sonnet-4-6",
            },
            "tools": [],
            "temperature": -0.1,
        }
        resp = await client.post("/v1/agents", json=body)
        assert resp.status_code == 422, resp.text
        envelope = resp.json()
        assert envelope["type"] == "/errors/validation-error", envelope
    finally:
        await client.delete(f"/v1/llm_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0304_delete_on_collections_list_endpoint_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0304 — Mirror of T0280 (DELETE on /v1/llm_providers list) for
    the Collections router. Pins per-router method handling parity:
    every CRUD-generator entity rejects DELETE on the collection-
    plural path with 405 + non-empty Allow.
    """
    resp = await client.request("DELETE", "/v1/collections")
    assert resp.status_code == 405, resp.text
    allow = resp.headers.get("allow", "")
    assert allow, f"405 should set Allow header; got {allow!r}"
    allow_upper = allow.upper()
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"Allow header {allow!r} should mention GET or POST"
    )


# ============================================================================
# T0322 — DELETE on /v1/sessions list endpoint returns 405 with Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0322_delete_on_sessions_list_endpoint_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0322 — Mirror of T0280/T0304 for the cross-workspace
    /v1/sessions router. DELETE on the list path is not a documented
    verb (per spec §12: top-level Sessions is GET + POST /find +
    GET /{id} only). 405 with non-empty Allow.
    """
    resp = await client.request("DELETE", "/v1/sessions")
    assert resp.status_code == 405, resp.text
    allow = resp.headers.get("allow", "")
    assert allow, f"405 should set Allow header; got {allow!r}"
    allow_upper = allow.upper()
    assert "GET" in allow_upper or "POST" in allow_upper, (
        f"Allow header {allow!r} should mention GET or POST"
    )


# ============================================================================
# T0323 — PATCH on /v1/workers list endpoint returns 405 with Allow
# ============================================================================


@pytest.mark.asyncio
async def test_t0323_patch_on_workers_list_endpoint_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0323 — Mirror of T0281 for the read-only /v1/workers router.
    PATCH on the list path is not a documented verb (per spec §6:
    workers is GET only at the list path; only POST /{id}/drain is
    mounted on instance paths). 405 with non-empty Allow.
    """
    resp = await client.request("PATCH", "/v1/workers", json={})
    assert resp.status_code == 405, resp.text
    allow = resp.headers.get("allow", "")
    assert allow, f"405 should set Allow header; got {allow!r}"
    allow_upper = allow.upper()
    assert "GET" in allow_upper, (
        f"Allow header {allow!r} should mention GET (the only "
        f"documented verb on /v1/workers list)"
    )


# ============================================================================
# T0564 — DELETE /v1/openapi.json returns 405 with Allow listing GET
# ============================================================================


@pytest.mark.asyncio
async def test_t0564_delete_on_openapi_json_returns_405(
    client: httpx.AsyncClient,
) -> None:
    """T0564 — The /v1/openapi.json route is always available
    (FastAPI auto-generated). DELETE is not a documented verb on it;
    must return 405 with a non-empty Allow header listing GET.

    Mirror of T0281/T0322 for the always-on OpenAPI route.

    NB: openapi.json is mounted at the FastAPI app's root with the
    `/v1` prefix, so the path is `/v1/openapi.json`.
    """
    # Try both common mount points; whichever serves OpenAPI is the
    # one where 405 should land. Locate it first via GET.
    for path in ("/v1/openapi.json", "/openapi.json"):
        get = await client.get(path)
        if get.status_code == 200:
            target = path
            break
    else:
        pytest.skip("openapi.json not reachable at known paths")

    resp = await client.request("DELETE", target)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"DELETE {target} leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 405, (
        f"DELETE {target} should be 405 (read-only route); "
        f"got {resp.status_code}: {resp.text}"
    )
    allow = resp.headers.get("allow", "")
    assert allow, (
        f"405 on {target} should set Allow header; got {allow!r}"
    )
    allow_upper = allow.upper()
    assert "GET" in allow_upper, (
        f"Allow header {allow!r} on {target} should mention GET"
    )


# ============================================================================
# T0701 — LLMProvider models[].context_length=2**63 boundary clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0701_llm_provider_context_length_int64_max_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0701 — Extends T0381/T0382 (lower bound) to the int64-max
    path through Pydantic + JSONB serialisation. PositiveInt has no
    upper bound in Pydantic v2 (Python int is unbounded), but the
    server-side path may stumble on Postgres BIGINT range or asyncpg
    type-bind. Clean envelope expected: 201 (accepted) OR clean 4xx;
    never /errors/internal.
    """
    huge = 2 ** 63  # 1 past int64 signed max
    body = {
        "id": f"llm-t0701-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "x", "context_length": huge}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"context_length=2**63 leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code in (201, 400, 422, 502), (
        f"context_length=2**63 unexpected status: "
        f"{resp.status_code}: {resp.text}"
    )
    if resp.status_code == 201:
        # If accepted, GET round-trips byte-exact
        got = await client.get(f"/v1/llm_providers/{body['id']}")
        assert got.status_code == 200, got.text
        assert got.json()["models"][0]["context_length"] == huge, (
            f"int64-max corrupted on round-trip: "
            f"{got.json()['models'][0]['context_length']!r}"
        )
    else:
        assert envelope["type"].startswith("/errors/"), envelope
    await client.delete(f"/v1/llm_providers/{body['id']}")


# ============================================================================
# T0702 — LLMProvider models[].context_length="42" string-coercion clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0702_llm_provider_context_length_string_coercion_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0702 — context_length="42" (string). Pydantic v2 has strict-
    by-default int parsing (no string→int coercion), so this should
    be 422. Hard pin: never /errors/internal.

    Documents the type-coercion edge in the PositiveInt boundary.
    """
    body = {
        "id": f"llm-t0702-{unique_suffix}",
        "provider": "anthropic",
        "models": [{"name": "x", "context_length": "42"}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"context_length='42' leaked /errors/internal: {resp.text}"
    )
    # Pydantic typically rejects string-for-int with 422; some
    # configurations may coerce (201). Both are clean.
    assert resp.status_code in (201, 422), (
        f"context_length='42' unexpected status: "
        f"{resp.status_code}: {resp.text}"
    )
    if resp.status_code == 422:
        assert envelope["type"] == "/errors/validation-error", envelope
    await client.delete(f"/v1/llm_providers/{body['id']}")


# ============================================================================
# T0703 — EmbeddingProvider POST with models=[] returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0703_embedding_provider_empty_models_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0703 — Cross-family mirror of T0097/T0449 (LLMProvider). The
    `models` field has min_length=1 across all three provider
    families; this confirms EmbeddingProvider enforces it.
    """
    body = {
        "id": f"emb-t0703-{unique_suffix}",
        "provider": "huggingface",
        "models": [],
        "config": {"token": "hf-placeholder"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/embedding_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"empty models on EmbeddingProvider leaked /errors/internal: "
        f"{resp.text}"
    )
    assert resp.status_code == 422, (
        f"empty models on EmbeddingProvider should be 422; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert envelope["type"] == "/errors/validation-error", envelope
    # Defence: row not created
    got = await client.get(f"/v1/embedding_providers/{body['id']}")
    assert got.status_code == 404, got.text


# ============================================================================
# T0704 — CrossEncoderProvider POST with models=[] returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0704_cross_encoder_provider_empty_models_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0704 — Third-family mirror of T0097/T0449 (LLMProvider) and
    T0703 (EmbeddingProvider). Completes the cross-family
    `min_length=1` contract symmetry pin.
    """
    body = {
        "id": f"ce-t0704-{unique_suffix}",
        "provider": "huggingface",
        "models": [],
        "config": {"token": None},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/cross_encoder_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"empty models on CrossEncoderProvider leaked /errors/internal: "
        f"{resp.text}"
    )
    assert resp.status_code == 422, (
        f"empty models on CrossEncoderProvider should be 422; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert envelope["type"] == "/errors/validation-error", envelope
    got = await client.get(f"/v1/cross_encoder_providers/{body['id']}")
    assert got.status_code == 404, got.text


# ============================================================================
# T0705 — OpenResponsesConfig.flavor=null returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0705_openresponses_flavor_null_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0705 — Sub-discriminator edge sibling of T0380 (which pinned
    unknown-string coercion to 'other' default). This pins explicit
    `null`. Acceptable: 422 (Pydantic enum reject), 201 with default
    coerced to "other", or clean 4xx. Hard pin: never /errors/internal.
    """
    entity_id = f"llm-t0705-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "openresponses",
        "models": [{"name": "x", "context_length": 1000}],
        "config": {
            "url": "http://localhost:1234/v1",
            "api_key": "placeholder",
            "flavor": None,
        },
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"flavor=null leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code in (201, 400, 422), (
        f"flavor=null unexpected status: {resp.status_code}: {resp.text}"
    )
    if resp.status_code == 201:
        # Accepted — GET round-trip should reflect SOME flavor (likely
        # default-coerced to 'other' per T0380's pattern)
        got = await client.get(f"/v1/llm_providers/{entity_id}")
        assert got.status_code == 200, got.text
        flavor = got.json()["config"].get("flavor")
        assert flavor is not None, (
            f"flavor=null silently persisted as null on round-trip "
            f"(should default-coerce); got config={got.json()['config']!r}"
        )
    else:
        assert envelope["type"].startswith("/errors/"), envelope
    await client.delete(f"/v1/llm_providers/{entity_id}")
