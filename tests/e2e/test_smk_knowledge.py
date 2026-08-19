"""SMK knowledge tests.

Hermetic ids run against in-repo backends (HuggingFace embedder placeholder +
LanceDB ssp): collection CRUD (KNW-01), graceful empty-search (KNW-05), file
conversion (KNW-03).

The embedding-dependent ids run against the live e2e server's real backends:
the LM Studio OpenAI-compatible embedder, the pgvector semantic-search provider,
and the local HuggingFace cross-encoder. They gate on the corresponding
testconfig capabilities:

* KNW-02  document ingest embeds on create
* KNW-04  per-collection semantic search returns relevant hits
* KNW-06  list indexed documents + filter chunks by document_id
* KNW-08  rerank (cross-encoder) + MMR retrieval
* KNW-09  startup backfill of missing vectors (self-healing re-index)
* KNW-10  document update re-indexes; delete removes chunks

Assertions are loosened for a real, non-deterministic embedding model: we assert
semantic relevance (the on-topic document appears, ideally on top), non-empty
hit sets, chunk counts > 0, and index-consistency after update/delete -- not
exact scores or a fixed ordering.
"""
from __future__ import annotations


import pytest

from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Real-provider builders (LM Studio embedder + pgvector ssp + HF cross-encoder)
# ---------------------------------------------------------------------------


def _embedder_cfg() -> dict:
    return load_config()["embedder"]


def _cross_encoder_cfg() -> dict:
    return load_config()["cross_encoder"]


def _pgvector_dsn_cfg() -> dict:
    """pgvector connection fields matching the bringup server's vector store.

    The live e2e server (tests/.e2e/config.yaml) points pgvector at the
    compose Postgres on localhost:5432, db primer_e2e, user/pass primer.
    Reuse exactly that instance so collections created via the API are
    visible to the same store.
    """
    return {
        "hostname": "localhost",
        "port": 5432,
        "database": "primer_e2e",
        "username": "primer",
        "password": "primer",
    }


async def _make_real_embedder(authed_client, suffix, *, url: str | None = None) -> str:
    """Create an OpenAI-flavoured (LM Studio) embedding provider; return its id.

    ``url`` overrides the configured base_url (used by the backfill test to
    register an intentionally-unreachable embedder first).
    """
    cfg = _embedder_cfg()
    eid = f"emb-{suffix}"
    r = await authed_client.post(
        "/v1/embedding_providers",
        json={
            "id": eid,
            "provider": "openai",
            "models": [{"name": cfg["model"]}],
            "config": {
                "url": url or cfg["base_url"],
                "api_key": cfg["api_key"],
                "flavor": "lmstudio",
            },
            "limits": {"max_concurrency": 2},
        },
    )
    assert r.status_code in (200, 201), r.text
    return eid


async def _make_pgvector_ssp(authed_client, suffix) -> str:
    sid = f"ssp-{suffix}"
    r = await authed_client.post(
        "/v1/ssp",
        json={"id": sid, "provider": "pgvector", "config": _pgvector_dsn_cfg()},
    )
    assert r.status_code in (200, 201), r.text
    return sid


async def _make_cross_encoder(authed_client, suffix) -> str:
    cfg = _cross_encoder_cfg()
    cid = f"ce-{suffix}"
    r = await authed_client.post(
        "/v1/cross_encoder_providers",
        json={
            "id": cid,
            "provider": "huggingface",
            "models": [{"name": cfg["model"]}],
            "config": {},
            "limits": {"max_concurrency": 1},
        },
    )
    assert r.status_code in (200, 201), r.text
    return cid


async def _make_collection(
    authed_client, suffix, eid, sid, *, search: dict | None = None
) -> str:
    """Create a collection and turn search on, bound to (eid, sid).

    S2 split these into two calls: create takes no vector-space fields
    any more, and PUT /collections/{id}/search is what binds them. The
    old single-call form still returns 201, because the extra keys are
    simply ignored, so every collection built that way came out
    grep-only and every search below it answered 409.

    ``search`` merges extra keys (a cross-encoder, chunking) into the
    search config.
    """
    cfg = _embedder_cfg()
    cid = f"col-{suffix}"
    r = await authed_client.post(
        "/v1/collections",
        json={"id": cid, "description": "smk real-embedder collection",
              "system": False},
    )
    assert r.status_code in (200, 201), r.text

    body = {
        "embedder": {"provider_id": eid, "model": cfg["model"]},
        "vector_store_provider_id": sid,
        **(search or {}),
    }
    r = await authed_client.put(f"/v1/collections/{cid}/search", json=body)
    assert r.status_code in (200, 201, 202), r.text
    return cid


async def _ingest(authed_client, cid, stem, text) -> str:
    """Write a document with a body and return its id.

    The flat /v1/documents route this used to call writes an entity row
    and nothing else: S2 moved bodies into the content store, so a
    document created that way has no text to chunk and every indexing
    assertion below it was reading an empty index. Ids are assigned by
    the tree service, hence the return value.
    """
    r = await authed_client.post(
        f"/v1/collections/{cid}/docs",
        json={"parent": "", "slug": f"{stem}.md", "body": text},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _indexed(authed_client, cid, *, document_id: str | None = None) -> dict:
    params = {} if document_id is None else {"document_id": document_id}
    r = await authed_client.get(
        f"/v1/collections/{cid}/indexed_documents", params=params
    )
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# Hermetic ids (no external embedder needed)
# ===========================================================================


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
    cid = f"col-{unique_suffix}"
    create = await authed_client.post(
        "/v1/collections",
        json={"id": cid, "description": "smk collection", "system": False},
    )
    assert create.status_code in (200, 201), create.text
    got = await authed_client.get(f"/v1/collections/{cid}")
    assert got.status_code == 200
    delete = await authed_client.delete(f"/v1/collections/{cid}")
    assert delete.status_code in (200, 204), delete.text


@smk("SMK-KNW-05")
async def test_search_on_unindexed_collection_is_graceful(authed_client, unique_suffix, tmp_path):
    """Both no-results states answer clearly, and they are different.

    S2 made semantic search a per-collection opt-in, which split what
    used to be one case in two: a collection with search switched off,
    and a collection with search on but nothing indexed yet. Conflating
    them would let a misconfigured collection look merely empty.
    """
    eid, sid = await _embedder_and_ssp(authed_client, unique_suffix, tmp_path)
    cid = f"col-{unique_suffix}"
    created = await authed_client.post(
        "/v1/collections",
        json={"id": cid, "description": "empty", "system": False},
    )
    assert created.status_code == 201, created.text

    # Search off: refused, and the refusal names the route that enables it.
    off = await authed_client.post(
        f"/v1/collections/{cid}/search", json={"query": "anything", "top_k": 5},
    )
    assert off.status_code == 409, off.text
    assert "/search" in off.json()["detail"]

    # Search on, nothing indexed: an ordinary empty result.
    enabled = await authed_client.put(
        f"/v1/collections/{cid}/search",
        json={
            "embedder": {
                "provider_id": eid,
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            },
            "vector_store_provider_id": sid,
        },
    )
    assert enabled.status_code in (200, 201, 202), enabled.text

    r = await authed_client.post(
        f"/v1/collections/{cid}/search", json={"query": "anything", "top_k": 5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == []


@smk("SMK-KNW-02", "SMK-KNW-04", "SMK-KNW-06")
@requires("embedder", "pgvector")
async def test_ingest_search_and_chunks(authed_client, unique_suffix):
    """KNW-02 embed-on-ingest, KNW-04 semantic search, KNW-06 chunk listing."""
    eid = await _make_real_embedder(authed_client, unique_suffix)
    sid = await _make_pgvector_ssp(authed_client, unique_suffix)
    cid = await _make_collection(authed_client, unique_suffix, eid, sid)

    doc_ids = {
        name: await _ingest(authed_client, cid, f"doc-{name}-{unique_suffix}", text)
        for name, text in _DOCS.items()
    }

    # KNW-02: documents are embedded synchronously on create -- chunks appear
    # in the indexed-documents listing with no separate re-index step.
    listing = await _indexed(authed_client, cid)
    assert listing["total"] >= len(_DOCS), listing
    indexed_doc_ids = {item["document_id"] for item in listing["items"]}
    for did in doc_ids.values():
        assert did in indexed_doc_ids, (did, indexed_doc_ids)

    # KNW-04: semantic search returns the on-topic document. Loosened to
    # "relevant doc appears in top-k" rather than asserting a fixed score.
    r = await authed_client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "a programming language for software and data", "top_k": 3},
    )
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]
    assert hits, "real semantic search returned no hits"
    # response contract: each hit carries the documented fields.
    for h in hits:
        assert {"document_id", "chunk_id", "score", "text", "meta"} <= set(h)
    top_ids = [h["document_id"] for h in hits]
    assert doc_ids["python"] in top_ids, top_ids
    # the on-topic doc should also be the single best hit for this query.
    assert top_ids[0] == doc_ids["python"], top_ids

    # KNW-06: chunk-level inspection; document_id filter narrows to one doc.
    filtered = await _indexed(authed_client, cid, document_id=doc_ids["coffee"])
    assert filtered["total"] > 0, filtered
    assert all(
        item["document_id"] == doc_ids["coffee"] for item in filtered["items"]
    ), filtered


@smk("SMK-KNW-08")
@requires("embedder", "pgvector", "cross_encoder")
async def test_cross_encoder_rerank(authed_client, unique_suffix):
    """KNW-08: a collection with a cross-encoder reranks and still returns
    relevant hits. Ordering is loosened (real cross-encoder), but the on-topic
    docs must surface above the off-topic one.

    MMR used to ride along here under a ``mmr`` key. It is not part of
    the search config any more, and the key was being ignored, so this
    covered the cross-encoder alone even while it claimed both.
    """
    eid = await _make_real_embedder(authed_client, unique_suffix)
    sid = await _make_pgvector_ssp(authed_client, unique_suffix)
    ceid = await _make_cross_encoder(authed_client, unique_suffix)
    cfg = _cross_encoder_cfg()
    cid = await _make_collection(
        authed_client,
        unique_suffix,
        eid,
        sid,
        search={
            "cross_encoder": {
                "provider_id": ceid,
                "model": cfg["model"],
                "top_n": 10,
            },
        },
    )

    # Several overlapping (near-duplicate) Python docs plus one off-topic doc.
    overlapping = {
        "py1": "Python is a popular programming language for data science.",
        "py2": "Developers use the Python language to build web servers and APIs.",
        "py3": "Python supports object-oriented and functional programming styles.",
        "garden": "Gardening involves planting flowers and vegetables in rich soil.",
    }
    doc_ids = {
        k: await _ingest(authed_client, cid, f"doc-{k}-{unique_suffix}", text)
        for k, text in overlapping.items()
    }

    r = await authed_client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "the Python programming language", "top_k": 4},
    )
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]
    assert hits, "reranked search returned no hits"
    top_ids = [h["document_id"] for h in hits]
    # The cross-encoder must rank the on-topic Python doc first and push the
    # off-topic gardening doc out of the top spot.
    assert top_ids[0] in {doc_ids["py1"], doc_ids["py2"], doc_ids["py3"]}, top_ids
    assert top_ids[0] != doc_ids["garden"], top_ids
    # At least two of the three Python docs surface in the reranked top-4.
    py_in_top = sum(
        1 for did in (doc_ids["py1"], doc_ids["py2"], doc_ids["py3"]) if did in top_ids
    )
    assert py_in_top >= 2, top_ids


@smk("SMK-KNW-10")
@requires("embedder", "pgvector")
async def test_update_reindexes_and_delete_removes_chunks(authed_client, unique_suffix):
    """KNW-10: PUT replaces a document's chunks (no stale lingering); DELETE
    removes all chunks and drops the document from search."""
    eid = await _make_real_embedder(authed_client, unique_suffix)
    sid = await _make_pgvector_ssp(authed_client, unique_suffix)
    cid = await _make_collection(authed_client, unique_suffix, eid, sid)
    stem = f"doc-upd-{unique_suffix}"
    path = f"{stem}.md"

    did = await _ingest(
        authed_client, cid, stem,
        "The original content is about volcanoes, magma chambers, and lava flows.",
    )
    before = await _indexed(authed_client, cid, document_id=did)
    assert before["total"] > 0, before
    old_text = " ".join(item["text"] for item in before["items"]).lower()
    assert "volcano" in old_text or "lava" in old_text, old_text

    # Update with completely different text.
    new_text = (
        "Completely new content about ocean tides, marine biology, coral "
        "reefs, and the migration of fish populations."
    )
    # Path-addressed, and carrying a body: the flat /v1/documents PUT
    # rewrites the entity row only, which leaves the indexed text exactly
    # as it was and would pass this test without reindexing anything.
    r = await authed_client.put(
        f"/v1/collections/{cid}/documents",
        params={"path": path},
        json={"content": new_text},
    )
    assert r.status_code in (200, 201), r.text

    after = await _indexed(authed_client, cid, document_id=did)
    assert after["total"] > 0, after
    merged = " ".join(item["text"] for item in after["items"]).lower()
    # New content present, old content gone (no stale chunks lingered).
    assert "ocean" in merged or "marine" in merged, merged
    assert "volcano" not in merged and "lava" not in merged, merged

    # Delete removes all chunks + the doc disappears from search.
    r = await authed_client.delete(
        f"/v1/collections/{cid}/documents", params={"path": path},
    )
    assert r.status_code in (200, 204), r.text
    gone = await _indexed(authed_client, cid, document_id=did)
    assert gone["total"] == 0, gone
    r = await authed_client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "ocean tides and marine biology", "top_k": 5},
    )
    assert r.status_code == 200, r.text
    assert all(h["document_id"] != did for h in r.json()["hits"]), r.json()


@smk("SMK-KNW-09")
@requires("embedder", "pgvector")
async def test_reenabling_search_backfills_missing_vectors(
    authed_client, unique_suffix
):
    """KNW-09: documents that missed indexing are healed by a backfill.

    S2 deleted the boot-time backfill pass this used to drive in-process
    (``backfill_missing_document_vectors``) and made backfill part of an
    explicit lifecycle instead: turning search on indexes whatever is
    already in the collection. So the scenario is unchanged, but the
    trigger is now a REST call rather than a restart, and the whole test
    runs through the public API.

    The precondition -- a document row with no chunks -- is still made
    the same way: bind an unreachable embedder so the best-effort
    embed-on-write hook fails, then repair it.
    """
    cfg = _embedder_cfg()

    # 1) Write while the embedder is unreachable -> row persists, no chunks.
    bad_eid = await _make_real_embedder(
        authed_client, unique_suffix, url="http://127.0.0.1:9/v1"
    )
    sid = await _make_pgvector_ssp(authed_client, unique_suffix)
    cid = await _make_collection(authed_client, unique_suffix, bad_eid, sid)
    did = await _ingest(
        authed_client, cid, f"doc-bf-{unique_suffix}",
        "Photosynthesis converts sunlight, water, and carbon dioxide into "
        "glucose and oxygen inside chloroplasts.",
    )

    # Confirm no chunks exist yet. With pgvector the collection may not even be
    # registered in the store (it is created lazily on first successful write),
    # which the indexed_documents route surfaces as a 400 "has not been
    # created"; either way means "no vectors". Treat both as the precondition.
    r = await authed_client.get(
        f"/v1/collections/{cid}/indexed_documents", params={"document_id": did}
    )
    if r.status_code == 200:
        assert r.json()["total"] == 0, r.json()
    else:
        assert r.status_code == 400, r.text
        assert "has not been created" in r.text or "not registered" in r.text, r.text

    # 2) Repair the embedder: point it at the real LM Studio endpoint.
    rr = await authed_client.put(
        f"/v1/embedding_providers/{bad_eid}",
        json={
            "id": bad_eid,
            "provider": "openai",
            "models": [{"name": cfg["model"]}],
            "config": {"url": cfg["base_url"], "api_key": cfg["api_key"], "flavor": "lmstudio"},
            "limits": {"max_concurrency": 2},
        },
    )
    assert rr.status_code in (200, 201), rr.text

    # 3) Re-run the lifecycle: enabling search backfills the collection.
    #    The config is unchanged; it is the pass over existing documents
    #    that matters, and it now has a reachable embedder.
    enabled = await authed_client.put(
        f"/v1/collections/{cid}/search",
        json={
            "embedder": {"provider_id": bad_eid, "model": cfg["model"]},
            "vector_store_provider_id": sid,
        },
    )
    assert enabled.status_code in (200, 201, 202), enabled.text
    # A backfill that failed reports itself rather than leaving the
    # collection looking ready, so read the state instead of trusting 2xx.
    status = await authed_client.get(f"/v1/collections/{cid}/search")
    assert status.status_code == 200, status.text
    assert status.json()["state"] != "error", status.text

    # 4) The previously-unindexed document now has chunks.
    healed = await _indexed(authed_client, cid, document_id=did)
    assert healed["total"] > 0, healed

    # 5) Idempotent: a second pass leaves our document indexed and findable.
    again = await authed_client.put(
        f"/v1/collections/{cid}/search",
        json={
            "embedder": {"provider_id": bad_eid, "model": cfg["model"]},
            "vector_store_provider_id": sid,
        },
    )
    assert again.status_code in (200, 201, 202), again.text
    still = await _indexed(authed_client, cid, document_id=did)
    assert still["total"] >= healed["total"], (still, healed)
    sr = await authed_client.post(
        f"/v1/collections/{cid}/search",
        json={"query": "how plants make glucose from sunlight", "top_k": 3},
    )
    assert sr.status_code == 200, sr.text
    assert any(h["document_id"] == did for h in sr.json()["hits"]), sr.json()
