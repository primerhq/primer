"""E2E: Collection / Document referential behaviour.

Covers backlog item T0068. Spec §10 says POSTing a Document is a
storage-row operation that doesn't gate on the vector store. The open
question this test pins: what happens when the row references a
collection_id that doesn't exist?

Acceptable responses (any of which is a clean RFC 7807 envelope):
- 404 with /errors/not-found (referential integrity caught at the API)
- 422 with /errors/validation-error (caught at the validation layer)
- 200/201 (referential integrity is NOT enforced at create-time;
  documented in passing rather than failing)

The test asserts ONLY the negative invariant: no 500 leaks through.
The actual path the implementation takes is recorded in the response,
so a future regression that flips the contract will be visible in
diff but won't itself fail this test.
"""

from __future__ import annotations

import httpx
import pytest


def _document_body(*, doc_id: str, collection_id: str) -> dict:
    return {
        "id": doc_id,
        "name": "test doc",
        "collection_id": collection_id,
        "text": "hello world",
        "meta": {},
    }


@pytest.mark.asyncio
async def test_t0068_document_create_with_missing_collection_id_no_500(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    doc_id = f"doc-t0068-{unique_suffix}"
    missing_collection_id = f"never-existed-{unique_suffix}"
    body = _document_body(doc_id=doc_id, collection_id=missing_collection_id)

    resp = await client.post("/v1/documents", json=body)

    # Hard contract: no 500 envelope. The handler must convert any
    # referential mishap into an ordinary 4xx (or accept the row
    # outright if there's no enforcement at create-time).
    assert resp.status_code != 500, (
        f"unhandled exception leaked through as 500: {resp.text}"
    )
    assert resp.status_code < 500, (
        f"unexpected 5xx on missing collection_id: "
        f"{resp.status_code}: {resp.text}"
    )

    if resp.status_code in (200, 201):
        # Referential integrity is NOT enforced at create-time.
        # Clean up the orphan row to keep the iteration tidy.
        await client.delete(f"/v1/documents/{doc_id}")
    else:
        # 4xx — must carry the documented RFC 7807 envelope shape.
        envelope = resp.json()
        for key in ("type", "title", "status", "detail"):
            assert key in envelope, (
                f"problem-details key {key!r} missing in {envelope!r}"
            )
        assert envelope["status"] == resp.status_code
        assert envelope["type"].startswith("/errors/"), envelope


@pytest.mark.asyncio
async def test_t0129_orphan_document_collection_documents_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0129 — when a Document references a collection_id that doesn't
    exist (T0068 confirmed POST allows orphans), the read path that
    enumerates documents by that collection must return cleanly:
    either an empty list (the orphan is invisible there, treated as
    if the collection itself doesn't exist) or 404 (no such
    collection). Either is a clean envelope — the pin is "no 5xx".
    """
    orphan_cid = f"never-existed-{unique_suffix}"
    doc_id = f"doc-orphan-{unique_suffix}"

    create = await client.post(
        "/v1/documents",
        json={
            "id": doc_id,
            "name": "orphan",
            "collection_id": orphan_cid,
            "meta": {},
        },
    )
    assert create.status_code in (200, 201), create.text
    try:
        # GET /v1/collections/{cid}/documents for the orphan id
        resp = await client.get(f"/v1/collections/{orphan_cid}/documents")
        assert resp.status_code != 500, resp.text
        if resp.status_code == 200:
            # Implementation enumerates by collection_id from storage —
            # the orphan doc DOES carry this id, so it might appear,
            # OR the route validates the collection exists first and
            # the body's empty / items=[]. Either is clean.
            page = resp.json()
            # offset envelope shape regardless
            assert "items" in page, page
            # If the orphan does show up here, that's documented
            # behaviour; if not, that's also documented. The strict
            # invariant is "no internal-error envelope".
        elif resp.status_code == 404:
            envelope = resp.json()
            assert envelope["type"] == "/errors/not-found", envelope
        else:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await client.delete(f"/v1/documents/{doc_id}")


@pytest.mark.asyncio
async def test_t0108_document_put_replaces_name_and_metadata(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0108 — PUT a Document with new `name` + `meta`; subsequent GET
    reflects the new body, id is unchanged.

    NB: the `Document` Pydantic model only declares `id`,
    `collection_id`, `name`, `meta` — `text` was not an actual model
    field, so this test focuses on the fields the API actually
    persists and echoes back.

    Per T0068 the API doesn't enforce referential integrity at create-
    time, so we use a placeholder collection_id.
    """
    doc_id = f"doc-t0108-{unique_suffix}"
    initial = {
        "id": doc_id,
        "name": "initial",
        "collection_id": f"unenforced-{unique_suffix}",
        "meta": {"version": 1, "tag": "old"},
    }
    create = await client.post("/v1/documents", json=initial)
    assert create.status_code in (200, 201), create.text
    try:
        replacement = {
            "id": doc_id,
            "name": "replaced",
            "collection_id": f"unenforced-{unique_suffix}",
            "meta": {"version": 2, "tag": "new"},
        }
        put = await client.put(f"/v1/documents/{doc_id}", json=replacement)
        assert put.status_code == 200, put.text

        got = await client.get(f"/v1/documents/{doc_id}")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["id"] == doc_id  # id unchanged
        assert body["name"] == "replaced", body
        assert body["meta"] == {"version": 2, "tag": "new"}, body
    finally:
        await client.delete(f"/v1/documents/{doc_id}")


@pytest.mark.asyncio
async def test_t0087_multi_key_order_by_breaks_ties(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0087 — `order_by` with multiple keys applies them left-to-right.
    Two rows tied on the primary key are ordered by the secondary,
    and reversing the secondary's direction flips just the tied pair.
    """
    prefix = f"doc-t0087-{unique_suffix}"
    # Two docs share name "alpha" (the tie); a third has "bravo"
    rows = [
        {"id": f"{prefix}-1", "name": "alpha"},
        {"id": f"{prefix}-2", "name": "alpha"},
        {"id": f"{prefix}-3", "name": "bravo"},
    ]
    created: list[str] = []
    try:
        for r in rows:
            body = {
                "id": r["id"],
                "name": r["name"],
                "collection_id": f"unenforced-{unique_suffix}",
                "text": "x",
                "meta": {},
            }
            resp = await client.post("/v1/documents", json=body)
            assert resp.status_code in (200, 201), resp.text
            created.append(r["id"])

        find_body_template = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }

        # Primary asc by name; secondary desc by id
        body_a = {
            **find_body_template,
            "order_by": [
                {"field": "name", "direction": "asc"},
                {"field": "id", "direction": "desc"},
            ],
        }
        resp_a = await client.post("/v1/documents/find", json=body_a)
        assert resp_a.status_code == 200, resp_a.text
        ids_a = [item["id"] for item in resp_a.json()["items"]]
        # Tie on "alpha": the two alpha rows ordered by id desc → -2, -1
        # Then the "bravo" row → -3
        assert ids_a == [
            f"{prefix}-2", f"{prefix}-1", f"{prefix}-3",
        ], f"unexpected order with [name asc, id desc]: {ids_a!r}"

        # Now flip secondary to id asc: only the tied pair flips
        body_b = {
            **find_body_template,
            "order_by": [
                {"field": "name", "direction": "asc"},
                {"field": "id", "direction": "asc"},
            ],
        }
        resp_b = await client.post("/v1/documents/find", json=body_b)
        assert resp_b.status_code == 200, resp_b.text
        ids_b = [item["id"] for item in resp_b.json()["items"]]
        assert ids_b == [
            f"{prefix}-1", f"{prefix}-2", f"{prefix}-3",
        ], f"unexpected order with [name asc, id asc]: {ids_b!r}"
    finally:
        for did in created:
            await client.delete(f"/v1/documents/{did}")


@pytest.mark.asyncio
async def test_t0088_order_by_jsonb_null_path_is_stable(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0088 — ordering on a JSONB nested key (`meta.tag`) where some
    rows have it set and others don't must:
    - not 500
    - produce a deterministic ordering across two consecutive calls

    Postgres' default `NULLS LAST` for asc / `NULLS FIRST` for desc
    is the conventional placement, but this test pins only stability,
    not the specific position.
    """
    prefix = f"doc-t0088-{unique_suffix}"
    rows = [
        {"id": f"{prefix}-1", "meta": {"tag": "z"}},
        {"id": f"{prefix}-2", "meta": {}},  # no tag
        {"id": f"{prefix}-3", "meta": {"tag": "a"}},
        {"id": f"{prefix}-4", "meta": {}},  # no tag
    ]
    created: list[str] = []
    try:
        for r in rows:
            body = {
                "id": r["id"],
                "name": "x",
                "collection_id": f"unenforced-{unique_suffix}",
                "text": "x",
                "meta": r["meta"],
            }
            resp = await client.post("/v1/documents", json=body)
            assert resp.status_code in (200, 201), resp.text
            created.append(r["id"])

        find_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
            "order_by": [{"field": "meta.tag", "direction": "asc"}],
        }

        first = await client.post("/v1/documents/find", json=find_body)
        assert first.status_code == 200, first.text
        ids_first = [item["id"] for item in first.json()["items"]]

        second = await client.post("/v1/documents/find", json=find_body)
        assert second.status_code == 200, second.text
        ids_second = [item["id"] for item in second.json()["items"]]

        # Same set of ids, identical ordering across the two calls.
        assert sorted(ids_first) == sorted([r["id"] for r in rows]), ids_first
        assert ids_first == ids_second, (
            f"order_by on a nullable JSONB key is unstable across calls: "
            f"first={ids_first!r}, second={ids_second!r}"
        )
    finally:
        for did in created:
            await client.delete(f"/v1/documents/{did}")


@pytest.mark.asyncio
async def test_t0074_order_by_jsonb_nested_path_sorts_deterministically(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0074 — find with `order_by=[{field: "meta.tag", direction: ...}]`
    sorts rows lexicographically by the JSONB-nested `meta.tag` value.

    Documents are the simplest entity with a free-form `meta` dict.
    Insert three rows with `meta.tag` deliberately out of order; the
    asc walk must put them in lexical order.

    Filters by id-prefix LIKE so the sort applies to the seeded set
    only. Per T0068 the referential integrity isn't enforced, so the
    `collection_id` doesn't need to point at a real row.
    """
    prefix = f"doc-t0074-{unique_suffix}"
    docs = [
        {"id": f"{prefix}-c", "tag": "ccc"},
        {"id": f"{prefix}-a", "tag": "aaa"},
        {"id": f"{prefix}-b", "tag": "bbb"},
    ]
    # Insert in non-sorted order so the test exercises the sort, not
    # insertion order leakage.
    created: list[str] = []
    try:
        for d in docs:
            body = {
                "id": d["id"],
                "name": f"doc {d['tag']}",
                "collection_id": f"unenforced-{unique_suffix}",
                "text": "ignored",
                "meta": {"tag": d["tag"]},
            }
            resp = await client.post("/v1/documents", json=body)
            assert resp.status_code in (200, 201), resp.text
            created.append(d["id"])

        find_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
            "order_by": [{"field": "meta.tag", "direction": "asc"}],
        }
        resp = await client.post("/v1/documents/find", json=find_body)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        tags = [item["meta"]["tag"] for item in items]
        assert tags == ["aaa", "bbb", "ccc"], (
            f"expected ['aaa','bbb','ccc'] sorted, got {tags!r}"
        )
        # Sanity: corresponding ids are in matching order.
        ids = [item["id"] for item in items]
        assert ids == [f"{prefix}-a", f"{prefix}-b", f"{prefix}-c"], ids
    finally:
        for did in created:
            await client.delete(f"/v1/documents/{did}")


# ============================================================================
# T0177 — Collection POST with missing embedder.provider_id orphan-tolerated
# ============================================================================


@pytest.mark.asyncio
async def test_t0177_collection_with_missing_embedder_provider_orphan_tolerated(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0177 — mirror of T0068 for Collection→EmbeddingProvider. The
    Collection.embedder.provider_id is a foreign-key-like reference; the
    API may or may not enforce it at POST time.

    Hard contract: no 500 leak. If accepted, the orphan row is visible
    through GET and the documents-list path; if rejected, the envelope
    is a clean 4xx.
    """
    coll_id = f"coll-t0177-{unique_suffix}"
    missing_embedder = f"never-existed-emb-{unique_suffix}"
    body = {
        "id": coll_id,
        "description": "orphan-embedder probe",
        "embedder": {
            "provider_id": missing_embedder,
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        },
    }

    resp = await client.post("/v1/collections", json=body)
    assert resp.status_code != 500, resp.text
    assert resp.status_code < 500, resp.text

    if resp.status_code in (200, 201):
        # Permissive: orphan accepted
        try:
            # GET the row back
            got = await client.get(f"/v1/collections/{coll_id}")
            assert got.status_code == 200, got.text
            assert got.json()["embedder"]["provider_id"] == missing_embedder

            # Document list under the orphan collection must respond
            # cleanly (empty list OR a clean 4xx)
            docs = await client.get(f"/v1/collections/{coll_id}/documents")
            assert docs.status_code != 500, docs.text
            assert docs.status_code < 500, docs.text
            if docs.status_code == 200:
                assert isinstance(docs.json().get("items"), list), docs.json()
        finally:
            await client.delete(f"/v1/collections/{coll_id}")
    else:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope
