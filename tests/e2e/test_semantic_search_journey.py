"""E2E: Semantic Search subsystem full journey.

Fourth post-pivot user-journey on the API surface. Walks the entire
SSP-Collection lifecycle introduced by the semantic-search-subsystem
refactor:

  1. Create a SemanticSearchProvider
  2. Create an EmbeddingProvider (collections need one)
  3. Create a Collection referencing both
  4. List collections — assert it appears with the right SSP id
  5. Try to delete the SSP while the collection is live — assert 409
     with /errors/conflict and detail naming the collection
  6. Delete the collection
  7. Delete the SSP (now succeeds)
  8. Cleanup the embedding provider

Pins the cascade-block contract end-to-end. The block is what makes
collections "load-bearing" on their SSP rather than orphan-prone.

Envelope shapes (verified against the live server):
  - 409 cascade-block: RFC 7807 flat envelope {"type": "/errors/conflict",
    "status": 409, "detail": "...", "instance": "...", "extensions": {...}}
  - 404 not-found: RFC 7807 flat envelope {"type": "/errors/not-found",
    "status": 404, "detail": "...", "instance": "...", "extensions": {...}}
"""

from __future__ import annotations

import httpx
import pytest


def _ssp_body(sid: str) -> dict:
    return {
        "id": sid,
        "provider": "pgvector",
        "config": {
            "hostname": "localhost",
            "port": 5432,
            "database": "matrix_e2e",
            "username": "matrix",
            "password": "matrix",
            "db_schema": "public",
        },
    }


def _emb_body(eid: str) -> dict:
    return {
        "id": eid,
        "provider": "openai",
        "models": [{"name": "stub-embed"}],
        "config": {
            "url": "http://127.0.0.1:1",
            "api_key": "sk-not-used",
            "flavor": "other",
        },
        "limits": {"max_concurrency": 1},
    }


@pytest.mark.asyncio
async def test_semantic_search_full_journey(
    client: httpx.AsyncClient, unique_suffix: str,
):
    """End-to-end: SSP create → Collection create → list verify →
    cascade-block 409 → cleanup in reverse."""
    ssp_id = f"ssp-{unique_suffix}"
    emb_id = f"emb-{unique_suffix}"
    coll_id = f"coll-{unique_suffix}"

    try:
        # ----- Create SSP -----
        r = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
        assert r.status_code == 201, r.text

        # ----- Create EmbeddingProvider -----
        r = await client.post("/v1/embedding_providers", json=_emb_body(emb_id))
        assert r.status_code == 201, r.text

        # ----- Create Collection bound to both -----
        r = await client.post("/v1/collections", json={
            "id": coll_id,
            "description": "ssp journey",
            "embedder": {"provider_id": emb_id, "model": "stub-embed"},
            "search_provider_id": ssp_id,
        })
        assert r.status_code == 201, r.text

        # ----- Verify in list with search_provider_id field intact -----
        r = await client.get("/v1/collections?length=50")
        assert r.status_code == 200, r.text
        items = [c for c in r.json().get("items", []) if c["id"] == coll_id]
        assert len(items) == 1, items
        assert items[0]["search_provider_id"] == ssp_id, items[0]

        # ----- Cascade-block: DELETE SSP while collection is live -----
        # The hook raises ConflictError → RFC 7807 flat envelope.
        r = await client.delete(f"/v1/ssp/{ssp_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body.get("type") == "/errors/conflict", body
        # Detail names the referencing collection id
        assert coll_id in body.get("detail", ""), body

        # ----- Confirm SSP row is STILL present (cascade-block worked) -----
        r = await client.get(f"/v1/ssp/{ssp_id}")
        assert r.status_code == 200, r.text

        # ----- Delete the collection, then SSP delete should succeed -----
        r = await client.delete(f"/v1/collections/{coll_id}")
        assert r.status_code in (200, 204), r.text
        r = await client.delete(f"/v1/ssp/{ssp_id}")
        assert r.status_code in (200, 204), r.text

        # ----- Post-delete: GET returns 404 for both -----
        r = await client.get(f"/v1/ssp/{ssp_id}")
        assert r.status_code == 404, r.text
        r = await client.get(f"/v1/collections/{coll_id}")
        assert r.status_code == 404, r.text

    finally:
        # Best-effort cleanup in reverse dependency order
        await client.delete(f"/v1/collections/{coll_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")
        await client.delete(f"/v1/embedding_providers/{emb_id}")


@pytest.mark.asyncio
async def test_semantic_search_collection_with_unknown_ssp_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
):
    """Sister: Collection create with unknown search_provider_id is
    rejected with 404 + /errors/not-found flat RFC 7807 envelope."""
    emb_id = f"emb-unk-{unique_suffix}"
    try:
        r = await client.post("/v1/embedding_providers", json=_emb_body(emb_id))
        assert r.status_code == 201, r.text

        r = await client.post("/v1/collections", json={
            "id": f"coll-unk-{unique_suffix}",
            "description": "unknown ssp",
            "embedder": {"provider_id": emb_id, "model": "stub-embed"},
            "search_provider_id": "ssp-does-not-exist-xyz",
        })
        assert r.status_code == 404, r.text
        # _validate_ssp_exists raises NotFoundError → RFC 7807 flat envelope
        body = r.json()
        assert body.get("type") == "/errors/not-found", body
        assert "ssp-does-not-exist-xyz" in body.get("detail", ""), body
    finally:
        await client.delete(f"/v1/embedding_providers/{emb_id}")
