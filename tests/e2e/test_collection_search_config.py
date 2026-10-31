"""E2E: the v2 Collection search block, created / edited / removed.

S2 replaced the embedder trio with one optional ``search`` block, so the
cases this module used to cover changed shape:

- KNW-SC-01: Create a grep-only collection (no search block); searching
  it answers 409.
- KNW-SC-02: Create with a search block carrying a cross encoder.
- KNW-SC-03: Edit (PUT) the cross encoder's top_n; 200.
- KNW-SC-04: Remove the search block via PUT (set to null); searching
  the collection then answers 409 again.

Dropped with the model: the MMR cases (MMR was deleted when its config
became unreachable) and the embedder / search_provider_id immutability
cases (those validators only made sense while the trio was required).

These tests are safe to run against a live server with a real postgres
SSP configured. They do NOT require an LLM or a cross-encoder provider
to be configured: the cross-encoder reference is only resolved when a
search is executed.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import pgvector_ssp_config


_PGVECTOR_SSP = {
    "provider": "pgvector",
    "config": pgvector_ssp_config(),
}

_EMBED_PROVIDER = {
    "provider": "huggingface",
    "models": [{"name": "all-MiniLM-L6-v2", "dimensions": 384}],
    "config": {},
    "limits": {"max_concurrency": 1},
}


async def _make_ssp(client: httpx.AsyncClient, suffix: str) -> str:
    sid = f"ssp-sc-{suffix}"
    resp = await client.post("/v1/ssp", json={"id": sid, **_PGVECTOR_SSP})
    assert resp.status_code in (201, 409), f"SSP create: {resp.text}"
    return sid


async def _make_embedder(client: httpx.AsyncClient, suffix: str) -> str:
    eid = f"emb-sc-{suffix}"
    resp = await client.post(
        "/v1/embedding_providers",
        json={"id": eid, **_EMBED_PROVIDER},
    )
    assert resp.status_code in (201, 409), f"Embedder create: {resp.text}"
    return eid


def _coll_body(
    *,
    coll_id: str,
    eid: str,
    sid: str,
    cross_encoder: dict | None = None,
    with_search: bool = True,
) -> dict:
    body: dict = {
        "id": coll_id,
        "description": "e2e search-config test collection",
    }
    if with_search:
        search: dict = {
            "embedder": {"provider_id": eid, "model": "all-MiniLM-L6-v2"},
            "vector_store_provider_id": sid,
        }
        if cross_encoder is not None:
            search["cross_encoder"] = cross_encoder
        body["search"] = search
    return body


@pytest.mark.asyncio
async def test_knw_sc_01_grep_only_collection_rejects_search(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """A collection with no search block is created fine and answers 409."""
    coll_id = f"coll-sc01-{unique_suffix}"
    resp = await client.post(
        "/v1/collections",
        json={"id": coll_id, "description": "grep-only"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["search"] is None

    hit = await client.post(f"/v1/collections/{coll_id}/search", json={"query": "x"})
    assert hit.status_code == 409, hit.text
    assert "semantic search is not enabled" in hit.text


@pytest.mark.asyncio
async def test_knw_sc_02_create_with_cross_encoder(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """A search block persists its embedder, vector store and reranker."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc02-{unique_suffix}"

    body = _coll_body(
        coll_id=coll_id, eid=eid, sid=sid,
        cross_encoder={
            "provider_id": "ce-provider-fake",
            "model": "BAAI/bge-reranker-v2-m3",
            "top_n": 50,
        },
    )
    resp = await client.post("/v1/collections", json=body)
    assert resp.status_code == 201, resp.text
    search = resp.json()["search"]
    assert search["embedder"]["provider_id"] == eid
    assert search["vector_store_provider_id"] == sid
    assert search["cross_encoder"]["top_n"] == 50
    assert search["state"] == "indexing"


async def _make_cross_encoder(client: httpx.AsyncClient, suffix: str) -> str:
    cid = f"ce-sc-{suffix}"
    resp = await client.post(
        "/v1/cross_encoder_providers",
        json={
            "id": cid,
            "provider": "huggingface",
            "models": [{"name": "m"}],
            "config": {"token": None},
            "limits": {"max_concurrency": 1},
        },
    )
    assert resp.status_code in (200, 201, 409), f"Cross-encoder create: {resp.text}"
    return cid


@pytest.mark.asyncio
async def test_knw_sc_03_edit_cross_encoder_top_n(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """The reranker's top_n changes through the search route; a whole-row PUT cannot touch the search block (owner-only, access-control P3)."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    cid = await _make_cross_encoder(client, unique_suffix)
    coll_id = f"coll-sc03-{unique_suffix}"

    body = _coll_body(
        coll_id=coll_id, eid=eid, sid=sid,
        cross_encoder={"provider_id": cid, "model": "m", "top_n": 10},
    )
    created = await client.post("/v1/collections", json=body)
    assert created.status_code == 201, created.text

    body["search"]["cross_encoder"]["top_n"] = 25
    whole_row = await client.put(f"/v1/collections/{coll_id}", json=body)
    assert whole_row.status_code == 403, whole_row.text  # collection_search_protected: a PUT of the row never changes search
    assert "collection_search_protected" in whole_row.text

    put = await client.put(f"/v1/collections/{coll_id}/search", json=body["search"])
    assert put.status_code == 200, put.text
    assert put.json()["search"]["cross_encoder"]["top_n"] == 25
    stored = await client.get(f"/v1/collections/{coll_id}")
    assert stored.json()["search"]["cross_encoder"]["top_n"] == 25


@pytest.mark.asyncio
async def test_knw_sc_04_remove_search_block(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Disabling search through the search route turns the collection back into grep-only."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc04-{unique_suffix}"

    created = await client.post(
        "/v1/collections",
        json=_coll_body(coll_id=coll_id, eid=eid, sid=sid),
    )
    assert created.status_code == 201, created.text

    removed = await client.delete(f"/v1/collections/{coll_id}/search")
    assert removed.status_code == 204, removed.text
    stored = await client.get(f"/v1/collections/{coll_id}")
    assert stored.json()["search"] is None

    hit = await client.post(f"/v1/collections/{coll_id}/search", json={"query": "x"})
    assert hit.status_code == 409, hit.text
