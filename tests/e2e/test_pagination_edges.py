"""E2E: pagination edge cases per §4 of the app spec.

Covers backlog items T0011, T0012, T0013.
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
    """Create ``count`` toolsets with a shared prefix; return their ids."""
    ids = [f"{prefix}-{i:02d}" for i in range(count)]
    for entity_id in ids:
        resp = await client.post("/v1/toolsets", json=_toolset_body(entity_id))
        assert resp.status_code == 201, resp.text
    return ids


async def _delete_toolsets(client: httpx.AsyncClient, ids: list[str]) -> None:
    for entity_id in ids:
        await client.delete(f"/v1/toolsets/{entity_id}")


@pytest.mark.asyncio
async def test_t0011_pagination_exactly_limit_items(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0011 — create N items, GET with ?limit=N returns exactly N."""
    prefix = f"ts-t0011-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 5)
    try:
        resp = await client.get("/v1/toolsets?limit=5&offset=0")
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert page["length"] == 5
        assert len(page["items"]) == 5
        # Total is across the whole table, but it must be >= what we
        # just inserted.
        assert page["total"] >= 5
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0012_pagination_limit_plus_one_spans_pages(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0012 — create 6 items; with limit=5 the offset=5 page returns the 6th."""
    prefix = f"ts-t0012-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 6)
    try:
        # Filter by id-prefix via the predicate find endpoint so the
        # assertion is robust against any toolsets a sibling test
        # might have left behind (per-test DB reset only happens
        # between iterations).
        find_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 5, "length": 5},
        }
        resp = await client.post("/v1/toolsets/find", json=find_body)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert page["total"] == 6, f"prefix-filtered total mismatch: {page!r}"
        # offset=5 with 6 prefix-matching items → length 1.
        assert page["length"] == 1
        assert len(page["items"]) == 1
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0118_pagination_total_stable_across_pages(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0118 — walking 7 seeded items in chunks of 3 (offsets 0/3/6),
    every page must report `total=7`. Catches any total-recomputation
    drift between pages, e.g. if the count query were to ignore the
    predicate filter while the items query honoured it.
    """
    prefix = f"ts-t0118-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 7)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }
        seen: list[str] = []
        for offset in (0, 3, 6):
            body = {
                "predicate": predicate,
                "page": {"kind": "offset", "offset": offset, "length": 3},
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert page["total"] == 7, (
                f"total drifted at offset={offset}: {page['total']}"
            )
            seen.extend(item["id"] for item in page["items"])
        # No duplicates, full coverage
        assert len(seen) == 7, seen
        assert sorted(seen) == sorted(ids), (
            f"walk did not cover seeded set: walked={sorted(seen)!r}, "
            f"expected={sorted(ids)!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0077_pagination_offset_above_total_returns_empty(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0077 — when `offset` exceeds `total`, the response is a normal
    OffsetPageResponse with `items=[]`, `length=0`, but `total`
    reflects the true row count.

    Filter via predicate so the assertion isn't disturbed by leftover
    rows from a sibling test in the same iteration.
    """
    prefix = f"ts-t0077-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 10, "length": 5},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert page["kind"] == "offset"
        assert page["items"] == []
        assert page["length"] == 0
        assert page["total"] == 2, page
        assert page["offset"] == 10
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0013_pagination_limit_out_of_range_rejected(
    client: httpx.AsyncClient,
) -> None:
    """T0013 — limit=500 (above the documented 1..200 cap) yields 422."""
    resp = await client.get("/v1/toolsets?limit=500&offset=0")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error"
    assert body["status"] == 422
