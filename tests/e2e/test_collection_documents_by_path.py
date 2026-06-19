"""E2E: path-addressed document lifecycle on a collection.

Exercises the P1 path-addressed document REST surface on the collection
router (knowledge.py):

  PUT    /v1/collections/{cid}/documents?path=<p>  -> upsert
  GET    /v1/collections/{cid}/documents?path=<p>  -> {document, content}
  GET    /v1/collections/{cid}/documents?prefix=<p> -> {documents: [...]}
  DELETE /v1/collections/{cid}/documents?path=<p>  -> 204
  POST   /v1/collections/{cid}/documents/move      -> move

Search stays ON in P1, so the collection is still created with a real
search provider + embedder. This mirrors test_collection_search_config.py:
a pgvector SSP plus a huggingface embedder, both created over REST (the
create calls are idempotent (201 on first run, 409 on reruns).

These tests run against a live ``primer api --run-worker`` instance started
by ``scripts/e2e/bringup.sh`` (gated by ``PRIMER_RUN_E2E=1`` via conftest).
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
    sid = f"ssp-docpath-{suffix}"
    resp = await client.post("/v1/ssp", json={"id": sid, **_PGVECTOR_SSP})
    assert resp.status_code in (201, 409), f"SSP create: {resp.text}"
    return sid


async def _make_embedder(client: httpx.AsyncClient, suffix: str) -> str:
    eid = f"emb-docpath-{suffix}"
    resp = await client.post(
        "/v1/embedding_providers",
        json={"id": eid, **_EMBED_PROVIDER},
    )
    assert resp.status_code in (201, 409), f"Embedder create: {resp.text}"
    return eid


@pytest.mark.asyncio
async def test_collection_document_by_path_lifecycle(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Full upsert -> read -> list -> move -> delete journey by path."""
    sid = await _make_ssp(client, unique_suffix)
    eid = await _make_embedder(client, unique_suffix)
    cid = f"coll-docpath-{unique_suffix}"

    cleanup: list[str] = []
    try:
        # Create the collection bound to (embedder, SSP). Search stays on.
        r = await client.post("/v1/collections", json={
            "id": cid,
            "description": "e2e path-addressed document test collection",
            "embedder": {"provider_id": eid, "model": "all-MiniLM-L6-v2"},
            "search_provider_id": sid,
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/collections/{cid}")

        docs_url = f"/v1/collections/{cid}/documents"

        # 1. Upsert a document at guide/intro.md.
        r = await client.put(
            docs_url,
            params={"path": "guide/intro.md"},
            json={"content": "hello world", "title": "Intro"},
        )
        assert r.status_code in (200, 201), r.text

        # 2. Read it back by path.
        r = await client.get(docs_url, params={"path": "guide/intro.md"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["content"] == "hello world"
        assert body["document"]["path"] == "guide/intro.md"
        assert body["document"]["title"] == "Intro"

        # 3. Upsert a second document under the same prefix.
        r = await client.put(
            docs_url,
            params={"path": "guide/setup.md"},
            json={"content": "setup steps"},
        )
        assert r.status_code in (200, 201), r.text

        # 4. List under the guide/ prefix: both paths, no content/body field.
        r = await client.get(docs_url, params={"prefix": "guide/"})
        assert r.status_code == 200, r.text
        entries = r.json()["documents"]
        listed_paths = {e["path"] for e in entries}
        assert {"guide/intro.md", "guide/setup.md"} <= listed_paths, entries
        for e in entries:
            assert "content" not in e, f"listing leaked body: {e}"

        # 5. Move guide/intro.md -> guide/overview.md.
        r = await client.post(
            f"/v1/collections/{cid}/documents/move",
            json={"from": "guide/intro.md", "to": "guide/overview.md"},
        )
        assert r.status_code in (200, 204), r.text

        # 6. Old path 404s (problem+json); new path serves the same body.
        r = await client.get(docs_url, params={"path": "guide/intro.md"})
        assert r.status_code == 404, r.text
        assert "application/problem+json" in r.headers.get("content-type", "")

        r = await client.get(docs_url, params={"path": "guide/overview.md"})
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "hello world"

        # 7. Delete by path -> 204; subsequent read 404s.
        r = await client.delete(docs_url, params={"path": "guide/overview.md"})
        assert r.status_code == 204, r.text

        r = await client.get(docs_url, params={"path": "guide/overview.md"})
        assert r.status_code == 404, r.text
    finally:
        for url in reversed(cleanup):
            await client.delete(url)
