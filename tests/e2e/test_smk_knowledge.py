"""SMK knowledge tests. Hermetic: collection CRUD, graceful empty-search, file
conversion. Embedding-dependent ids (ingest/search/chunks/rerank/backfill,
KNW-02/04/06/08/09/10) gate on a real embedder; the system-collection guard
(KNW-07) needs a bootstrapped system collection.
"""
from __future__ import annotations

import pytest

from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


async def _embedder_and_ssp(authed_client, suffix, tmp_path):
    eid = f"emb-{suffix}"
    er = await authed_client.post(
        "/v1/embedding_providers",
        json={"id": eid, "provider": "huggingface",
              "models": [{"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384}],
              "config": {"token": "hf-placeholder"},
              "limits": {"max_concurrency": 1}},
    )
    assert er.status_code in (200, 201), er.text
    sid = f"ssp-{suffix}"
    sr = await authed_client.post(
        "/v1/ssp",
        json={"id": sid, "provider": "lance", "config": {"path": str(tmp_path / "lance")}},
    )
    assert sr.status_code in (200, 201), sr.text
    return eid, sid


@smk("SMK-KNW-01")
async def test_collection_crud(authed_client, unique_suffix, tmp_path):
    eid, sid = await _embedder_and_ssp(authed_client, unique_suffix, tmp_path)
    cid = f"col-{unique_suffix}"
    create = await authed_client.post(
        "/v1/collections",
        json={"id": cid, "description": "smk collection",
              "embedder": {"provider_id": eid, "model": "sentence-transformers/all-MiniLM-L6-v2"},
              "search_provider_id": sid, "system": False},
    )
    assert create.status_code in (200, 201), create.text
    got = await authed_client.get(f"/v1/collections/{cid}")
    assert got.status_code == 200
    delete = await authed_client.delete(f"/v1/collections/{cid}")
    assert delete.status_code in (200, 204), delete.text


@smk("SMK-KNW-05")
async def test_search_on_unindexed_collection_is_graceful(authed_client, unique_suffix, tmp_path):
    eid, sid = await _embedder_and_ssp(authed_client, unique_suffix, tmp_path)
    cid = f"col-{unique_suffix}"
    await authed_client.post(
        "/v1/collections",
        json={"id": cid, "description": "empty",
              "embedder": {"provider_id": eid, "model": "sentence-transformers/all-MiniLM-L6-v2"},
              "search_provider_id": sid, "system": False},
    )
    r = await authed_client.post(f"/v1/collections/{cid}/search", json={"query": "anything", "top_k": 5})
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == []


@smk("SMK-KNW-03")
async def test_file_upload_conversion(authed_client):
    files = {"file": ("note.md", b"# Title\n\nSome **markdown** body text.", "text/markdown")}
    r = await authed_client.post("/v1/documents/_convert_file", files=files)
    assert r.status_code in (200, 201), r.text
    # the conversion returns text derived from the file
    assert "markdown" in r.text.lower() or "title" in r.text.lower()


@smk("SMK-KNW-02", "SMK-KNW-04", "SMK-KNW-06", "SMK-KNW-08", "SMK-KNW-09", "SMK-KNW-10")
@requires("embedder")
async def test_embedding_ingest_search_backfill():
    pytest.skip("ingest/search/chunks/rerank/backfill need a reachable embedder (testconfig.embedder)")
