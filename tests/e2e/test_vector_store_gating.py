"""E2E: Collection / Document CRUD does NOT gate on vector_store config.

Covers backlog items T0017, T0018 (reframed).

The original backlog entries claimed `GET /v1/collections` and
`POST /v1/documents` return 503 with `/errors/service-unavailable`
when `AppConfig.vector_store` is null. That's wrong: probing a server
brought up via ``MATRIX_E2E_NO_VECTOR=1 bash scripts/e2e/bringup.sh``
shows both routes operate against storage rows and respond normally.
The 503 gating only applies to the search / ingestion paths that
actually consult the vector store (covered separately by T0019).

These tests pin the real contract so a future regression that adds
spurious gating to the CRUD path is caught.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0017_collections_list_works_regardless_of_vector_store(
    client: httpx.AsyncClient,
) -> None:
    """T0017 — `GET /v1/collections` returns 200 even when no vector
    store is configured. The route enumerates Collection rows out of
    Postgres; it never consults the vector backend."""
    resp = await client.get("/v1/collections?limit=10&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "offset"
    assert isinstance(body["items"], list)


@pytest.mark.asyncio
async def test_t0018_document_create_does_not_gate_on_vector_store(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0018 — `POST /v1/documents` writes the row directly without
    embedding it; absence of vector_store does NOT cause a 503.

    Embedding into the vector store is a separate (currently lazy)
    concern — the row is persisted unconditionally.
    """
    body = {
        "id": f"doc-t0018-{unique_suffix}",
        "name": "test doc",
        "collection_id": "any-collection",
        "text": "anything",
        "meta": {},
    }
    resp = await client.post("/v1/documents", json=body)
    try:
        assert resp.status_code in (200, 201), resp.text
        out = resp.json()
        assert out["id"] == body["id"]
        # Service did NOT advertise itself as inactive.
        assert "subsystem-inactive" not in resp.text
        assert "service-unavailable" not in resp.text
    finally:
        await client.delete(f"/v1/documents/{body['id']}")
