"""E2E: lance-backed SemanticSearchProvider end-to-end journey.

Multi-subsystem walk:
  1. Seed EmbeddingProvider (placeholder url; never called upstream).
  2. Create a lance SSP under a container-internal tmp path.
  3. Create a Collection bound to the SSP + embedder.
  4. POST chunks directly into the vector index via the internal
     /v1/internal_collections/test_seed_chunk seam (or the documented
     equivalent) with a hand-rolled placeholder vector.
  5. POST /v1/collections/{id}/search with the same vector; assert
     the top hit text matches.

Pins the cross-router lance contract end-to-end against a real
LanceDB. No LM Studio, no upstream LLM calls.

SKIP REASON: No vector-bypass seam exists in the public API.

Investigation:
  - POST /v1/collections/{id}/search (knowledge.py:search_collection)
    accepts ``{"query": str, "top_k": int}`` — it ALWAYS calls the
    collection's configured embedder to vectorize the query string.
    There is no ``vector`` field or bypass mode.
  - The Document model (model/collection.py:Document) carries only
    metadata (id, collection_id, name, meta) — no ``chunks`` or
    ``vector`` fields. POST /v1/documents stores a metadata row only.
  - The ingest pipeline (ingest/ingester.py:DocumentIngester.ingest)
    requires a real Embedder.embed() call to learn vector dimensionality
    and populate the vector store. There is no pre_vectorized or
    vector_bypass path through DocumentIngester.
  - The VectorStore.put(EmbeddingRecord) method exists as an internal
    interface but is only exposed through DocumentIngester; there is no
    public REST endpoint that directly accepts EmbeddingRecord payloads.
  - The internal_collections router provides subsystem-level search
    over internal entity indexes but no per-collection vector seeding.

To unblock this test a future task would need to add either:
  (a) A ``POST /v1/collections/{id}/seed_chunks`` endpoint that accepts
      pre-computed vectors (bypasses the embedder), or
  (b) A ``vector`` field on the search body that skips re-embedding.

Actual API shape (for reference):
  Collection.embedder field: {"provider_id": str, "model": str}
    (NOT "model_name" — that is the LLM-provider convention; embedder
    uses "model" per CollectionEmbedder in model/collection.py)
  Search body: {"query": str, "top_k": int}
    (NOT {"vector": [...], "k": int})
"""

from __future__ import annotations

import httpx
import pytest


# Container-internal tmp dir — host tmp_path is not visible inside the
# matrix-app container. Use /tmp/<suffix>; the local backend convention.
def _container_lance_root(suffix: str) -> str:
    return f"/tmp/lance-t{suffix}"


@pytest.mark.asyncio
@pytest.mark.skip(
    reason=(
        "requires real embedder; no vector-bypass test seam exists yet. "
        "POST /v1/collections/{id}/search always calls the embedder "
        "(accepts 'query' str, not a pre-computed 'vector'). "
        "POST /v1/documents stores metadata only — no chunks/vector field. "
        "See module docstring for full investigation and unblock path."
    )
)
async def test_lance_ssp_collection_search_journey(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Lance SSP + Collection + vector-search end-to-end journey.

    This test is skipped because no vector-bypass seam exists in the
    public API (see module docstring). The test body below documents
    the intended journey and actual API shapes discovered during
    investigation so a future implementer can unblock it quickly.

    Actual field names corrected from the original spec:
      - Collection.embedder: {"provider_id": ..., "model": ...}
        (NOT "model_name" — that is the LLM-provider convention)
      - Search body: {"query": str, "top_k": int}
        (NOT {"vector": [...], "k": int})
    """
    pid_ssp = f"ssp-lance-{unique_suffix}"
    pid_emb = f"emb-lance-{unique_suffix}"
    cid = f"coll-lance-{unique_suffix}"
    lance_root = _container_lance_root(unique_suffix)

    cleanup: list[str] = []
    try:
        # 1. EmbeddingProvider — placeholder, no upstream call.
        r = await client.post("/v1/embedding_providers", json={
            "id": pid_emb,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [
                {"name": "fake-embed", "dimensions": 4},
            ],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/embedding_providers/{pid_emb}")

        # 2. Lance SSP.
        r = await client.post("/v1/ssp", json={
            "id": pid_ssp,
            "provider": "lance",
            "config": {"path": lance_root, "index_min_rows": 2},
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/ssp/{pid_ssp}")

        # 3. Collection bound to (embedder, lance SSP).
        #    NOTE: field is "model" (CollectionEmbedder) not "model_name".
        r = await client.post("/v1/collections", json={
            "id": cid,
            "description": "T-lance journey",
            "embedder": {
                "provider_id": pid_emb,
                "model": "fake-embed",          # correct field name
            },
            "search_provider_id": pid_ssp,
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/collections/{cid}")

        # 4. BLOCKED: no vector-bypass ingestion path exists.
        #    The test would need a seam like:
        #
        #      POST /v1/collections/{cid}/seed_chunks
        #      {"chunks": [{"chunk_id": "c1", "text": "...",
        #                   "vector": [1.0, 0.0, 0.0, 0.0]}],
        #       "document_id": f"doc-{unique_suffix}",
        #       "meta": {"src": "test"}}
        #
        #    or a pre-vectorized flag on POST /v1/documents.
        #    Until that seam is added this test cannot proceed past here.
        pytest.skip("vector-bypass seam missing; see module docstring")

        # 5. Search — correct body shape for the ACTUAL endpoint:
        #      POST /v1/collections/{cid}/search
        #      {"query": "the quick brown fox", "top_k": 5}
        #    (NOT {"vector": [...], "k": 5})
        r = await client.post(f"/v1/collections/{cid}/search", json={
            "query": "the quick brown fox",     # correct field name
            "top_k": 5,                         # correct field name
        })
        assert r.status_code == 200, r.text
        hits = r.json().get("hits", [])
        assert len(hits) >= 1
        assert hits[0]["text"] == "the quick brown fox"
        assert hits[0]["meta"]["src"] == "test"
    finally:
        for url in reversed(cleanup):
            await client.delete(url)
