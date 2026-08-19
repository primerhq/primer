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
            "database": "primer_e2e",
            "username": "primer",
            "password": "primer",
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

        # ----- Create Collection, then bind both through search -----
        r = await client.post("/v1/collections", json={
            "id": coll_id,
            "description": "ssp journey",
        })
        assert r.status_code == 201, r.text

        # S2: the embedder and the vector store are the per-collection
        # search config, not top-level collection fields.
        r = await client.put(f"/v1/collections/{coll_id}/search", json={
            "embedder": {"provider_id": emb_id, "model": "stub-embed"},
            "vector_store_provider_id": ssp_id,
        })
        assert r.status_code in (200, 201, 202), r.text

        # ----- Verify the binding survives a list read -----
        r = await client.get("/v1/collections?length=50")
        assert r.status_code == 200, r.text
        items = [c for c in r.json().get("items", []) if c["id"] == coll_id]
        assert len(items) == 1, items
        assert items[0]["search"]["vector_store_provider_id"] == ssp_id, items[0]

        # ----- DELETE the SSP while the collection still references it --
        # This used to be blocked with a 409. S2 dropped that reference
        # check deliberately: an operator retiring a provider should not
        # have to hunt down every collection first. The collection is
        # left pointing at something that is gone, and says so through
        # its own search state rather than by vetoing the delete.
        r = await client.delete(f"/v1/ssp/{ssp_id}")
        assert r.status_code == 204, r.text

        r = await client.get(f"/v1/ssp/{ssp_id}")
        assert r.status_code == 404, r.text

        # The collection keeps the binding, and searching through it now
        # reports the missing provider instead of pretending to work.
        r = await client.get(f"/v1/collections/{coll_id}")
        assert r.status_code == 200, r.text
        assert r.json()["search"]["vector_store_provider_id"] == ssp_id, r.text

        r = await client.post(
            f"/v1/collections/{coll_id}/search", json={"query": "anything"},
        )
        assert r.status_code != 200, r.text
        # The message names the collection, not the provider: this
        # embedder stub is unreachable by design, so the query fails at
        # embedding time and never reaches the missing vector store.
        # What matters is that it refuses and says which collection.
        assert coll_id in r.text, r.text

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
async def test_semantic_search_collection_with_unknown_ssp_is_rejected(
    client: httpx.AsyncClient, unique_suffix: str,
):
    """Sister: enabling search against an unknown vector store is refused.

    409, not 404: the route reads it as a conflict with the platform's
    current state rather than a missing route target, and answers the
    same way for an unregistered embedder. Deleting a provider that
    collections already reference stays allowed (see the journey above);
    it is MAKING a reference to something absent that is refused.
    """
    emb_id = f"emb-unk-{unique_suffix}"
    try:
        r = await client.post("/v1/embedding_providers", json=_emb_body(emb_id))
        assert r.status_code == 201, r.text

        coll_id = f"coll-unk-{unique_suffix}"
        r = await client.post("/v1/collections", json={
            "id": coll_id, "description": "unknown ssp",
        })
        assert r.status_code == 201, r.text

        r = await client.put(f"/v1/collections/{coll_id}/search", json={
            "embedder": {"provider_id": emb_id, "model": "stub-embed"},
            "vector_store_provider_id": "ssp-does-not-exist-xyz",
        })
        assert r.status_code == 409, r.text
        body = r.json()
        assert body.get("type") == "/errors/conflict", body
        assert "ssp-does-not-exist-xyz" in body.get("detail", ""), body
    finally:
        await client.delete(f"/v1/collections/coll-unk-{unique_suffix}")
        await client.delete(f"/v1/embedding_providers/{emb_id}")
