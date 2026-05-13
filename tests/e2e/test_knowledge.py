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
