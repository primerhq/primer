"""E2E: internal-collections subsystem gating.

Covers backlog item T0020 — when the subsystem hasn't been activated
(no config row), ``GET /v1/internal_collections/config`` returns 404
with the ``/errors/not-found`` envelope.

Bringup runs against a freshly-created database, so the config row is
guaranteed absent at the start of every iteration. This test does NOT
create the config (that would interfere with sibling tests that rely
on the subsystem being inactive); instead it only asserts the absence
behaviour.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0020_internal_collections_config_404_when_inactive(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/internal_collections/config")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["type"] == "/errors/not-found"
    assert body["status"] == 404
    # detail should point operators at the activation path.
    assert "PUT" in body["detail"] or "configure" in body["detail"].lower()








@pytest.mark.asyncio
async def test_t0021_bootstrap_404_when_no_config(
    client: httpx.AsyncClient,
) -> None:
    """T0021 — `POST /v1/internal_collections/bootstrap` returns 404 with
    `/errors/not-found` when no config row exists.

    The handler raises ``NotFoundError("internal collections subsystem
    is not configured; PUT /v1/internal_collections/config first.")``
    which the registry maps to status 404, slug `/errors/not-found`.
    """
    resp = await client.post("/v1/internal_collections/bootstrap")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404
    assert (
        "configured" in envelope["detail"].lower()
        or "PUT" in envelope["detail"]
    )




# ============================================================================
# T0326 — DELETE /internal_collections/config before any PUT clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0326_ic_config_delete_before_put_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0326 — DELETE /v1/internal_collections/config when no config
    row exists. The handler must produce a clean envelope (404
    /errors/not-found OR 204 silent no-op like T0187 invalidate);
    NEVER /errors/internal.
    """
    resp = await client.delete("/v1/internal_collections/config")
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope


# ============================================================================
# T0603 — IC config PUT with embedding_model="" returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0603_ic_config_put_empty_embedding_model_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0603 — Per primer/api/routers/internal_collections.py:79-86,
    `embedding_model` has `min_length=1`. Pin: explicit empty string
    is rejected with 422 /errors/validation-error. The Pydantic
    body validator runs before the embedding-provider lookup, so this
    works against an inactive subsystem with no setup needed.

    Defence: subsequent GET still returns 404 (config row not created
    by the rejected PUT).
    """
    body = {
        "embedding_provider_id": "any-placeholder",
        "embedding_model": "",
    }
    resp = await client.put("/v1/internal_collections/config", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"empty embedding_model leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"empty embedding_model should be 422 (Pydantic min_length=1); "
        f"got {resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope

    # Defence: config row NOT created
    got = await client.get("/v1/internal_collections/config")
    assert got.status_code == 404, got.text


# ============================================================================
# T0612 — Two concurrent IC-config DELETEs converge cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0612_ic_config_concurrent_deletes_converge_clean(
    client: httpx.AsyncClient,
) -> None:
    """T0612 — Fire two concurrent DELETEs on /v1/internal_collections/
    config. Both must return clean envelopes (204 / 404) — never
    /errors/internal under config-DELETE race. Subsequent GET must
    return 404 (no config row).

    No bootstrap required: bringup leaves the subsystem inactive,
    so both DELETEs see "no row" — but the race is meaningful
    because the handler may set up backend cache state on either
    path.
    """
    import asyncio

    async def _del() -> httpx.Response:
        return await client.delete("/v1/internal_collections/config")

    r1, r2 = await asyncio.gather(_del(), _del(), return_exceptions=True)
    for i, r in enumerate((r1, r2)):
        assert not isinstance(r, BaseException), (
            f"DELETE #{i} raised: {r!r}"
        )
        envelope = r.json() if r.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"DELETE #{i} leaked /errors/internal: "
            f"{r.status_code}: {r.text}"
        )
        # 204 (no-op idempotent) or 404 (not found) — both acceptable
        assert r.status_code in (204, 404), (
            f"DELETE #{i} unexpected status: "
            f"{r.status_code}: {r.text}"
        )

    # Subsequent GET shows no config
    got = await client.get("/v1/internal_collections/config")
    assert got.status_code == 404, got.text


# ============================================================================
# T0707 — IC config PUT against deleted EmbeddingProvider clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0707_ic_config_put_against_deleted_embedding_provider(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0707 — Two-state pin: IC config write × provider lifecycle.
    Sequence:
        1. Create EmbeddingProvider X
        2. DELETE X
        3. PUT IC config referencing X

    The PUT must produce a clean envelope (4xx documented error or
    permissive 200 like T0265's Agent→deleted-provider pattern);
    never /errors/internal.

    Defence: If 200 (permissive path), DELETE the orphan config so
    it doesn't pollute later tests. If 4xx, no config row should exist.
    """
    embedder_id = f"emb-t0707-{unique_suffix}"
    pr = await client.post("/v1/embedding_providers", json={
        "id": embedder_id,
        "provider": "huggingface",
        "models": [
            {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
        ],
        "config": {"token": "hf-placeholder"},
        "limits": {"max_concurrency": 1},
    })
    assert pr.status_code == 201, pr.text

    # Ensure no IC config exists (defence against pollution)
    await client.delete("/v1/internal_collections/config")

    try:
        # DELETE the embedding provider
        rm = await client.delete(f"/v1/embedding_providers/{embedder_id}")
        assert rm.status_code in (204, 404), rm.text

        # PUT IC config referencing the deleted provider
        body = {
            "embedding_provider_id": embedder_id,
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        }
        resp = await client.put(
            "/v1/internal_collections/config", json=body,
        )
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"IC config PUT against deleted provider leaked "
            f"/errors/internal: {resp.text}"
        )
        # Acceptable: 200 (permissive — IC config persisted; bootstrap
        # would later fail to find provider), 4xx (validator caught
        # the dangling reference), or 502 (upstream surfaced).
        assert resp.status_code in (200, 400, 404, 422, 502), (
            f"IC config PUT against deleted provider unexpected "
            f"status: {resp.status_code}: {resp.text}"
        )
        if resp.status_code != 200:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        # Clean up any IC config that landed
        await client.delete("/v1/internal_collections/config")
        # Provider already deleted; idempotent best-effort
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


# ============================================================================
# T0424 — GET /v1/toolsets/_search/tools before subsystem activation
# returns clean envelope (never /errors/internal)
# ============================================================================


@pytest.mark.asyncio
async def test_t0424_search_toolset_tools_before_activation_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0424 - ``_search`` is a retired alias of ``search``, and no toolset answers either id (the catalogue stopped listing ``search`` in the platform revamp). GETting /v1/toolsets/_search/tools must produce a clean envelope, never /errors/internal. Setup ensures the subsystem is OFF (idempotent DELETE)."""
    # Ensure subsystem is OFF.
    await client.delete("/v1/internal_collections/config")

    resp = await client.get("/v1/toolsets/_search/tools")
    envelope = resp.json() if resp.content else {}

    # Primary invariant: never a generic /errors/internal leak.
    assert envelope.get("type") != "/errors/internal", (
        f"_search/tools before activation leaked /errors/internal: "
        f"{resp.status_code}: {resp.text}"
    )
    # Documented outcomes: 404 (toolset absent), 503 (subsystem
    # inactive — also documented for the /search routes), 200 with
    # empty tools. Forbid 5xx generic.
    assert resp.status_code in (200, 404, 503), (
        f"_search/tools before activation unexpected status: "
        f"{resp.status_code}: {resp.text}"
    )
    if resp.status_code != 200:
        assert envelope.get("type", "").startswith("/errors/"), (
            f"non-RFC-7807 envelope on _search/tools before activation: "
            f"{envelope}"
        )
