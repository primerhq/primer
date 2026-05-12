"""E2E: find endpoint shapes + cursor pagination.

Covers backlog items T0014 (cursor pagination round-trip),
T0015 (find with empty predicate), T0016 (malformed predicate → 422).
"""

from __future__ import annotations

import httpx
import pytest


def _toolset_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }


async def _seed_toolsets(
    client: httpx.AsyncClient, prefix: str, count: int,
) -> list[str]:
    ids = [f"{prefix}-{i:02d}" for i in range(count)]
    for entity_id in ids:
        resp = await client.post("/v1/toolsets", json=_toolset_body(entity_id))
        assert resp.status_code == 201, resp.text
    return ids


async def _delete_toolsets(client: httpx.AsyncClient, ids: list[str]) -> None:
    for entity_id in ids:
        await client.delete(f"/v1/toolsets/{entity_id}")


@pytest.mark.asyncio
async def test_t0014_cursor_pagination_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0014 — find with cursor pagination paginates through the page set
    and the final page returns ``next_cursor`` of ``null``.

    Filters on a unique id-prefix so the cursor walk is over exactly the
    7 toolsets the test created.
    """
    prefix = f"ts-t0014-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 7)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        seen: list[str] = []
        cursor: str | None = None
        # Walk pages of size 3 until exhausted. 7 items / 3 per page → 3
        # pages (3, 3, 1). Cap the walk to avoid an infinite loop if the
        # cursor contract regresses.
        for _ in range(10):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 3},
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert page["kind"] == "cursor", f"expected cursor envelope, got {page!r}"
            seen.extend(item["id"] for item in page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail(
                "cursor walk did not terminate within 10 pages: "
                f"seen={seen!r}, last cursor={cursor!r}"
            )

        assert sorted(seen) == sorted(ids), (
            f"cursor walk missed items: walked={sorted(seen)!r}, "
            f"expected={sorted(ids)!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0015_find_empty_predicate_returns_offset_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0015 — `POST /v1/toolsets/find` with no predicate returns an
    OffsetPageResponse with a consistent total."""
    body = {"page": {"kind": "offset", "offset": 0, "length": 50}}
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert page["kind"] == "offset"
    assert page["offset"] == 0
    # Cap-on-fresh-DB: items match length, length never exceeds total.
    assert isinstance(page["items"], list)
    assert page["length"] == len(page["items"])
    assert page["length"] <= page["total"]


@pytest.mark.asyncio
async def test_t0044_cursor_consistency_under_mid_walk_insert(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0044 — cursor pagination is stable under mid-walk inserts.

    1. Seed N items with a unique id-prefix.
    2. Open a cursor walk filtered by that prefix, fetch one page.
    3. Insert a NEW row matching the prefix.
    4. Continue the walk.
    5. Assert: no item id appears twice across all pages, and every
       item from the original snapshot appears at least once. The new
       insert MAY appear (consistency model isn't snapshot-isolated)
       or MAY NOT — but it must not corrupt the walk.
    """
    prefix = f"ts-t0044-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 5)
    inserted_id = f"{prefix}-99"
    inserted = False
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }
        seen: list[str] = []
        cursor: str | None = None
        page_no = 0
        for _ in range(15):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 2},
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert page["kind"] == "cursor"
            seen.extend(item["id"] for item in page["items"])

            # After the first page, insert one more matching row.
            page_no += 1
            if page_no == 1 and not inserted:
                ins = await client.post(
                    "/v1/toolsets", json=_toolset_body(inserted_id),
                )
                assert ins.status_code == 201, ins.text
                inserted = True

            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail(f"cursor walk did not terminate: seen={seen!r}")

        # Invariant 1 — no id appears twice.
        assert len(seen) == len(set(seen)), (
            f"cursor walk yielded duplicate ids: {seen!r}"
        )
        # Invariant 2 — every snapshot item appears at least once.
        for sid in seeded:
            assert sid in seen, (
                f"snapshot id {sid!r} missing from walk: {seen!r}"
            )
    finally:
        await _delete_toolsets(client, [*seeded, inserted_id])


@pytest.mark.asyncio
async def test_t0016_find_malformed_predicate_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """T0016 — a predicate whose shape doesn't match the schema yields
    422 with /errors/validation-error."""
    body = {
        "predicate": {"this is": "not a valid predicate tree"},
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error"
    assert envelope["status"] == 422
