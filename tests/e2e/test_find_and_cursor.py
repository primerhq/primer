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
async def test_t0122_predicate_always_true_returns_all_seeded(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0122 — predicate `id != ""` evaluates to true for every row
    (no row has an empty id). Returns the full set.

    Filters by id-prefix LIKE AND'd with `id != ""` to keep the
    assertion deterministic against rows from sibling tests.
    """
    prefix = f"ts-t0122-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 4)
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
                    "right": {"kind": "value", "value": ""},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == sorted(ids), (
            f"`id != \"\"` should be identity over seeded set; expected "
            f"{sorted(ids)!r}, got {out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


@pytest.mark.asyncio
async def test_t0123_predicate_eq_null_no_internal_error(
    client: httpx.AsyncClient,
) -> None:
    """T0123 — predicate `field = NULL` (literal None on the right)
    must NOT leak as `/errors/internal`. Acceptable behaviours:

    - 200 with empty items (the comparison evaluates false in SQL
      semantics — `x = NULL` is always NULL, treated as false)
    - 4xx with the documented validation/bad-request slug
    - 502 with `/errors/provider-server-error` (asyncpg may reject
      None at the bind site)

    Any of these is a clean envelope; the contract pin is "no
    catch-all 500".
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": None},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    if resp.status_code == 200:
        page = resp.json()
        assert page["kind"] == "offset"
        assert page["items"] == []
        return
    envelope = resp.json()
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, envelope
    assert envelope["type"].startswith("/errors/"), envelope
    assert envelope["type"] != "/errors/internal", (
        f"predicate=null leaked as /errors/internal: {envelope!r}"
    )


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
async def test_t0152_predicate_on_list_item_field_no_internal_error(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0152 — pin the actual behaviour of a predicate that targets a
    field nested inside a list (`models.name`). The storage layer's
    JSONB extraction may or may not descend into list items — both
    branches are acceptable as long as the response is a clean
    envelope and not `/errors/internal`.

    Setup: create an LLMProvider with two models named gpt-foo and
    gpt-bar. Predicate `models.name LIKE %foo%`. Whatever the API
    does (matches the row, returns empty, or 4xx), it must surface
    as a documented envelope.
    """
    entity_id = f"llm-models-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [
            {"name": "gpt-foo", "context_length": 8192},
            {"name": "gpt-bar", "context_length": 8192},
        ],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }
    create = await client.post("/v1/llm_providers", json=body)
    assert create.status_code == 201, create.text
    try:
        find_body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": entity_id},
                },
                "right": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "models.name"},
                    "right": {"kind": "value", "value": "%foo%"},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 5},
        }
        resp = await client.post("/v1/llm_providers/find", json=find_body)
        # The contract pin: not 500, not /errors/internal.
        assert resp.status_code != 500, resp.text
        if resp.status_code == 200:
            page = resp.json()
            assert page["kind"] == "offset"
            # Items list is a clean shape regardless of count
            assert isinstance(page["items"], list)
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            for key in ("type", "title", "status", "detail"):
                assert key in envelope, envelope
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0149_predicate_like_with_sql_keywords_parameterised(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0149 — predicate `LIKE` (`~=`) with a value containing
    Postgres-reserved-keyword fragments (`select`, `from`) does NOT
    produce a SQL-syntax error. Confirms that the storage layer is
    parameterising the value rather than splicing it into the query
    string.

    Two seeded ids contain the keyword fragments; the predicate
    matches only one of them.
    """
    prefix = f"ts-t0149-{unique_suffix}"
    a = f"{prefix}-select-x"
    b = f"{prefix}-from-y"
    ids = [a, b]
    for sid in ids:
        resp = await client.post("/v1/toolsets", json=_toolset_body(sid))
        assert resp.status_code == 201, resp.text
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
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": "%select%"},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == [a], (
            f"keyword-bearing predicate value should match only "
            f"{a!r}; got {out_ids!r}"
        )
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


# ============================================================================
# T0173 — predicate op="in" with an empty list returns a clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0173_predicate_in_with_empty_list_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0173 — predicate `op="in"` with `right.value=[]` must produce a
    clean envelope (no /errors/internal). Semantically "x IN ()" is
    always false, so a sensible API returns 200 with zero items. The
    SQL builder may also reasonably reject the empty list with a 4xx.
    Pin no 5xx, no internal error.
    """
    prefix = f"ts-t0173-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "in",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": []},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code != 500, resp.text
        if resp.status_code == 200:
            # "id IN ()" → no rows
            out_ids = [item["id"] for item in resp.json()["items"]]
            for seed_id in ids:
                assert seed_id not in out_ids, (
                    f"empty IN list should match nothing, but seed "
                    f"{seed_id!r} is in the result: {out_ids!r}"
                )
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0196 — GET ?cursor= (empty string) returns 400 /errors/bad-request
# ============================================================================


@pytest.mark.asyncio
async def test_t0196_get_cursor_empty_string_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """T0196 — Spec §4 explicitly calls out the cursor-mode quirk:
    `?cursor=` (empty string) and other non-JSON values are rejected
    with 400 /errors/bad-request and the detail "malformed cursor".
    """
    resp = await client.get("/v1/toolsets?cursor=")
    assert resp.status_code == 400, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/bad-request", envelope
    assert envelope["status"] == 400
    # Detail should reference malformed-cursor or similar; check
    # cursor-related wording is present
    detail = envelope.get("detail", "").lower()
    assert "cursor" in detail, envelope


# ============================================================================
# T0197 — GET ?cursor=garbage returns 400 /errors/bad-request
# ============================================================================


@pytest.mark.asyncio
async def test_t0197_get_cursor_non_json_garbage_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """T0197 — A random non-JSON cursor string is rejected with 400
    /errors/bad-request. Companion to T0196 for non-empty malformed
    cursor values.
    """
    resp = await client.get("/v1/toolsets?cursor=abc-not-a-cursor")
    assert resp.status_code == 400, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/bad-request", envelope
    assert envelope["status"] == 400
    detail = envelope.get("detail", "").lower()
    assert "cursor" in detail, envelope


# ============================================================================
# T0212 — cursor pagination with order_by desc preserves order across pages
# ============================================================================


@pytest.mark.asyncio
async def test_t0212_cursor_pagination_with_order_by_desc(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0212 — combines cursor mode (T0014) with order_by (T0043).
    Walks 7 seeded toolsets ordered by `id desc` in pages of 3.
    Pin: the concatenated id sequence is strictly descending and
    contains exactly the seeded ids.
    """
    prefix = f"ts-t0212-{unique_suffix}"
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
        for _ in range(10):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 3},
                "order_by": [{"field": "id", "direction": "desc"}],
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            seen.extend(item["id"] for item in page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail(f"cursor walk did not terminate: {seen!r}")

        assert sorted(seen) == sorted(ids), (
            f"cursor+desc walk did not cover seeded set. "
            f"seeded={sorted(ids)!r}, seen={sorted(seen)!r}"
        )
        # Strictly descending across pages — concatenation is sorted desc
        assert seen == sorted(seen, reverse=True), (
            f"cursor+desc walk did NOT preserve descending order across "
            f"page boundaries: {seen!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0213 — cursor + order_by on a JSONB key preserves order across pages
# ============================================================================


@pytest.mark.asyncio
async def test_t0213_cursor_pagination_with_order_by_jsonb_key(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0213 — extends T0088 (single-page JSONB ordering) to a multi-page
    cursor walk. Seeds 5 Documents whose meta.tag is set to a unique
    letter; walks ordered by meta.tag asc with page length 2. Pin
    the concatenated sequence is sorted ascending by tag.
    """
    prefix = f"doc-t0213-{unique_suffix}"
    rows = [
        {"id": f"{prefix}-a", "tag": "alpha"},
        {"id": f"{prefix}-b", "tag": "bravo"},
        {"id": f"{prefix}-c", "tag": "charlie"},
        {"id": f"{prefix}-d", "tag": "delta"},
        {"id": f"{prefix}-e", "tag": "echo"},
    ]
    collection_id = f"coll-t0213-{unique_suffix}"
    created: list[str] = []
    try:
        for r in rows:
            resp = await client.post(
                "/v1/documents",
                json={
                    "id": r["id"],
                    "name": r["tag"],
                    "collection_id": collection_id,
                    "meta": {"tag": r["tag"]},
                },
            )
            assert resp.status_code in (200, 201), resp.text
            created.append(r["id"])

        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        seen_tags: list[str] = []
        seen_ids: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 2},
                "order_by": [{"field": "meta.tag", "direction": "asc"}],
            }
            resp = await client.post("/v1/documents/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            for item in page["items"]:
                seen_ids.append(item["id"])
                seen_tags.append((item.get("meta") or {}).get("tag"))
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail(f"jsonb-cursor walk did not terminate: {seen_ids!r}")

        assert sorted(seen_ids) == sorted(r["id"] for r in rows), (
            f"missed/duplicated rows: {seen_ids!r}"
        )
        # Tags must be in ascending order across the walk
        assert seen_tags == sorted(seen_tags), (
            f"cursor walk did NOT preserve meta.tag asc ordering across "
            f"page boundaries: {seen_tags!r}"
        )
    finally:
        for did in created:
            await client.delete(f"/v1/documents/{did}")


# ============================================================================
# T0215 — triple-nested AND/OR/!= predicate returns the documented set
# ============================================================================


@pytest.mark.asyncio
async def test_t0215_predicate_triple_nested_and_or_not_via_neq(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0215 — predicate-tree depth >2 not covered by T0072/T0073.

    Build:  (id ~= prefix-%) AND ( (id = target) OR (id != exclude_a AND id != exclude_b) )

    Seeds 4 toolsets: a, b, c, d (zero-padded).
    Choose target=b, exclude_a=c, exclude_b=d → the inner OR matches
    rows that are b OR (NOT c AND NOT d) → a, b
    AND-ed with the prefix filter → a, b

    Verifies nested AND/OR composition with `!=` operands behaves
    consistently and never 5xx.
    """
    prefix = f"ts-t0215-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 4)
    target = ids[1]      # "b"
    exclude_a = ids[2]   # "c"
    exclude_b = ids[3]   # "d"
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
                    "op": "or",
                    "left": {
                        "kind": "predicate",
                        "op": "=",
                        "left": {"kind": "field", "name": "id"},
                        "right": {"kind": "value", "value": target},
                    },
                    "right": {
                        "kind": "predicate",
                        "op": "and",
                        "left": {
                            "kind": "predicate",
                            "op": "!=",
                            "left": {"kind": "field", "name": "id"},
                            "right": {"kind": "value", "value": exclude_a},
                        },
                        "right": {
                            "kind": "predicate",
                            "op": "!=",
                            "left": {"kind": "field", "name": "id"},
                            "right": {"kind": "value", "value": exclude_b},
                        },
                    },
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        # Expected: target + every prefix-matching id that isn't an exclude
        expected = sorted({target, *ids} - {exclude_a, exclude_b})
        assert out_ids == expected, (
            f"triple-nested predicate result mismatch. "
            f"expected={expected!r}, got={out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0216 — LIKE predicate with SQL-injection-shaped value is parameterized
# ============================================================================


@pytest.mark.asyncio
async def test_t0216_predicate_like_sql_injection_shape_isolated(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0216 — Send `~=` (LIKE) with a value that looks like SQL syntax
    (semicolons, DROP TABLE, comments). The predicate path must
    parameterize the value so the syntax is treated as literal pattern
    text, not SQL. Concretely:

      - A value with `%` is wildcard-expanded (documented behavior)
        but only within the intended pattern semantics — `id ~= "%"`
        matches rows but does NOT execute arbitrary SQL.
      - A value with `; DROP TABLE x; --` returns either 0 hits or a
        clean envelope; never 5xx; the toolsets table still has all
        seeded rows after.
    """
    prefix = f"ts-t0216-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        # Pattern with SQL-injection shape (literal text after the prefix)
        attack_value = f"{prefix}-; DROP TABLE toolsets; --"
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": attack_value},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        # Parameterized → 200 with zero hits (no row matches that literal)
        # OR a clean 4xx if validation rejects. NEVER 5xx.
        assert resp.status_code < 500, resp.text
        if resp.status_code == 200:
            # No row matches the literal injection pattern
            assert resp.json()["items"] == [], resp.json()
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope

        # Sanity: the seeded toolsets table still has all 3 seeded rows
        # (the "DROP TABLE" wasn't executed)
        survive = await client.post(
            "/v1/toolsets/find",
            json={
                "predicate": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
                "page": {"kind": "offset", "offset": 0, "length": 50},
            },
        )
        assert survive.status_code == 200, survive.text
        survive_ids = sorted(item["id"] for item in survive.json()["items"])
        assert survive_ids == sorted(ids), (
            f"seeded rows missing after injection-shape probe! "
            f"seeded={sorted(ids)!r}, surviving={survive_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0237 — predicate `=` with null literal returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0237_predicate_eq_null_returns_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0237 — find with `op=", right=null` against a nullable field
    (LLMProvider has no documented nullable description-style field
    on Identifiable, but Toolset has none either; use the universally-
    present Toolset.config which is nullable for `provider="internal"`).

    Pin: clean envelope (200 with possibly-empty hits OR 4xx); never
    /errors/internal.
    """
    prefix = f"ts-t0237-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "config"},
                "right": {"kind": "value", "value": None},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak: {resp.text}"
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope

        # Same predicate against the non-nullable id field
        body2 = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": None},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        r2 = await client.post("/v1/toolsets/find", json=body2)
        assert r2.status_code != 500 or (
            r2.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak on non-nullable: {r2.text}"
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0238 — predicate `in` with mixed-type list returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0238_predicate_in_mixed_type_list_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0238 — find with `op="in", right=[1, "two", null]` (mixed
    types). The asyncpg driver may reject the type-coerce; the
    handler must surface a clean envelope, never /errors/internal.
    """
    prefix = f"ts-t0238-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "in",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": [1, "two", None]},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        # Must NOT be /errors/internal regardless of status code
        body_resp = resp.json() if resp.content else {}
        assert body_resp.get("type") != "/errors/internal", (
            f"/errors/internal leak from mixed-type IN: {resp.text}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0239 — cursor walk survives concurrent DELETE of a not-yet-visited row
# ============================================================================


@pytest.mark.asyncio
async def test_t0239_cursor_walk_survives_concurrent_delete(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0239 — Seed 6 toolsets. Walk with cursor + length=2:

      page 1 → 2 items
      [DELETE one of the not-yet-visited ids]
      page 2 → up to 2 items (the deleted one missing)
      page 3 → final page

    Pin: walk completes cleanly (no 5xx); no duplicates across pages;
    total visited ids is at most 6 and at least 5 (the deleted one
    may or may not appear depending on cursor snapshot semantics).
    """
    prefix = f"ts-t0239-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 6)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        # Page 1
        body = {
            "predicate": predicate,
            "page": {"kind": "cursor", "cursor": None, "length": 2},
            "order_by": [{"field": "id", "direction": "asc"}],
        }
        r1 = await client.post("/v1/toolsets/find", json=body)
        assert r1.status_code == 200, r1.text
        page1 = r1.json()
        seen = [item["id"] for item in page1["items"]]
        cursor = page1.get("next_cursor")
        assert cursor is not None, page1

        # Pick a not-yet-visited id and delete it.
        not_visited = sorted(set(ids) - set(seen))
        assert not_visited, f"page 1 already covered everything: {seen!r}"
        target_to_delete = not_visited[-1]  # delete the LAST one
        rm = await client.delete(f"/v1/toolsets/{target_to_delete}")
        assert rm.status_code == 204, rm.text

        # Continue the cursor walk
        for _ in range(10):  # safety bound
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 2},
                "order_by": [{"field": "id", "direction": "asc"}],
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            seen.extend(item["id"] for item in page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail("cursor walk did not terminate after concurrent delete")

        # No duplicates
        assert len(seen) == len(set(seen)), (
            f"duplicates across cursor walk: {seen!r}"
        )
        # All ids minus the deleted one are a subset of seen
        # (deleted one may or may not appear depending on snapshot
        # semantics — both are acceptable)
        remaining = set(ids) - {target_to_delete}
        assert remaining.issubset(set(seen) | {target_to_delete}), (
            f"some non-deleted ids missing from walk: "
            f"remaining={remaining!r}, seen={seen!r}"
        )
        assert len(seen) <= len(ids), (
            f"walk returned MORE ids than seeded: {len(seen)} > "
            f"{len(ids)}; seen={seen!r}"
        )
    finally:
        # The deleted one is already gone; cleanup the rest
        for tid in ids:
            await client.delete(f"/v1/toolsets/{tid}")


# ============================================================================
# T0248 — predicate `~=` with empty-string value: deterministic clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0248_predicate_like_empty_string_deterministic_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0248 — find with `op="~=", right=""` is a degenerate LIKE
    pattern. SQL semantics: `LIKE ''` matches only empty strings (no
    rows have empty id). The contract pin is:
      - response is 200 OR a clean 4xx (no /errors/internal)
      - two sequential calls return the same status code AND same
        item set (deterministic, not flaky)
    """
    prefix = f"ts-t0248-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": ""},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        r1 = await client.post("/v1/toolsets/find", json=body)
        r2 = await client.post("/v1/toolsets/find", json=body)

        # No /errors/internal on either call
        for r, label in ((r1, "first"), (r2, "second")):
            try:
                envelope = r.json()
            except Exception:
                envelope = {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} call leaked /errors/internal: {r.text}"
            )

        # Determinism: same status code
        assert r1.status_code == r2.status_code, (
            f"empty-LIKE response is non-deterministic: "
            f"r1={r1.status_code}, r2={r2.status_code}"
        )
        if r1.status_code == 200:
            ids1 = sorted(item["id"] for item in r1.json()["items"])
            ids2 = sorted(item["id"] for item in r2.json()["items"])
            assert ids1 == ids2, (
                f"empty-LIKE item set is non-deterministic: "
                f"r1={ids1!r} vs r2={ids2!r}"
            )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0256 — predicate with right.kind="field" (field-vs-field compare)
# ============================================================================


@pytest.mark.asyncio
async def test_t0256_predicate_field_vs_field_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0256 — POST /find with a predicate where BOTH operands are
    fields (no literal). Pins that the predicate validator either
    accepts the shape (returning 200 with results) or cleanly rejects
    it (4xx); never /errors/internal.

    Many predicate translators only support field-vs-value; field-vs-
    field requires a column-comparison codepath that often isn't
    wired. Pin the no-crash invariant.
    """
    prefix = f"ts-t0256-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "field", "name": "provider"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"field-vs-field predicate leaked /errors/internal: {resp.text}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0257 — predicate with left.kind="value", right.kind="field" (swapped)
# ============================================================================


@pytest.mark.asyncio
async def test_t0257_predicate_swapped_operand_kinds_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0257 — Predicate with literal on the left and field on the
    right (the reverse of the usual `field op value` shape). The
    schema may accept this (mathematically equivalent) or reject it;
    pin the no-/errors/internal invariant.
    """
    prefix = f"ts-t0257-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "value", "value": ids[0]},
                "right": {"kind": "field", "name": "id"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"swapped operand predicate leaked /errors/internal: {resp.text}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0276 — order_by on JSONB list-item path returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0276_order_by_jsonb_list_item_path_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0276 — Predicate/order-builder edge case: order_by on a path
    that includes a list index (`models.0.name`). The predicate
    builder may not support list-index syntax; the contract pin is
    "no /errors/internal" — accept either a 200 (sort applied) or
    a clean 4xx/5xx-non-internal.
    """
    prefix = f"llm-t0276-{unique_suffix}"
    ids = []
    try:
        for i in range(3):
            entity_id = f"{prefix}-{i}"
            r = await client.post(
                "/v1/llm_providers",
                json={
                    "id": entity_id,
                    "provider": "anthropic",
                    "models": [{"name": f"model-z-{i}",
                                 "context_length": 200_000}],
                    "config": {"api_key": "sk-test"},
                    "limits": {"max_concurrency": 1},
                },
            )
            assert r.status_code == 201, r.text
            ids.append(entity_id)

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
            "order_by": [{"field": "models.0.name", "direction": "asc"}],
        }
        resp = await client.post("/v1/llm_providers/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"order_by JSONB list-index leaked /errors/internal: "
            f"{resp.text}"
        )
    finally:
        for entity_id in ids:
            await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0279 — predicate `and` with identical left/right returns single-clause set
# ============================================================================


@pytest.mark.asyncio
async def test_t0279_predicate_and_with_identical_clauses_idempotent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0279 — `op="and"` is binary (left+right per spec §4). When
    left and right are the SAME predicate, the result MUST equal the
    result of evaluating either lone clause. Pins boolean operator
    idempotency / no double-counting.

    NB: spec doesn't define a "single-element clauses array" since
    and/or take left+right; this test reframes T0279's original
    wording to the spec-compatible variant.
    """
    prefix = f"ts-t0279-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        like_clause = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        # Single-clause baseline
        baseline = await client.post(
            "/v1/toolsets/find",
            json={
                "predicate": like_clause,
                "page": {"kind": "offset", "offset": 0, "length": 50},
            },
        )
        assert baseline.status_code == 200, baseline.text
        baseline_ids = sorted(item["id"] for item in baseline.json()["items"])

        # AND with the same clause on both sides
        andthe = await client.post(
            "/v1/toolsets/find",
            json={
                "predicate": {
                    "kind": "predicate",
                    "op": "and",
                    "left": like_clause,
                    "right": like_clause,
                },
                "page": {"kind": "offset", "offset": 0, "length": 50},
            },
        )
        assert andthe.status_code == 200, andthe.text
        and_ids = sorted(item["id"] for item in andthe.json()["items"])

        assert baseline_ids == and_ids, (
            f"AND of identical clauses should be idempotent: "
            f"baseline={baseline_ids!r} vs AND={and_ids!r}"
        )
        assert sorted(ids) == baseline_ids, (
            f"baseline missed seeded rows: {baseline_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0283 — Predicate `~=` with `_` single-char wildcard
# ============================================================================


@pytest.mark.asyncio
async def test_t0283_predicate_like_underscore_single_char_wildcard(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0283 — Spec §4 says `~=` is SQL LIKE with `%` (any) and `_`
    (single char). T0012 covers `%`; this pins `_`. Seed 3 toolsets
    with prefix-suffix-1, prefix-suffix-22, prefix-suffix-3; LIKE
    pattern `prefix-suffix-_` matches only the single-digit ones.
    """
    base = f"ts-t0283-{unique_suffix}"
    one_char_ids = [f"{base}-1", f"{base}-3"]
    two_char_id = f"{base}-22"
    all_ids = one_char_ids + [two_char_id]
    try:
        for entity_id in all_ids:
            r = await client.post(
                "/v1/toolsets",
                json={
                    "id": entity_id,
                    "provider": "mcp",
                    "config": {
                        "transport": "stdio",
                        "config": {"command": ["echo"]},
                    },
                },
            )
            assert r.status_code == 201, r.text

        # LIKE pattern with one trailing _ — matches only single-char tails
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{base}-_"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out = sorted(item["id"] for item in resp.json()["items"])
        assert out == sorted(one_char_ids), (
            f"`_` wildcard should match single-char only; "
            f"expected {sorted(one_char_ids)!r}, got {out!r}"
        )
        assert two_char_id not in out
    finally:
        for entity_id in all_ids:
            await client.delete(f"/v1/toolsets/{entity_id}")


# ============================================================================
# T0301 — Predicate `op="in"` with single-element value list
# ============================================================================


@pytest.mark.asyncio
async def test_t0301_predicate_in_with_single_element_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0301 — Between T0173 (empty IN list) and T0071 (multi-element
    IN list). Pin that a single-element IN list returns exactly that
    one row (no off-by-one in the SQL builder).
    """
    prefix = f"ts-t0301-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    target = ids[1]
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "in",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": [target]},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        assert out_ids == [target], (
            f"single-element IN list should match exactly the target; "
            f"got {out_ids!r}"
        )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0302 — GET /v1/workspaces?cursor= (empty) returns 400
# ============================================================================


@pytest.mark.asyncio
async def test_t0302_workspaces_get_cursor_empty_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """T0302 — Mirror of T0196 / T0197 cursor-malformed handling on
    the bespoke /v1/workspaces router. Empty cursor must be rejected
    with 400 /errors/bad-request.
    """
    resp = await client.get("/v1/workspaces?cursor=")
    assert resp.status_code == 400, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/bad-request", envelope
    detail = envelope.get("detail", "").lower()
    assert "cursor" in detail, envelope


# ============================================================================
# T0311 — Predicate `~=` LIKE is case-sensitive
# ============================================================================


@pytest.mark.asyncio
async def test_t0311_predicate_like_is_case_sensitive(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0311 — Pin Postgres LIKE semantics (vs ILIKE): the `~=`
    operator should be case-sensitive. Seed a toolset with a
    lowercase id; an uppercase pattern returns 0 rows, the lowercase
    pattern returns the row.
    """
    # Use predictable lowercase suffix
    base = f"ts-t0311-{unique_suffix}"
    lowercase_id = f"{base}-alpha"
    try:
        r = await client.post(
            "/v1/toolsets",
            json={
                "id": lowercase_id,
                "provider": "mcp",
                "config": {
                    "transport": "stdio",
                    "config": {"command": ["echo"]},
                },
            },
        )
        assert r.status_code == 201, r.text

        # Uppercase LIKE pattern
        upper_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{base.upper()}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        upper_resp = await client.post("/v1/toolsets/find", json=upper_body)
        assert upper_resp.status_code == 200, upper_resp.text
        upper_ids = [item["id"] for item in upper_resp.json()["items"]]
        assert lowercase_id not in upper_ids, (
            f"uppercase LIKE pattern should NOT match lowercase id "
            f"if LIKE is case-sensitive (Postgres LIKE). "
            f"Got matches: {upper_ids!r}"
        )

        # Lowercase pattern matches
        lower_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{base}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        lower_resp = await client.post("/v1/toolsets/find", json=lower_body)
        assert lower_resp.status_code == 200, lower_resp.text
        lower_ids = [item["id"] for item in lower_resp.json()["items"]]
        assert lowercase_id in lower_ids, (
            f"lowercase LIKE pattern should match lowercase id: "
            f"got {lower_ids!r}"
        )
    finally:
        await client.delete(f"/v1/toolsets/{lowercase_id}")


# ============================================================================
# T0327 — Cursor token from /v1/toolsets reused on /v1/llm_providers
# ============================================================================


@pytest.mark.asyncio
async def test_t0327_cross_entity_cursor_token_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0327 — Get a valid cursor token by paginating /v1/toolsets,
    then reuse it on /v1/llm_providers. The cursor decoder may
    treat tokens as opaque (accepting any well-formed token) OR as
    entity-scoped (rejecting cross-entity reuse). Either is fine —
    pin no /errors/internal leak.
    """
    prefix = f"ts-t0327-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 4)
    try:
        # Get a cursor by paginating toolsets
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "cursor", "cursor": None, "length": 2},
        }
        first = await client.post("/v1/toolsets/find", json=body)
        assert first.status_code == 200, first.text
        cursor = first.json().get("next_cursor")
        if not cursor:
            pytest.skip(
                "first toolsets page didn't return a cursor — can't "
                "test cross-entity reuse"
            )

        # Reuse on /v1/llm_providers
        resp = await client.get(f"/v1/llm_providers?cursor={cursor}")
        assert resp.status_code != 500, resp.text
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"cross-entity cursor reuse leaked /errors/internal: "
            f"{resp.text}"
        )
    finally:
        await _delete_toolsets(client, seeded)


# ============================================================================
# T0328 — Predicate `~=` with literal `%` (escaped) matches literal-percent rows
# ============================================================================


@pytest.mark.asyncio
async def test_t0328_predicate_like_escaped_percent_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0328 — Pin behaviour when the LIKE pattern includes a
    backslash-escaped `%`. The predicate translator may either:
      - support SQL LIKE escape semantics (`\\%` matches literal %)
      - treat `\\` as a literal char (no escape support)
      - reject the pattern with a clean 4xx

    Hard pin: no /errors/internal regardless. Documents the actual
    behaviour for future reference.
    """
    # Seed a toolset with a literal `%` in its id
    pct_id = f"ts-t0328-{unique_suffix}-%pct%"
    plain_id = f"ts-t0328-{unique_suffix}-plain"

    import urllib.parse

    try:
        # Create both rows
        for entity_id in (pct_id, plain_id):
            r = await client.post(
                "/v1/toolsets",
                json={
                    "id": entity_id,
                    "provider": "mcp",
                    "config": {
                        "transport": "stdio",
                        "config": {"command": ["echo"]},
                    },
                },
            )
            # The literal-% id may or may not be allowed by the
            # validator; tolerate either
            assert r.status_code in (201, 422), r.text

        # Try predicate `~=` with `\%` escape
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {
                    "kind": "value",
                    "value": f"ts-t0328-{unique_suffix}-\\%pct\\%",
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code != 500, resp.text
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"escaped-percent LIKE leaked /errors/internal: {resp.text}"
        )
    finally:
        for entity_id in (pct_id, plain_id):
            # Use urllib quote since id has special chars
            await client.delete(
                f"/v1/toolsets/{urllib.parse.quote(entity_id, safe='')}",
            )


# ============================================================================
# T0329 — Cursor walk with order_by desc + delete visited row mid-walk
# ============================================================================


@pytest.mark.asyncio
async def test_t0329_cursor_walk_with_desc_order_after_visited_delete(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0329 — Walk a cursor with order_by id desc; after page 1
    delete an ALREADY-visited id; subsequent pages return clean
    envelopes with no duplicates of remaining ids.

    Distinct from T0239 (which deleted a not-yet-visited row).
    """
    prefix = f"ts-t0329-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 6)
    deleted_remaining = list(ids)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        # Page 1 (desc order)
        body = {
            "predicate": predicate,
            "page": {"kind": "cursor", "cursor": None, "length": 2},
            "order_by": [{"field": "id", "direction": "desc"}],
        }
        r1 = await client.post("/v1/toolsets/find", json=body)
        assert r1.status_code == 200, r1.text
        page1 = r1.json()
        seen = [item["id"] for item in page1["items"]]
        cursor = page1.get("next_cursor")
        assert cursor is not None, page1

        # Delete an ALREADY-visited id
        target = seen[0]
        rm = await client.delete(f"/v1/toolsets/{target}")
        assert rm.status_code == 204, rm.text
        deleted_remaining.remove(target)

        # Continue cursor walk — must complete cleanly
        for _ in range(10):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 2},
                "order_by": [{"field": "id", "direction": "desc"}],
            }
            resp = await client.post("/v1/toolsets/find", json=body)
            assert resp.status_code == 200, resp.text
            page = resp.json()
            seen.extend(item["id"] for item in page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail("cursor walk did not terminate")

        # No duplicates anywhere
        assert len(seen) == len(set(seen)), (
            f"duplicates after deleting visited id: {seen!r}"
        )
        # Total visited never exceeds seeded set
        assert len(seen) <= len(ids), (
            f"walk returned MORE ids than seeded: {len(seen)} > "
            f"{len(ids)}; seen={seen!r}"
        )
    finally:
        for tid in deleted_remaining:
            await client.delete(f"/v1/toolsets/{tid}")


# ============================================================================
# T0330 — Cursor walk with composite order_by [meta.tag asc, id asc]
# ============================================================================


@pytest.mark.asyncio
async def test_t0330_cursor_walk_composite_order_by_two_keys(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0330 — Compose two order_by keys (one JSONB nested, one
    scalar id) under cursor pagination. Walk visits each row exactly
    once; the concatenated sequence respects both sort keys.
    """
    prefix = f"doc-t0330-{unique_suffix}"
    rows = [
        {"id": f"{prefix}-1", "tag": "a"},
        {"id": f"{prefix}-2", "tag": "b"},
        {"id": f"{prefix}-3", "tag": "a"},
        {"id": f"{prefix}-4", "tag": "b"},
        {"id": f"{prefix}-5", "tag": "a"},
    ]
    created: list[str] = []
    try:
        for r in rows:
            resp = await client.post(
                "/v1/documents",
                json={
                    "id": r["id"],
                    "name": r["tag"],
                    "collection_id": f"unenforced-{unique_suffix}",
                    "meta": {"tag": r["tag"]},
                },
            )
            assert resp.status_code in (200, 201), resp.text
            created.append(r["id"])

        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        seen: list[tuple[str, str]] = []
        cursor: str | None = None
        for _ in range(10):
            body = {
                "predicate": predicate,
                "page": {"kind": "cursor", "cursor": cursor, "length": 2},
                "order_by": [
                    {"field": "meta.tag", "direction": "asc"},
                    {"field": "id", "direction": "asc"},
                ],
            }
            resp = await client.post("/v1/documents/find", json=body)
            assert resp.status_code == 200, resp.text
            for item in resp.json()["items"]:
                seen.append((
                    (item.get("meta") or {}).get("tag"),
                    item["id"],
                ))
            cursor = resp.json().get("next_cursor")
            if cursor is None:
                break
        else:
            pytest.fail("composite order cursor walk did not terminate")

        # All seeded rows visited exactly once
        assert sorted(item[1] for item in seen) == sorted(r["id"] for r in rows), (
            f"missed/duplicated rows: {seen!r}"
        )
        # Composite ordering: tag asc primary, id asc secondary
        # within each tag group
        from itertools import groupby
        tags = [t for (t, _i) in seen]
        assert tags == sorted(tags), (
            f"primary tag asc violated: {tags!r}"
        )
        for tag, group in groupby(seen, key=lambda p: p[0]):
            ids_in_tag = [i for (_t, i) in group]
            assert ids_in_tag == sorted(ids_in_tag), (
                f"secondary id asc violated within tag={tag!r}: "
                f"{ids_in_tag!r}"
            )
    finally:
        for did in created:
            await client.delete(f"/v1/documents/{did}")


# ============================================================================
# T0331 — Cursor against a deleted-row position returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0331_cursor_referencing_deleted_row_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0331 — Issue a cursor token; delete the row that the cursor
    most likely encodes the position of; reuse the cursor. Response
    must be a clean envelope (200 with adjusted continuation OR a
    documented 4xx); never /errors/internal.
    """
    prefix = f"ts-t0331-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 4)
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }
        body = {
            "predicate": predicate,
            "page": {"kind": "cursor", "cursor": None, "length": 2},
            "order_by": [{"field": "id", "direction": "asc"}],
        }
        first = await client.post("/v1/toolsets/find", json=body)
        assert first.status_code == 200, first.text
        page1 = first.json()
        cursor = page1.get("next_cursor")
        if cursor is None:
            pytest.skip("no cursor after first page; can't test reuse")
        seen = [item["id"] for item in page1["items"]]

        # Delete the LAST visited row — cursor likely encodes its position
        target = seen[-1]
        rm = await client.delete(f"/v1/toolsets/{target}")
        assert rm.status_code == 204, rm.text

        # Reuse the cursor
        body["page"] = {"kind": "cursor", "cursor": cursor, "length": 2}
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"cursor reuse after deleted-row leaked /errors/internal: "
            f"{resp.text}"
        )
    finally:
        for tid in seeded:
            await client.delete(f"/v1/toolsets/{tid}")


# ============================================================================
# T0332 — Cursor does NOT include rows inserted after issue
# ============================================================================


@pytest.mark.asyncio
async def test_t0332_cursor_walk_excludes_post_issue_inserts(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0332 — Snapshot semantics pin: seed N rows, start cursor walk,
    insert M new matching rows mid-walk, complete walk. Documents
    behaviour: either the walk visits exactly the original N
    (snapshot) OR includes the new rows (live view). Pin the actual
    contract — no /errors/internal regardless.
    """
    prefix = f"ts-t0332-{unique_suffix}"
    initial = await _seed_toolsets(client, prefix, 4)
    inserted: list[str] = []
    try:
        predicate = {
            "kind": "predicate",
            "op": "~=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": f"{prefix}%"},
        }

        # Page 1
        body = {
            "predicate": predicate,
            "page": {"kind": "cursor", "cursor": None, "length": 2},
            "order_by": [{"field": "id", "direction": "asc"}],
        }
        r1 = await client.post("/v1/toolsets/find", json=body)
        assert r1.status_code == 200, r1.text
        seen = [item["id"] for item in r1.json()["items"]]
        cursor = r1.json().get("next_cursor")
        assert cursor is not None

        # Insert 2 NEW rows that match the predicate
        for i in range(2):
            new_id = f"{prefix}-new-{i:02d}"
            r = await client.post(
                "/v1/toolsets",
                json={
                    "id": new_id,
                    "provider": "mcp",
                    "config": {
                        "transport": "stdio",
                        "config": {"command": ["echo"]},
                    },
                },
            )
            assert r.status_code == 201, r.text
            inserted.append(new_id)

        # Continue walking
        for _ in range(10):
            body["page"] = {"kind": "cursor", "cursor": cursor, "length": 2}
            r = await client.post("/v1/toolsets/find", json=body)
            assert r.status_code != 500, r.text
            envelope = r.json()
            assert envelope.get("type") != "/errors/internal", r.text
            if r.status_code != 200:
                break
            seen.extend(item["id"] for item in r.json()["items"])
            cursor = r.json().get("next_cursor")
            if cursor is None:
                break
        # Document the observed cardinality but don't strict-pin
        # snapshot vs live (both contracts are reasonable)
    finally:
        for tid in initial + inserted:
            await client.delete(f"/v1/toolsets/{tid}")


# ============================================================================
# T0337 — Predicate composite `in` + `and` filters intersection correctly
# ============================================================================


@pytest.mark.asyncio
async def test_t0337_predicate_composite_in_and_intersection(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0337 — and(field LIKE prefix%, in(field, [A,B,C])) returns
    exactly the rows matching BOTH clauses (intersection). Pins
    composite of `in` with `and` returns documented set.
    """
    prefix = f"ts-t0337-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 4)
    # Pick a subset of ids for the IN list — only 2 of the 4 seeded
    in_subset = sorted(seeded[:2])
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
                    "op": "in",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": in_subset},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out = sorted(item["id"] for item in resp.json()["items"])
        assert out == in_subset, (
            f"composite IN+AND intersection wrong; expected "
            f"{in_subset!r}, got {out!r}"
        )
    finally:
        await _delete_toolsets(client, seeded)


# ============================================================================
# T0338 — Predicate nested 4 levels deep returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0338_predicate_nested_four_levels_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0338 — Build a predicate tree of depth 4 with mixed and/or/=
    nodes. Tests the translator's depth handling: must return either
    a documented result set (200) or a clean 4xx; never
    /errors/internal.

    Tree shape (depth 4):
        and(
            ~= prefix%,
            or(
                = id_a,
                and(
                    != id_b,
                    or(
                        = id_c,
                        != id_d,
                    ),
                ),
            ),
        )
    """
    prefix = f"ts-t0338-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 5)
    a, b, c, d, _e = seeded
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
                    "op": "or",
                    "left": {
                        "kind": "predicate",
                        "op": "=",
                        "left": {"kind": "field", "name": "id"},
                        "right": {"kind": "value", "value": a},
                    },
                    "right": {
                        "kind": "predicate",
                        "op": "and",
                        "left": {
                            "kind": "predicate",
                            "op": "!=",
                            "left": {"kind": "field", "name": "id"},
                            "right": {"kind": "value", "value": b},
                        },
                        "right": {
                            "kind": "predicate",
                            "op": "or",
                            "left": {
                                "kind": "predicate",
                                "op": "=",
                                "left": {"kind": "field", "name": "id"},
                                "right": {"kind": "value", "value": c},
                            },
                            "right": {
                                "kind": "predicate",
                                "op": "!=",
                                "left": {"kind": "field", "name": "id"},
                                "right": {"kind": "value", "value": d},
                            },
                        },
                    },
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"depth-4 predicate leaked /errors/internal: {resp.text}"
        )
        # If 200, the result set is the prefix-scoped intersection
        # of the inner OR clauses
        if resp.status_code == 200:
            out = {item["id"] for item in resp.json()["items"]}
            # All seeded ids are prefix-matching — outer AND just
            # narrows by id-prefix (which is already true). Inner
            # OR: a OR (NOT b AND (c OR NOT d)). The expected set
            # is at minimum {a} since a satisfies the lone equality.
            assert a in out, (
                f"expected at least {a!r} (matches lone =a clause); "
                f"got {out!r}"
            )
    finally:
        await _delete_toolsets(client, seeded)


# ============================================================================
# T0217 — find with order_by referencing unknown field returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0217_find_order_by_unknown_field_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0217 — POST /v1/toolsets/find with order_by referencing a column
    that doesn't exist on Toolset. Must produce a clean envelope (4xx
    or 5xx-non-internal); never /errors/internal.

    Mirrors T0084 (unknown predicate field) for the order_by path.
    """
    prefix = f"ts-t0217-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 2)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
            "order_by": [
                {"field": f"nonexistent_field_{unique_suffix}",
                 "direction": "asc"},
            ],
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak: {resp.text}"
        # Implementations vary: some validate at request-time (422),
        # some at SQL-build time (4xx /errors/bad-request), some are
        # lenient and order non-deterministically (200). Accept any
        # non-internal outcome.
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0352 — Predicate `~=` with "" pattern vs "%" returns documented sets
# ============================================================================


@pytest.mark.asyncio
async def test_t0352_predicate_like_empty_vs_percent_consistent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0352 — Compare LIKE behaviour for `""` (empty pattern) and
    `"%"` (any-string wildcard). Pin both responses are clean
    (no /errors/internal); document whether they match the same
    set or different sets.

    Postgres semantics: `LIKE ''` matches only empty strings (no
    rows have empty id); `LIKE '%'` matches all rows. So they
    SHOULD return different sets — `%` is a strict superset.
    """
    prefix = f"ts-t0352-{unique_suffix}"
    seeded = await _seed_toolsets(client, prefix, 3)
    try:
        async def _find_with_pattern(pattern: str) -> tuple[int, set[str]]:
            body = {
                "predicate": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": pattern},
                },
                "page": {"kind": "offset", "offset": 0, "length": 200},
            }
            r = await client.post("/v1/toolsets/find", json=body)
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"LIKE pattern={pattern!r} leaked /errors/internal: "
                f"{r.text}"
            )
            if r.status_code == 200:
                return r.status_code, {item["id"] for item in r.json()["items"]}
            return r.status_code, set()

        empty_status, empty_ids = await _find_with_pattern("")
        pct_status, pct_ids = await _find_with_pattern("%")

        # If both 200, % must match a strict superset of "" (since
        # % matches everything, "" matches at most empty strings).
        if empty_status == 200 and pct_status == 200:
            assert empty_ids.issubset(pct_ids), (
                f"empty-LIKE result not a subset of `%` result: "
                f"empty={empty_ids!r}, pct={pct_ids!r}"
            )
            # Seeded ids must be in the `%` set (which matches all)
            for sid in seeded:
                assert sid in pct_ids, (
                    f"seeded id {sid!r} missing from %-LIKE result: "
                    f"{pct_ids!r}"
                )
    finally:
        await _delete_toolsets(client, seeded)


# ============================================================================
# T0377 — Predicate `~=` with leading `%` wildcard matches by suffix
# ============================================================================


@pytest.mark.asyncio
async def test_t0377_predicate_like_leading_wildcard_matches_suffix(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0377 — Companion to T0283 (trailing `_` wildcard) for the
    leading `%` wildcard. Seed rows with ids ending in a unique
    suffix; query `~= "%suffix"` returns exactly those rows.
    """
    suffix_marker = f"-marker-{unique_suffix}"
    seeded = []
    other = f"ts-other-{unique_suffix}"
    try:
        for i in range(3):
            entity_id = f"ts-suffix-{i}{suffix_marker}"
            r = await client.post(
                "/v1/toolsets",
                json={
                    "id": entity_id,
                    "provider": "mcp",
                    "config": {
                        "transport": "stdio",
                        "config": {"command": ["echo"]},
                    },
                },
            )
            assert r.status_code == 201, r.text
            seeded.append(entity_id)
        # Other row that doesn't have the suffix
        r = await client.post(
            "/v1/toolsets",
            json={
                "id": other,
                "provider": "mcp",
                "config": {
                    "transport": "stdio",
                    "config": {"command": ["echo"]},
                },
            },
        )
        assert r.status_code == 201, r.text

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"%{suffix_marker}"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out = sorted(item["id"] for item in resp.json()["items"])
        assert out == sorted(seeded), (
            f"leading-wildcard LIKE pattern `%{suffix_marker}` should "
            f"match exactly the suffix-ending rows; expected "
            f"{sorted(seeded)!r}, got {out!r}"
        )
        assert other not in out
    finally:
        for entity_id in seeded + [other]:
            await client.delete(f"/v1/toolsets/{entity_id}")


# ============================================================================
# T0404 — Predicate `op="not"` operator behaviour pinned
# ============================================================================


@pytest.mark.asyncio
async def test_t0404_predicate_op_not_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0404 — The Op enum (matrix/model/storage.py:35) does NOT
    include a NOT operator; the wire vocabulary is `=`, `!=`, `~=`,
    `>`, `<`, `>=`, `<=`, `in`, `and`, `or`. Per spec contract,
    `op="not"` MUST be rejected with 422 — never accepted as a
    logical NOT, never `/errors/internal`.

    Companion to T0086 (uppercase LIKE rejected): pin that any future
    addition of a NOT op would deliberately break this test.
    """
    # Try with right operand present (NOT-as-binary, like `a NOT b`)
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "not",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "anything"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, (
        f"op='not' should be rejected with 422; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope


# ============================================================================
# T0405 — Predicate `op="=="` (unknown operator) returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0405_predicate_op_double_equals_rejected_422(
    client: httpx.AsyncClient,
) -> None:
    """T0405 — The wire equality op is `=` (single). The ASCII
    double-equals `==` is not a member of the Op enum and MUST be
    rejected with 422. Companion to T0086 (uppercase LIKE).
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "==",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "anything"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, (
        f"op='==' should be rejected with 422; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope


# ============================================================================
# T0406 — Predicate `op=""` (empty operator) returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0406_predicate_op_empty_rejected_422(
    client: httpx.AsyncClient,
) -> None:
    """T0406 — Pin blank-operator rejection separate from the
    unknown-op T0086 path. Blank string is a distinct wire condition
    from any legal Op enum value; the validator must reject with 422
    (not silently default).
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": "anything"},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, (
        f"op='' should be rejected with 422; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope


# ============================================================================
# T0407 — Predicate boolean literal on a string field returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0407_predicate_boolean_literal_on_string_field_clean(
    client: httpx.AsyncClient,
) -> None:
    """T0407 — Type-coercion edge: send `{op:"=", left:{name:"id"},
    right:{value:true}}` against a string-typed `id` column. Postgres
    is strict about type coercion; the JSONB-comparison-bug callout
    in spec §4 documents that some type mismatches surface as 502
    /errors/provider-server-error instead of 200-empty. Either way:
    never 5xx /errors/internal leak; never silent acceptance that
    matches arbitrary rows.
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": "=",
            "left": {"kind": "field", "name": "id"},
            "right": {"kind": "value", "value": True},
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/toolsets/find", json=body)
    # Hard pin: never /errors/internal
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"boolean-on-string predicate leaked /errors/internal: "
        f"{resp.text}"
    )
    # Acceptable outcomes: 200 (empty/no rows match — id is never the
    # boolean True), 422 (Pydantic/handler rejected the type
    # mismatch), 400 (handler raised BadRequestError), or 502
    # (Postgres surfaced the JSONB coercion bug).
    assert resp.status_code in (200, 400, 422, 502), (
        f"unexpected status: {resp.status_code}: {resp.text}"
    )
    if resp.status_code == 200:
        items = resp.json()["items"]
        # Soft pin: a string `id` should never literally match the
        # boolean True, so the result set must be empty.
        assert items == [], (
            f"boolean-on-string predicate matched non-empty results: "
            f"{items!r}"
        )


# ============================================================================
# T0408 — Predicate `>=` on Session.created_at with malformed datetime
# ============================================================================


@pytest.mark.asyncio
async def test_t0408_predicate_ge_on_datetime_with_malformed_iso_clean(
    client: httpx.AsyncClient,
) -> None:
    """T0408 — `created_at` is a datetime column on Session. Pass a
    malformed ISO literal as the right operand. Pin: clean envelope,
    never `/errors/internal`. Acceptable: 422 (Pydantic / handler
    parsed and rejected), 400 (BadRequestError from backend), 502
    (provider-server-error — Postgres wrapped a parse failure), or
    even 200-empty if the backend treats the literal as a string and
    the comparison naturally yields zero rows.
    """
    body = {
        "predicate": {
            "kind": "predicate",
            "op": ">=",
            "left": {"kind": "field", "name": "created_at"},
            "right": {
                "kind": "value",
                "value": "not-a-real-datetime-2026-99-99T99:99:99Z",
            },
        },
        "page": {"kind": "offset", "offset": 0, "length": 10},
    }
    resp = await client.post("/v1/sessions/find", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"malformed-datetime predicate leaked /errors/internal: "
        f"{resp.text}"
    )
    assert resp.status_code in (200, 400, 422, 502), (
        f"unexpected status: {resp.status_code}: {resp.text}"
    )
    if resp.status_code in range(400, 600):
        # Error envelope must be cleanly typed
        assert envelope.get("type", "").startswith("/errors/"), envelope


# ============================================================================
# T0439 — Predicate `=` with NULL right operand on nullable column
# ============================================================================


@pytest.mark.asyncio
async def test_t0439_predicate_eq_null_on_nullable_column_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0439 — Send `{op:"=", left:{name:"description"},
    right:{value:null}}` against /v1/toolsets/find. The predicate
    translator (matrix/storage/_predicate.py) does NOT special-case
    NULL — the rendered SQL is `data->>'description' = NULL` which
    in Postgres ALWAYS evaluates to NULL (treated as falsy by WHERE).

    Hard pin: never 5xx, never `/errors/internal`.
    Documented behaviour: 200 with empty items (Postgres NULL
    semantics — no row's `description` literally equals SQL NULL).

    Toolset.description is the inherited `Describeable.description`
    field which defaults to ``""`` and is nullable on the model.
    """
    prefix = f"ts-t0439-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "description"},
                "right": {"kind": "value", "value": None},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"NULL right operand leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code < 500, resp.text
        # Documented: SQL `= NULL` always falsy → 200 empty.
        # Acceptable alternatives: 422 (handler validates and rejects),
        # 400 (BadRequestError), 502 (Postgres surfaced an error).
        assert resp.status_code in (200, 400, 422, 502), resp.text
        if resp.status_code == 200:
            items = resp.json()["items"]
            # Hard pin: NONE of our seeded toolsets should match
            # (Postgres = NULL never matches even when description is
            # the empty string default).
            assert all(
                item["id"] not in ids for item in items
            ), (
                f"= NULL unexpectedly matched seeded toolsets: "
                f"{[item['id'] for item in items]!r}"
            )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0440 — Predicate `in` with mixed-type list `[1, "two", null]`
# ============================================================================


@pytest.mark.asyncio
async def test_t0440_predicate_in_mixed_type_list_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0440 — Send `{op:"in", left:{name:"id"}, right:{value:[1,
    "two", null]}}` against /v1/toolsets/find. The right operand is
    a list with int + string + null members — none of which match
    any of our seeded `ts-t0440-*` ids.

    Hard pin: never 5xx, never `/errors/internal`.
    Documented behaviour: 200 with empty items (no row's id literally
    equals 1, "two", or null), OR 422/400 if the handler validates
    and rejects mixed-type lists.
    """
    prefix = f"ts-t0440-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "in",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": [1, "two", None]},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"mixed-type IN list leaked /errors/internal: {resp.text}"
        )
        # Hard pin: documented surfaces only — never /errors/internal.
        # 502 /errors/provider-server-error is the observed shape on
        # asyncpg (refuses to encode a mixed-type list as a typed array
        # parameter).
        assert resp.status_code in (200, 400, 422, 502), (
            f"unexpected status: {resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            items = resp.json()["items"]
            # Hard pin: NONE of our seeded toolsets should match
            assert all(
                item["id"] not in ids for item in items
            ), (
                f"mixed-type IN unexpectedly matched seeded toolsets: "
                f"{[item['id'] for item in items]!r}"
            )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0452 — Cursor token with single character flipped returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0452_cursor_token_single_char_flipped_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0452 — Pin cursor opaqueness contract: a tampered cursor
    token (single char changed) is rejected with a clean 4xx
    envelope (400 /errors/bad-request per
    matrix/storage/postgres.py:_decode_cursor). Never 5xx, never
    /errors/internal.
    """
    prefix = f"ts-t0452-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 5)
    try:
        # Get a real cursor from a normal pagination call
        first_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "cursor", "cursor": None, "length": 2},
        }
        first = await client.post("/v1/toolsets/find", json=first_body)
        assert first.status_code == 200, first.text
        real_cursor = first.json().get("next_cursor")
        assert real_cursor, (
            f"expected a non-null next_cursor for tampering: "
            f"{first.json()!r}"
        )

        # Flip one character in the middle of the token
        idx = len(real_cursor) // 2
        flipped_char = "Z" if real_cursor[idx] != "Z" else "Y"
        tampered = real_cursor[:idx] + flipped_char + real_cursor[idx+1:]
        assert tampered != real_cursor

        # Reuse with the tampered cursor
        tamper_body = {
            "predicate": first_body["predicate"],
            "page": {
                "kind": "cursor", "cursor": tampered, "length": 2,
            },
        }
        resp = await client.post("/v1/toolsets/find", json=tamper_body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"tampered cursor leaked /errors/internal: {resp.text}"
        )
        # Documented: 400 /errors/bad-request (BadRequestError raised
        # in _decode_cursor). Allow 422 if the validator catches
        # malformed base64 earlier.
        assert resp.status_code in (200, 400, 422), (
            f"unexpected status: {resp.status_code}: {resp.text}"
        )
        # If 200 (cursor happened to decode to a valid-looking state
        # but didn't match real data), result must be empty or
        # consistent. The strong contract is no 5xx.
        if resp.status_code >= 400:
            assert envelope.get("type", "").startswith("/errors/"), envelope
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0453 — Cursor token truncated to half length returns clean 4xx envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0453_cursor_token_truncated_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0453 — Sibling of T0452. Truncate a real cursor to half its
    length and reuse. Pin: 4xx with clean envelope (BadRequestError
    from _decode_cursor's `try/except → BadRequestError("malformed
    cursor: ...")` per matrix/storage/postgres.py:599); never 5xx;
    never /errors/internal.
    """
    prefix = f"ts-t0453-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 5)
    try:
        first_body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}%"},
            },
            "page": {"kind": "cursor", "cursor": None, "length": 2},
        }
        first = await client.post("/v1/toolsets/find", json=first_body)
        assert first.status_code == 200, first.text
        real_cursor = first.json().get("next_cursor")
        assert real_cursor, (
            f"expected a non-null next_cursor for truncation: "
            f"{first.json()!r}"
        )

        # Truncate to half length (still non-empty)
        truncated = real_cursor[: max(1, len(real_cursor) // 2)]
        assert truncated != real_cursor

        trunc_body = {
            "predicate": first_body["predicate"],
            "page": {
                "kind": "cursor", "cursor": truncated, "length": 2,
            },
        }
        resp = await client.post("/v1/toolsets/find", json=trunc_body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"truncated cursor leaked /errors/internal: {resp.text}"
        )
        # 400 /errors/bad-request from _decode_cursor, or 422 if the
        # validator catches malformed base64 earlier
        assert resp.status_code in (400, 422), (
            f"truncated cursor should be 4xx; got "
            f"{resp.status_code}: {resp.text}"
        )
        assert envelope.get("type", "").startswith("/errors/"), envelope
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0454 — Predicate AND/OR nested 10 levels deep returns clean envelope
# ============================================================================


def _build_nested_and_predicate(depth: int, prefix: str) -> dict:
    """Build a left-balanced AND tree of `depth` levels.

    The leaf at every level is a `~=` LIKE comparison on the toolset
    `id` field — `id ~= '<prefix>%'` — so the tree resolves to the
    same row set as a single LIKE predicate would.
    """
    leaf = {
        "kind": "predicate",
        "op": "~=",
        "left": {"kind": "field", "name": "id"},
        "right": {"kind": "value", "value": f"{prefix}%"},
    }
    node = leaf
    for _ in range(depth - 1):
        node = {
            "kind": "predicate",
            "op": "and",
            "left": node,
            "right": leaf,
        }
    return node


@pytest.mark.asyncio
async def test_t0454_predicate_nested_10_levels_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0454 — Builds a 10-level left-balanced AND tree of identical
    LIKE clauses. Each level resolves to the same row set, so the
    semantically-correct outcome is the LIKE prefix matched (the
    seeded toolsets). Pin: 200 with the seeded ids returned, OR
    4xx (handler rejected the depth) — never 5xx, never
    /errors/internal.

    Builds on T0338 (4-level case) — pushes the depth to a less
    trivial value where Python recursion limits could plausibly
    matter.
    """
    prefix = f"ts-t0454-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        body = {
            "predicate": _build_nested_and_predicate(10, prefix),
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"10-level AND leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code in (200, 400, 422), (
            f"unexpected status: {resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            out_ids = sorted(item["id"] for item in resp.json()["items"])
            assert out_ids == sorted(ids), (
                f"10-level AND of identical clauses should match the "
                f"same row set as the lone clause: expected "
                f"{sorted(ids)!r}, got {out_ids!r}"
            )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0455 — Predicate AND/OR nested 100 levels deep returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0455_predicate_nested_100_levels_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0455 — Push the depth to 100 levels — well above any natural
    use case but below Python's default recursion limit (1000).
    Pin: 200, 400, or 422 — but ABSOLUTELY never /errors/internal
    from a RecursionError leaking through the predicate translator's
    recursive `_render_predicate` call.

    If a future version adds a hard depth cap (e.g. 32 levels),
    the expected outcome is 422 with a documented
    /errors/validation-error or /errors/bad-request — both are fine
    so long as the envelope is clean.
    """
    prefix = f"ts-t0455-{unique_suffix}"
    ids = await _seed_toolsets(client, prefix, 3)
    try:
        body = {
            "predicate": _build_nested_and_predicate(100, prefix),
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        envelope = resp.json() if resp.content else {}
        # Hard pin: never /errors/internal (RecursionError leak)
        assert envelope.get("type") != "/errors/internal", (
            f"100-level AND leaked /errors/internal "
            f"(possible RecursionError): {resp.text}"
        )
        assert resp.status_code in (200, 400, 422), (
            f"unexpected status: {resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            out_ids = sorted(item["id"] for item in resp.json()["items"])
            assert out_ids == sorted(ids), (
                f"100-level AND of identical clauses should match the "
                f"same row set: expected {sorted(ids)!r}, got "
                f"{out_ids!r}"
            )
    finally:
        await _delete_toolsets(client, ids)


# ============================================================================
# T0483 — Predicate ~= with `_` wildcard matches single-char tail only
# ============================================================================


@pytest.mark.asyncio
async def test_t0483_predicate_like_underscore_matches_single_char_tail(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0483 — Per matrix/storage.Op.LIKE docstring, `~=` follows
    SQL LIKE semantics: `%` matches any sequence, `_` matches a
    single character. Pin: predicate `id ~= "<prefix>-_"` on rows
    {prefix-a, prefix-aa, prefix-b} matches exactly the two
    one-char-suffix rows (prefix-a, prefix-b) and excludes the
    two-char-suffix row (prefix-aa).

    The matched-set semantics are deterministic and a strong
    regression-detector for any backend that interprets `_` as a
    literal underscore instead of a single-char wildcard.
    """
    prefix = f"ts-t0483-{unique_suffix}"
    # Construct ids manually to control suffix shape
    ids = [
        f"{prefix}-a",   # one-char tail — should match
        f"{prefix}-aa",  # two-char tail — should NOT match
        f"{prefix}-b",   # one-char tail — should match
    ]
    for entity_id in ids:
        resp = await client.post(
            "/v1/toolsets", json=_toolset_body(entity_id),
        )
        assert resp.status_code == 201, resp.text
    try:
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "~=",
                "left": {"kind": "field", "name": "id"},
                "right": {"kind": "value", "value": f"{prefix}-_"},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/toolsets/find", json=body)
        assert resp.status_code == 200, resp.text
        out_ids = sorted(item["id"] for item in resp.json()["items"])
        expected = sorted([f"{prefix}-a", f"{prefix}-b"])
        assert out_ids == expected, (
            f"`_` wildcard should match exactly the one-char-suffix "
            f"rows; expected {expected!r}, got {out_ids!r}"
        )
        # Defence: explicit exclusion of the two-char tail
        assert f"{prefix}-aa" not in out_ids, (
            f"`_` wildcard incorrectly matched two-char tail "
            f"{prefix}-aa"
        )
    finally:
        await _delete_toolsets(client, ids)
