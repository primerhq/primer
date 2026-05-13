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
