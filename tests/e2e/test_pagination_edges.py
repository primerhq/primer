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


# T0011 (create N items, GET ?limit=N returns N) pruned: T0118 walks
# 7 items in chunks of 3 and T0195 walks 5 items at limit=1 — both
# subsume the "exactly limit" assertion. Removed as redundant.


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


# ============================================================================
# T0195 — pagination with limit=1 visits every seeded row exactly once
# ============================================================================


@pytest.mark.asyncio
async def test_t0195_pagination_limit_one_walks_full_set_once(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0195 — Seed N=5 rows with a shared prefix, walk the pages with
    `limit=1` using offset pagination scoped to the prefix via POST
    /find. Each seeded id must appear exactly once across the walk;
    the page count equals N (since limit=1).
    """
    prefix = f"ts-t0195-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 5)
    try:
        seen: list[str] = []
        offset = 0
        # Limit walk to a safety bound much larger than the seed
        for _ in range(20):
            body = {
                "predicate": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}-%"},
                },
                "page": {"kind": "offset", "offset": offset, "length": 1},
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            items = page["items"]
            if not items:
                break
            assert len(items) == 1, page
            seen.append(items[0]["id"])
            offset += 1

        assert sorted(seen) == sorted(ids), (
            f"limit=1 walk did not cover each seeded id exactly once. "
            f"seeded={sorted(ids)!r}, seen={sorted(seen)!r}"
        )
        # No duplicates within the walk
        assert len(seen) == len(set(seen)), (
            f"duplicates in limit=1 walk: {seen!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0214 — pagination boundary: limit=200 (max) returns 200; limit=201 → 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0214_pagination_limit_at_documented_boundary(
    client: httpx.AsyncClient,
) -> None:
    """T0214 — Spec §4 says `limit` is bounded `1..200`. T0013 covers
    far-out values (limit=500). This pins the off-by-one boundary:
    limit=200 must succeed, limit=201 must reject as 422
    /errors/validation-error.
    """
    # 200 is the documented maximum — must succeed
    ok = await client.get("/v1/toolsets?limit=200&offset=0")
    assert ok.status_code == 200, ok.text
    page = ok.json()
    # length is bounded by limit, not by available rows
    assert page["length"] <= 200, page

    # 201 is one past the maximum — must reject 422
    over = await client.get("/v1/toolsets?limit=201&offset=0")
    assert over.status_code == 422, over.text
    body = over.json()
    assert body["type"] == "/errors/validation-error", body
    assert body["status"] == 422


# ============================================================================
# T0300 — Pagination limit=0 is rejected as 422 (lower bound)
# ============================================================================


@pytest.mark.asyncio
async def test_t0300_pagination_limit_zero_rejected_422(
    client: httpx.AsyncClient,
) -> None:
    """T0300 — Spec §4 says `limit` is bounded `1..200`. T0214 covered
    the upper bound (limit=200/201). This pins the lower bound:
    limit=0 must be rejected with 422 /errors/validation-error.
    """
    resp = await client.get("/v1/toolsets?limit=0&offset=0")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error", body
    assert body["status"] == 422


# ============================================================================
# T0362 — OffsetPageResponse.total is a non-null integer for CRUD entities
# ============================================================================


@pytest.mark.asyncio
async def test_t0362_offset_page_total_is_non_null_int_for_crud_entities(
    client: httpx.AsyncClient,
) -> None:
    """T0362 — Spec §4 says the cheap-count backend MAY return None
    for `total` if it can't supply a count cheaply. Pin that the
    standard CRUD entities (Toolset, LLMProvider) DO return a real
    int total in the offset envelope (the Postgres backend is
    cheap-count for these tables).
    """
    for url in ("/v1/toolsets", "/v1/llm_providers"):
        resp = await client.get(f"{url}?limit=10&offset=0")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "total" in body, body
        total = body["total"]
        assert isinstance(total, int), (
            f"{url} `total` should be an int (cheap-count backend), "
            f"got {total!r} (type={type(total).__name__})"
        )
        assert total >= 0, (
            f"{url} `total` should be >= 0, got {total}"
        )


# ============================================================================
# T0363 — OffsetPageResponse.length equals len(items) on partial last page
# ============================================================================


@pytest.mark.asyncio
async def test_t0363_offset_page_length_equals_items_count_on_partial_page(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0363 — Stricter than T0118 (total stability). Pin that
    `length` exactly mirrors `len(items)` even on a partial last
    page (where length < the requested limit).

    Seed 7 toolsets, walk in chunks of 3 → final page has 1 entry,
    so length=1 and len(items)=1.
    """
    prefix = f"ts-t0363-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 7)
    try:
        for offset in (0, 3, 6):
            body = {
                "predicate": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
                "page": {"kind": "offset", "offset": offset, "length": 3},
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            assert "length" in page, page
            assert "items" in page, page
            assert page["length"] == len(page["items"]), (
                f"page at offset={offset} mismatch: length={page['length']} "
                f"vs len(items)={len(page['items'])}; page={page!r}"
            )
    finally:
        await _delete_toolsets(client, seeded)


# ============================================================================
# T0385 — Pagination `total` reflects the filtered set, not the table
# ============================================================================


@pytest.mark.asyncio
async def test_t0385_pagination_total_reflects_filtered_set(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0385 — POST /find with a LIKE predicate must report `total`
    equal to the count of MATCHING rows, not the whole table size.
    Seed N matching rows + M unrelated rows; total should be N.
    """
    prefix = f"ts-t0385-{unique_suffix}"
    other_prefix = f"ts-other-t0385-{unique_suffix}"
    matching_n = 4
    unrelated_m = 3

    matching_ids = await _seed_toolsets(client, prefix, matching_n)
    unrelated_ids = await _seed_toolsets(
        client, other_prefix, unrelated_m,
    )
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert page["total"] == matching_n, (
            f"total should reflect filtered set ({matching_n}), got "
            f"{page['total']}; page={page!r}"
        )
    finally:
        await _delete_toolsets(client, matching_ids)
        await _delete_toolsets(client, unrelated_ids)


# ============================================================================
# T0386 — Cursor walk does NOT have `total`; offset walk does
# ============================================================================


@pytest.mark.asyncio
async def test_t0386_cursor_response_omits_total_offset_includes_it(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0386 — Pin runtime: the same predicate evaluated with cursor
    pagination must NOT carry `total` (per spec §4 CursorPageResponse
    shape; corrected in T0255), while offset pagination MUST carry it.
    """
    prefix = f"ts-t0386-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 3)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        offset_body = {
            "predicate": predicate,
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        offset_resp = await client.post("/v1/toolsets/find", json=offset_body)
        assert offset_resp.status_code == 200, offset_resp.text
        assert "total" in offset_resp.json(), offset_resp.json()

        cursor_body = {
            "predicate": predicate,
            "page": {"kind": "cursor", "cursor": None, "length": 50},
        }
        cursor_resp = await client.post("/v1/toolsets/find", json=cursor_body)
        assert cursor_resp.status_code == 200, cursor_resp.text
        assert "total" not in cursor_resp.json(), (
            f"CursorPageResponse should NOT carry `total` (per spec §4); "
            f"got {cursor_resp.json()!r}"
        )
    finally:
        await _delete_toolsets(client, seeded)


# ============================================================================
# T0456 — POST /v1/sessions/find with page.length=10000 returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0456_find_with_excessive_length_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0456 — Per primer/model/storage.py:237/261, OffsetPage.length
    and CursorPage.length both have `ge=1` but NO upper bound on the
    model. T0214 confirmed the GET list endpoints enforce the
    documented max=200 via 422; the find POST endpoints accept the
    raw model.

    Pin observed behaviour for length=10000:
      - 200 with items list bounded to ≤ 10000 (and likely much
        smaller — there are not 10000 sessions in the test DB), OR
      - 4xx if a size cap exists at the find layer too.

    Hard contract: never 5xx, never /errors/internal — even on a
    request that asks for 10× the documented max.
    """
    body = {
        "predicate": None,
        "page": {"kind": "offset", "offset": 0, "length": 10000},
    }
    # Some find endpoints require a non-null predicate. /v1/sessions/find
    # accepts None per spec §4. If it doesn't, fall back to a trivial
    # always-true clause.
    resp = await client.post("/v1/sessions/find", json=body)
    if resp.status_code == 422 and "predicate" in resp.text.lower():
        body["predicate"] = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "%"},
        }
        resp = await client.post("/v1/sessions/find", json=body)

    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"length=10000 leaked /errors/internal: {resp.text}"
    )
    # 200 (accepted; possibly clamped server-side) or 4xx (rejected
    # with documented validation/bad-request envelope).
    assert resp.status_code in (200, 400, 422), (
        f"length=10000 unexpected status: {resp.status_code}: "
        f"{resp.text}"
    )
    if resp.status_code == 200:
        body_got = resp.json()
        items = body_got.get("items", [])
        assert isinstance(items, list), body_got
        # Whatever items came back must be bounded by the request.
        assert len(items) <= 10000, (
            f"items exceeded requested length: got {len(items)}"
        )


# ============================================================================
# T0416 — Cursor pagination with length=200 succeeds; length=201 rejected 422
# (cursor-mode mirror of T0214's offset-mode bound check)
# ============================================================================


@pytest.mark.asyncio
async def test_t0416_cursor_pagination_length_at_documented_boundary(
    client: httpx.AsyncClient,
) -> None:
    """T0416 — Spec §4 caps page length at 200. T0214 pinned the
    offset-mode boundary (limit=200/201). T0416 pins the cursor-mode
    boundary on the same boundary value via /find body (length=200
    succeeds; length=201 rejected 422 /errors/validation-error).

    Priority 4 — pagination correctness. The cursor-mode and
    offset-mode validators must agree on the upper bound.
    """
    # length=200 succeeds.
    body_ok = {
        "page": {"kind": "cursor", "cursor": None, "length": 200},
    }
    ok = await client.post("/v1/toolsets/find", json=body_ok)
    assert ok.status_code == 200, ok.text
    page = ok.json()
    assert page["kind"] == "cursor", page
    # Bounded by length, not by available rows.
    assert len(page["items"]) <= 200, page

    # length=201 rejected as 422.
    body_over = {
        "page": {"kind": "cursor", "cursor": None, "length": 201},
    }
    over = await client.post("/v1/toolsets/find", json=body_over)
    envelope = over.json() if over.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"length=201 leaked /errors/internal: "
        f"{over.status_code}: {over.text}"
    )
    assert over.status_code == 422, (
        f"cursor length=201 should be 422; got "
        f"{over.status_code}: {over.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope
