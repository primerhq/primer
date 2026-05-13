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
async def test_t0069_predicate_eq_filters_to_named_row(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0069 — predicate `op="="` returns ONLY the row whose `id`
    matches the literal value. No partial / prefix matches.
    """
    prefix = f"ts-t0069-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    target = ids[0]
    other = ids[1]
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": target},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = [item["id"] for item in resp.json()["items"]]
        assert out_ids == [target], (
            f"expected only {target!r}, got {out_ids!r}"
        )
        assert other not in out_ids
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0070_predicate_ne_excludes_named_row(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0070 — predicate `op="!="` returns every prefix-matching row
    EXCEPT the one whose id equals the literal value.

    Filters by id-prefix (LIKE) AND'd with `id != target` so the
    assertion isn't disturbed by toolsets created by other tests in
    the same iteration.
    """
    prefix = f"ts-t0070-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    excluded = ids[0]
    expected_remainder = sorted(ids[1:])
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
                "right": {
                    "kind": "predicate",
                    "op": "!=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": excluded},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == expected_remainder, (
            f"expected {expected_remainder!r}, got {out_ids!r}"
        )
        assert excluded not in out_ids
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0081_predicate_gt_returns_strictly_greater(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0081 — predicate `op=">"` on the text `id` field returns only
    rows whose id is lexically strictly-greater than the literal.
    Strings ARE comparable in SQL, and `id` is a real text column
    (no JSONB cast issues).
    """
    prefix = f"ts-t0081-{unique_suffix}"
    # Suffix letters give a deterministic lexical order: a < b < c
    ids = [f"{prefix}-{c}" for c in "abc"]
    for sid in ids:
        resp = await client.post("/v1/toolsets", json=_toolset_body(sid))
        assert resp.status_code == 201, resp.text
    try:
        threshold = ids[1]  # "...-b"; expect only "...-c" above it
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
                "right": {
                    "kind": "predicate",
                    "op": ">",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": threshold},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == [ids[2]], (
            f"expected only [{ids[2]!r}] strictly above threshold "
            f"{threshold!r}, got {out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0082_predicate_le_inclusive_at_boundary(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0082 — `op="<="` is INCLUSIVE at the boundary. The row whose id
    equals the literal must appear in the result set.
    """
    prefix = f"ts-t0082-{unique_suffix}"
    ids = [f"{prefix}-{c}" for c in "abc"]
    for sid in ids:
        resp = await client.post("/v1/toolsets", json=_toolset_body(sid))
        assert resp.status_code == 201, resp.text
    try:
        threshold = ids[1]  # "...-b"; expect "...-a" and "...-b"
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
                "right": {
                    "kind": "predicate",
                    "op": "<=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": threshold},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == sorted([ids[0], ids[1]]), (
            f"<= should include the boundary row; expected "
            f"{[ids[0], ids[1]]!r}, got {out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0083_predicate_ge_and_lt_partition_set(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0083 — `>=N` and `<N` over the same field on the same set are
    disjoint and union to the whole. Pins the exclusivity invariant
    of the strict / non-strict pair across the boundary.
    """
    prefix = f"ts-t0083-{unique_suffix}"
    ids = [f"{prefix}-{c}" for c in "abcd"]
    for sid in ids:
        resp = await client.post("/v1/toolsets", json=_toolset_body(sid))
        assert resp.status_code == 201, resp.text
    try:
        threshold = ids[1]  # "...-b"

        async def _walk(op: str) -> set[str]:
            body = {
                "predicate": {
                    "kind": "predicate",
                    "op": "and",
                    "left": {
                        "kind": "predicate",
                        "op": "~=",
                        "left": {"kind": "field", "name": "id"},
                        "right": {"kind": "value", "value": f"{prefix}%"},
                    },
                    "right": {
                        "kind": "predicate",
                        "op": op,
                        "left": {"kind": "field", "name": "id"},
                        "right": {"kind": "value", "value": threshold},
                    },
                },
                "page": {"kind": "offset", "offset": 0, "length": 50},
            }
            r = await client.post("/v1/toolsets/find", json=body)
            assert r.status_code == 200, r.text
            return {item["id"] for item in r.json()["items"]}

        ge = await _walk(">=")
        lt = await _walk("<")
        # Disjoint
        assert not (ge & lt), f"`>=` and `<` overlap on {ge & lt}"
        # Cover the whole set
        assert ge | lt == set(ids), (
            f"union of `>=` ({ge}) and `<` ({lt}) does not equal "
            f"the seeded set {set(ids)!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0084_predicate_unknown_field_returns_4xx(
    client: httpx.AsyncClient,
) -> None:
    """T0084 — predicate referencing a field that doesn't exist on the
    model returns a clean 4xx envelope, not 500.

    The predicate translator raises ``BadRequestError`` when
    ``model_class.model_fields.get(parts[0])`` is None, which the
    error mapper serialises as 400 ``/errors/bad-request``.
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "nope_xyz_field"},
            "right": {"kind": "value", "value": "anything"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code != 500, (
        f"unhandled exception leaked through as 500: {resp.text}"
    )
    assert 400 <= resp.status_code < 500, (
        f"expected 4xx envelope on unknown field, got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, envelope
    assert envelope["status"] == resp.status_code
    assert envelope["type"].startswith("/errors/"), envelope
    # The detail should mention the bogus field name so an operator
    # can fix the request.
    assert "nope_xyz_field" in envelope["detail"], envelope


@pytest.mark.asyncio
async def test_t0085_predicate_type_mismatch_no_internal_error(
    client: httpx.AsyncClient,
) -> None:
    """T0085 — comparing a string field (`id`) against an integer
    literal must NOT leak through as `/errors/internal` (the
    catch-all 500). Acceptable behaviour:

    - 200 with empty items (storage accepts the comparison and finds
      no matches)
    - 4xx with the documented validation/bad-request slug
    - 502 with `/errors/provider-server-error` (asyncpg rejects the
      bind: "invalid input for query argument $1: 42 (expected str,
      got int)") — this is a clean envelope, just surfacing the
      Postgres-level type mismatch as an upstream-provider failure

    Pin the invariant: the response must be a clean RFC 7807 envelope
    and the slug must NOT be `/errors/internal`.
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": 42},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    if resp.status_code == 200:
        page = resp.json()
        assert page["kind"] == "offset"
        assert page["items"] == []
        return
    # Any non-200 must be a documented error envelope, NOT the
    # catch-all /errors/internal.
    envelope = resp.json()
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, envelope
    assert envelope["status"] == resp.status_code
    assert envelope["type"].startswith("/errors/"), envelope
    assert envelope["type"] != "/errors/internal", (
        f"type-mismatch predicate leaked as /errors/internal: {envelope!r}"
    )


@pytest.mark.asyncio
async def test_t0121_find_explicit_null_predicate_equivalent_to_omitting(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0121 — `{"predicate": null, "page": ...}` must be equivalent
    to omitting the predicate field entirely. The set of returned
    item ids is identical between the two forms.
    """
    prefix = f"ts-t0121-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        page = {"kind": "offset", "offset": 0, "length": 200}

        with_null = await client.post(
            "/v1/toolsets/find", json={"predicate": None, "page": page},
        )
        assert with_null.status_code == 200, with_null.text

        without = await client.post(
            "/v1/toolsets/find", json={"page": page},
        )
        assert without.status_code == 200, without.text

        # Compare item-id SETS (ordering may not be guaranteed without
        # an order_by, but membership must match).
        ids_a = {item["id"] for item in with_null.json()["items"]}
        ids_b = {item["id"] for item in without.json()["items"]}
        assert ids_a == ids_b, (
            f"explicit null predicate diverges from omitted: "
            f"with_null={sorted(ids_a)!r}, without={sorted(ids_b)!r}"
        )
        # And both contain the seeded set
        for sid in ids:
            assert sid in ids_a, sid
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0086_predicate_like_uppercase_rejected_422(
    client: httpx.AsyncClient,
) -> None:
    """T0086 — pin the wire op contract: `op="LIKE"` (uppercase, the
    SQL-symbol form) is REJECTED with 422. The valid wire value is
    `"~="`. The Op enum's *internal name* is LIKE; serialising as the
    name instead of the value is a common client mistake.
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "LIKE",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "ts-%"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope
    assert envelope["status"] == 422


@pytest.mark.asyncio
async def test_t0071_predicate_in_returns_listed_values_only(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0071 — predicate `op="in"` matches rows whose `id` equals any
    element of a literal list. Right operand is a Value carrying a
    list of scalars per the documented IN semantics.
    """
    prefix = f"ts-t0071-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    selected = sorted(ids[:2])
    excluded = ids[2]
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "in",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": selected},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == selected, (
            f"expected {selected!r}, got {out_ids!r}"
        )
        assert excluded not in out_ids
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0072_predicate_and_narrows_to_intersection(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0072 — `op="and"` requires Predicate on both sides; only rows
    satisfying BOTH clauses are returned. Compose `id LIKE prefix%`
    with `id = target` — only target qualifies.
    """
    prefix = f"ts-t0072-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    target = ids[1]
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": target},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = [item["id"] for item in resp.json()["items"]]
        assert out_ids == [target], (
            f"AND should yield only {target!r}; got {out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0073_predicate_or_unions_matches_no_duplicates(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0073 — `op="or"` returns rows satisfying EITHER clause; rows
    that satisfy both must NOT appear twice. Compose two distinct
    `id = ...` clauses against two different rows.
    """
    prefix = f"ts-t0073-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    a, b = ids[0], ids[2]
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "or",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": a},
                },
                "right": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": b},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = [item["id"] for item in resp.json()["items"]]
        assert sorted(out_ids) == sorted([a, b]), (
            f"OR should yield exactly {{a, b}}, got {out_ids!r}"
        )
        assert len(out_ids) == len(set(out_ids)), (
            f"OR yielded duplicates: {out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0078_find_no_predicate_with_page_returns_full_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0078 — `POST /v1/toolsets/find` with no predicate (predicate
    omitted) BUT a valid `page` returns the unfiltered list.

    NB: the original backlog wording said "empty body `{}` returns
    full list". The actual contract requires `page` to be supplied
    (FastAPI rejects `{}` with 422 for missing `page` field). This
    test pins the closest meaningful contract: predicate is optional,
    page is required.
    """
    prefix = f"ts-t0078-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        # predicate omitted; page supplied
        resp = await client.post(
            "/v1/toolsets/find",
            json={"page": {"kind": "offset", "offset": 0, "length": 200}},
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        assert page["kind"] == "offset"
        assert page["offset"] == 0
        assert page["total"] >= 3, page
        out_ids = {item["id"] for item in page["items"]}
        for sid in ids:
            assert sid in out_ids, (
                f"seeded id {sid!r} missing from page items: "
                f"{sorted(out_ids)!r}"
            )

        # And confirm that the all-empty body IS rejected with 422 —
        # this is the negative-case half of the contract pin.
        empty = await client.post("/v1/toolsets/find", json={})
        assert empty.status_code == 422, empty.text
        assert empty.json()["type"] == "/errors/validation-error"
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0043_find_order_by_asc_then_desc_reverses(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0043 — POST /v1/toolsets/find with order_by asc vs desc on the
    `id` field returns the same items in reversed order.

    The seeded ids are zero-padded so the lexical order is deterministic.
    Filter by id-prefix so the sort applies only to seeded items
    regardless of what other tests left in the table.
    """
    prefix = f"ts-t0043-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 4)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        async def _walk(direction: str) -> list[str]:
            body = {
                "predicate": predicate,
                "page": {"kind": "offset", "offset": 0, "length": 50},
                "order_by": [{"field": "id", "direction": direction}],
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            return [item["id"] for item in resp.json()["items"]]

        ascending = await _walk("asc")
        descending = await _walk("desc")

        # Same set of items regardless of direction.
        assert sorted(ascending) == sorted(descending) == sorted(ids), (
            f"asc={ascending!r} desc={descending!r} expected={sorted(ids)!r}"
        )
        # Reversal: desc must equal ascending reversed, exactly.
        assert descending == list(reversed(ascending)), (
            f"desc {descending!r} is not the reverse of asc {ascending!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


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
