"""E2E: Collection search config (MMR + CER) create/edit and embedder immutability.

Covers the user-2 collection-search-config-ui task:

- KNW-SC-01: Create a collection with MMR config; verify persisted.
- KNW-SC-02: Create a collection with CER config; verify persisted.
- KNW-SC-03: Edit (PUT) a collection to update MMR; embedder unchanged; 200.
- KNW-SC-04: Edit (PUT) a collection to update CER top_n; 200.
- KNW-SC-05: Edit (PUT) that changes embedder.provider_id returns 422.
- KNW-SC-06: Edit (PUT) that changes embedder.model returns 422.
- KNW-SC-07: Edit (PUT) that changes search_provider_id returns 422.
- KNW-SC-08: Remove search config via PUT (set to null); 200.

These tests are safe to run against a live server with a real postgres
SSP configured. They do NOT require an LLM or a cross-encoder provider
to be configured: CER config is just stored as a reference (it is only
resolved when a search is executed).
"""

from __future__ import annotations

import httpx
import pytest


_PGVECTOR_SSP = {
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
    search: dict | None = None,
) -> dict:
    body: dict = {
        "id": coll_id,
        "description": "e2e search-config test collection",
        "embedder": {"provider_id": eid, "model": "all-MiniLM-L6-v2"},
        "search_provider_id": sid,
    }
    if search is not None:
        body["search"] = search
    return body


@pytest.mark.asyncio
async def test_knw_sc_01_create_with_mmr(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Collection created with MMR config persists mmr fields."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc01-{unique_suffix}"

    body = _coll_body(
        coll_id=coll_id,
        eid=eid,
        sid=sid,
        search={"mmr": {"lambda_mult": 0.7, "fetch_k": 40}},
    )
    resp = await client.post("/v1/collections", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["search"] is not None
    assert data["search"]["mmr"] is not None
    assert data["search"]["mmr"]["lambda_mult"] == pytest.approx(0.7)
    assert data["search"]["mmr"]["fetch_k"] == 40
    assert data["search"]["cer"] is None


@pytest.mark.asyncio
async def test_knw_sc_02_create_with_cer(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Collection created with CER config persists cer fields."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc02-{unique_suffix}"

    body = _coll_body(
        coll_id=coll_id,
        eid=eid,
        sid=sid,
        search={
            "cer": {
                "provider_id": "ce-provider-fake",
                "model": "BAAI/bge-reranker-v2-m3",
                "top_n": 50,
            }
        },
    )
    resp = await client.post("/v1/collections", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["search"] is not None
    assert data["search"]["cer"] is not None
    assert data["search"]["cer"]["provider_id"] == "ce-provider-fake"
    assert data["search"]["cer"]["top_n"] == 50
    assert data["search"]["mmr"] is None


@pytest.mark.asyncio
async def test_knw_sc_03_edit_mmr(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """PUT updates MMR settings while keeping embedder unchanged."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc03-{unique_suffix}"

    body = _coll_body(coll_id=coll_id, eid=eid, sid=sid)
    await client.post("/v1/collections", json=body)

    updated = {
        **body,
        "search": {"mmr": {"lambda_mult": 0.3, "fetch_k": None}},
    }
    put = await client.put(f"/v1/collections/{coll_id}", json=updated)
    assert put.status_code == 200, put.text
    data = put.json()
    assert data["search"]["mmr"]["lambda_mult"] == pytest.approx(0.3)
    assert data["search"]["mmr"]["fetch_k"] is None
    # Embedder must be unchanged.
    assert data["embedder"]["provider_id"] == eid
    assert data["embedder"]["model"] == "all-MiniLM-L6-v2"


@pytest.mark.asyncio
async def test_knw_sc_04_edit_cer_top_n(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """PUT updates CER top_n."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc04-{unique_suffix}"

    body = _coll_body(
        coll_id=coll_id,
        eid=eid,
        sid=sid,
        search={"cer": {"provider_id": "ce-fake", "model": "m", "top_n": 100}},
    )
    await client.post("/v1/collections", json=body)

    updated = {
        **body,
        "search": {"cer": {"provider_id": "ce-fake", "model": "m", "top_n": 25}},
    }
    put = await client.put(f"/v1/collections/{coll_id}", json=updated)
    assert put.status_code == 200, put.text
    assert put.json()["search"]["cer"]["top_n"] == 25


@pytest.mark.asyncio
async def test_knw_sc_05_embedder_provider_id_immutable(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """PUT with changed embedder.provider_id returns 422."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc05-{unique_suffix}"

    body = _coll_body(coll_id=coll_id, eid=eid, sid=sid)
    await client.post("/v1/collections", json=body)

    bad = {
        **body,
        "embedder": {"provider_id": "other-provider", "model": "all-MiniLM-L6-v2"},
    }
    put = await client.put(f"/v1/collections/{coll_id}", json=bad)
    assert put.status_code == 422, put.text


@pytest.mark.asyncio
async def test_knw_sc_06_embedder_model_immutable(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """PUT with changed embedder.model returns 422."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc06-{unique_suffix}"

    body = _coll_body(coll_id=coll_id, eid=eid, sid=sid)
    await client.post("/v1/collections", json=body)

    bad = {**body, "embedder": {"provider_id": eid, "model": "other-model"}}
    put = await client.put(f"/v1/collections/{coll_id}", json=bad)
    assert put.status_code == 422, put.text


@pytest.mark.asyncio
async def test_knw_sc_07_search_provider_id_immutable(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """PUT with changed search_provider_id returns 422."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc07-{unique_suffix}"

    body = _coll_body(coll_id=coll_id, eid=eid, sid=sid)
    await client.post("/v1/collections", json=body)

    bad = {**body, "search_provider_id": "other-ssp"}
    put = await client.put(f"/v1/collections/{coll_id}", json=bad)
    assert put.status_code == 422, put.text


@pytest.mark.asyncio
async def test_knw_sc_08_remove_search_config(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """PUT with search=null removes retrieval augmentation."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    coll_id = f"coll-sc08-{unique_suffix}"

    body = _coll_body(
        coll_id=coll_id,
        eid=eid,
        sid=sid,
        search={"mmr": {"lambda_mult": 0.5}},
    )
    await client.post("/v1/collections", json=body)

    updated = {**body, "search": None}
    put = await client.put(f"/v1/collections/{coll_id}", json=updated)
    assert put.status_code == 200, put.text
    assert put.json()["search"] is None
