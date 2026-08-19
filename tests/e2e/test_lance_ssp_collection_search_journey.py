"""E2E: lance-backed SemanticSearchProvider end-to-end journey.

Multi-subsystem walk:
  1. Seed an embedding provider from the testconfig (the same LM Studio
     endpoint the other real-embedder e2e tests use).
  2. Create a lance SSP under a container-internal tmp path.
  3. Create a Collection and bind it to (embedder, lance SSP).
  4. Write documents with bodies; each indexes as it is written.
  5. Search, and assert the on-topic document comes back.

Pins the cross-router lance contract end-to-end against a real LanceDB.
The sibling coverage in ``test_smk_knowledge.py`` runs this same journey
against pgvector, so the vector store is the variable under test here.

This test was skipped for a long time, waiting on a "vector-bypass seam"
that would let it inject pre-computed vectors and avoid calling an
embedder. Two things retired that plan. S2 gave documents real bodies in
a content store, so writing one now produces text to chunk (the flat
document route it used to call stored metadata only, which is why no
amount of seeding made the index non-empty). And the e2e lane already
declares a real embedder capability, which is what the bypass existed to
avoid needing. So the journey runs as written, gated on that capability
rather than skipped outright.
"""

from __future__ import annotations

import httpx
import pytest

from tests._support.testconfig import load_config, requires


# Container-internal tmp dir -- host tmp_path is not visible inside the
# primer-app container. Use /tmp/<suffix>; the local backend convention.
def _container_lance_root(suffix: str) -> str:
    return f"/tmp/lance-t{suffix}"


_DOCS = {
    "printer": (
        "To add a new printer, open the settings panel, choose Devices, "
        "and select Add. Network printers are discovered automatically."
    ),
    "vpn": (
        "Connecting to the VPN requires the corporate certificate and "
        "two-factor authentication through the mobile app."
    ),
    "baking": (
        "Sourdough bread depends on a live starter, a long cold ferment, "
        "and a hot oven with steam for the first ten minutes."
    ),
}


@pytest.mark.asyncio
@requires("embedder")
async def test_lance_ssp_collection_search_journey(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Lance SSP + Collection + vector-search end-to-end journey."""
    cfg = load_config()["embedder"]
    pid_ssp = f"ssp-lance-{unique_suffix}"
    pid_emb = f"emb-lance-{unique_suffix}"
    cid = f"coll-lance-{unique_suffix}"

    cleanup: list[str] = []
    try:
        r = await client.post("/v1/embedding_providers", json={
            "id": pid_emb,
            "provider": "openai",
            "models": [{"name": cfg["model"]}],
            "config": {
                "url": cfg["base_url"],
                "api_key": cfg["api_key"],
                "flavor": "lmstudio",
            },
            "limits": {"max_concurrency": 2},
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/embedding_providers/{pid_emb}")

        r = await client.post("/v1/ssp", json={
            "id": pid_ssp,
            "provider": "lance",
            "config": {
                "path": _container_lance_root(unique_suffix),
                "index_min_rows": 2,
            },
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/ssp/{pid_ssp}")

        r = await client.post("/v1/collections", json={
            "id": cid,
            "description": "T-lance journey",
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/collections/{cid}")

        # Search on before the writes, so each document indexes as it
        # lands rather than needing a separate backfill.
        r = await client.put(f"/v1/collections/{cid}/search", json={
            "embedder": {"provider_id": pid_emb, "model": cfg["model"]},
            "vector_store_provider_id": pid_ssp,
        })
        assert r.status_code in (200, 201, 202), r.text

        doc_ids = {}
        for stem, body in _DOCS.items():
            r = await client.post(f"/v1/collections/{cid}/docs", json={
                "parent": "", "slug": stem, "body": body,
            })
            assert r.status_code in (200, 201), r.text
            doc_ids[stem] = r.json()["id"]

        # Indexing is best-effort on write, so a silent failure would
        # otherwise show up only as an empty search. Check the state.
        status = await client.get(f"/v1/collections/{cid}/search")
        assert status.status_code == 200, status.text
        assert status.json()["state"] != "error", status.text

        r = await client.post(f"/v1/collections/{cid}/search", json={
            "query": "how do I set up a printer",
            "top_k": 3,
        })
        assert r.status_code == 200, r.text
        hits = r.json()["hits"]
        assert hits, "lance-backed search returned no hits"
        top_ids = [h["document_id"] for h in hits]
        # Semantic, not keyword: the printer doc wins on meaning, and the
        # off-topic baking doc must not take the top spot.
        assert top_ids[0] == doc_ids["printer"], (top_ids, doc_ids)
    finally:
        for url in reversed(cleanup):
            await client.delete(url)
