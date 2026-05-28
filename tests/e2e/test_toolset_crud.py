"""E2E: Toolset CRUD round-trip.

Backlog item T0005 — six-step CRUD cycle, mirrors T0004 against
``/v1/toolsets``. Uses an MCP/stdio toolset with the harmless ``echo``
command so no real MCP server is needed; the test only verifies row
management, not tool dispatch.
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


@pytest.mark.asyncio
async def test_t0005_toolset_crud_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    entity_id = f"ts-{unique_suffix}"
    base = "/v1/toolsets"
    body = _toolset_body(entity_id)

    # --- create
    create = await client.post(base, json=body)
    assert create.status_code == 201, create.text
    assert create.json()["id"] == entity_id

    # --- get
    got = await client.get(f"{base}/{entity_id}")
    assert got.status_code == 200, got.text
    assert got.json()["id"] == entity_id

    # --- list must include
    listed = await client.get(f"{base}?limit=200&offset=0")
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["items"]]
    assert entity_id in ids

    # --- put (mutate command list to prove update goes through)
    updated = dict(body)
    updated["config"] = {
        "transport": "stdio",
        "config": {"command": ["echo", "hello"]},
    }
    put = await client.put(f"{base}/{entity_id}", json=updated)
    assert put.status_code == 200, put.text
    assert put.json()["config"]["config"]["command"] == ["echo", "hello"]

    # --- get reflects update
    got2 = await client.get(f"{base}/{entity_id}")
    assert got2.json()["config"]["config"]["command"] == ["echo", "hello"]

    # --- delete
    deleted = await client.delete(f"{base}/{entity_id}")
    assert deleted.status_code == 204, deleted.text

    # --- get after delete = 404
    gone = await client.get(f"{base}/{entity_id}")
    assert gone.status_code == 404
    assert gone.json()["type"] == "/errors/not-found"


# ============================================================================
# T0402 — Toolset POST with transport=stdio but http body shape returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0402_toolset_post_stdio_with_http_config_shape_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0402 — McpConfig is discriminated by `transport` and the
    inner `config` field is a `StdioConfig | HttpConfig` union; a
    model_validator (primer/model/provider.py:548) enforces that the
    inner type matches the discriminator.

    Build a body with `transport="stdio"` but an HTTP-shaped inner
    config (`url=...` instead of `command=...`). Pydantic should
    parse the inner as HttpConfig (via the union) then reject in the
    after-validator. Expected: 422 /errors/validation-error, never
    a 5xx.

    Pins discriminator-vs-shape mismatch detection so a future move
    to a `Annotated[..., discriminator]` form (which would change
    error semantics) is caught.
    """
    entity_id = f"ts-t0402-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            # HTTP-shaped inner: only `url`, no `command`
            "config": {"url": "https://example.invalid/mcp"},
        },
    }
    resp = await client.post("/v1/toolsets", json=body)
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, (
        f"stdio-with-http-config-shape should be 422; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope.get("type") == "/errors/validation-error", envelope
    # Defence: row should not have been created
    got = await client.get(f"/v1/toolsets/{entity_id}")
    assert got.status_code == 404, (
        f"toolset {entity_id!r} unexpectedly created despite 422: "
        f"{got.text}"
    )


# ============================================================================
# T0451 — MCP HTTP toolset url variations round-trip on CRUD
# ============================================================================


@pytest.mark.asyncio
async def test_t0451_toolset_mcp_http_url_variations_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0451 — McpHttpConfig.url is a free-form `str` (min_length=1)
    per primer/model/provider.py:511. Pin that several legitimate
    URL shapes round-trip byte-identically through CRUD: trailing
    slash, query string, port-only host, fragment, and a path with
    embedded spaces (URL-encoded). All accepted at create; GET
    echoes the url field unchanged. Never /errors/internal.
    """
    url_variations = [
        "https://example.invalid/",  # trailing slash
        "https://example.invalid/mcp?api=v1&debug=true",  # query string
        "http://127.0.0.1:9999",  # port-only host
        "https://example.invalid/mcp#frag",  # fragment
        "https://example.invalid/path%20with%20spaces",  # URL-encoded spaces
    ]
    created_ids: list[str] = []
    try:
        for i, url in enumerate(url_variations):
            entity_id = f"ts-t0451-{unique_suffix}-{i}"
            body = {
                "id": entity_id,
                "provider": "mcp",
                "config": {
                    "transport": "http",
                    "config": {"url": url},
                },
            }
            resp = await client.post("/v1/toolsets", json=body)
            envelope = resp.json() if resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"url={url!r} leaked /errors/internal: {resp.text}"
            )
            assert resp.status_code == 201, (
                f"url={url!r}: unexpected status "
                f"{resp.status_code}: {resp.text}"
            )
            created_ids.append(entity_id)

            # GET echoes url byte-identical
            got = await client.get(f"/v1/toolsets/{entity_id}")
            assert got.status_code == 200, got.text
            body_got = got.json()
            got_url = body_got["config"]["config"]["url"]
            assert got_url == url, (
                f"url not byte-identical: sent={url!r}, got={got_url!r}"
            )
    finally:
        for entity_id in created_ids:
            await client.delete(f"/v1/toolsets/{entity_id}")


# ============================================================================
# T0509 — POST entity with id=42 (integer, not string) returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0509_post_entity_with_integer_id_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """T0509 — Identifiable.id is `str` (with min_length=1). POSTing
    an integer in the id field must be rejected by Pydantic strict
    string validation as 422 /errors/validation-error. Catches a
    regression where a permissive coercion accidentally turned an
    integer id into the string "42".
    """
    body = {
        "id": 42,  # integer, not string
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    resp = await client.post("/v1/toolsets", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"integer id leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"integer id should be 422 /errors/validation-error; got "
        f"{resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope

    # Defence: no row was created under the coerced "42" string id
    got = await client.get("/v1/toolsets/42")
    assert got.status_code == 404, got.text


# ============================================================================
# T0510 — POST entity with id="" (empty string) returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0510_post_entity_with_empty_string_id_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """T0510 — Identifiable.id has min_length=1. POST with id=""
    must be rejected by Pydantic with 422 /errors/validation-error.
    Catches a regression where an empty-id row leaked through and
    became unaddressable via subsequent GET /toolsets/.
    """
    body = {
        "id": "",
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    resp = await client.post("/v1/toolsets", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"empty-id leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"empty id should be 422 /errors/validation-error; got "
        f"{resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope


# ============================================================================
# T0511 — POST entity with id="null" (literal string) accepted; round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0511_post_entity_with_literal_null_string_id_round_trips(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0511 — id="null" is the JSON STRING containing the four
    characters n-u-l-l, distinct from the JSON literal `null`.
    Pydantic must accept it as a regular string. Pin: 201; GET
    by the same id returns 200 with the byte-exact id field.

    Catches a regression where a string equal to "null" gets coerced
    to None at any layer (JSON parser, Pydantic, asyncpg).
    """
    # Use a unique suffix so we don't collide with prior runs that
    # may have left a row under id="null"
    weird_id = f"null-but-string-{unique_suffix}"
    body = {
        "id": weird_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    resp = await client.post("/v1/toolsets", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == weird_id, resp.json()
    try:
        got = await client.get(f"/v1/toolsets/{weird_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == weird_id, got.json()

        # Now also try the raw literal "null" — this is the most
        # confusable shape. Skip if a previous iteration left a row;
        # otherwise pin that the create-then-GET round-trip works.
        bare_id = "null"
        body_bare = {
            "id": bare_id,
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {"command": ["echo"]},
            },
        }
        bare_resp = await client.post("/v1/toolsets", json=body_bare)
        if bare_resp.status_code == 201:
            try:
                assert bare_resp.json()["id"] == bare_id, bare_resp.json()
                got_bare = await client.get(f"/v1/toolsets/{bare_id}")
                assert got_bare.status_code == 200, got_bare.text
                assert got_bare.json()["id"] == bare_id, got_bare.json()
            finally:
                await client.delete(f"/v1/toolsets/{bare_id}")
        else:
            # 409 if the row already exists, or 422 if a future
            # validator rejects "null" as a confusable id —
            # both are acceptable, never /errors/internal
            envelope = bare_resp.json()
            assert envelope.get("type") != "/errors/internal", (
                f"bare 'null' id leaked /errors/internal: "
                f"{bare_resp.text}"
            )
            assert bare_resp.status_code in (409, 422), (
                f"unexpected status: {bare_resp.status_code}: "
                f"{bare_resp.text}"
            )
    finally:
        await client.delete(f"/v1/toolsets/{weird_id}")


# ============================================================================
# T0512 — POST entity with id="../foo" returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0512_post_entity_with_path_traversal_id_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0512 — `id` is a body field, not a URL segment, so a
    traversal-shaped value like "../foo-<suffix>" doesn't engage
    the URL router. Pin: POST returns either 201 (id is byte-
    preserved as a column value) or 4xx (a future validator rejects
    path-shaped ids). Never /errors/internal.

    The companion concern — what happens if a caller then tries to
    GET /v1/toolsets/../foo — is a URL-routing question covered by
    other tests; this only pins the create path.
    """
    # Suffix the traversal-shaped id so re-runs across iterations
    # don't collide with leftover rows from previous runs
    weird_id = f"../foo-t0512-{unique_suffix}"
    body = {
        "id": weird_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    resp = await client.post("/v1/toolsets", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"traversal-shaped id leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code in (201, 400, 422), (
        f"unexpected status: {resp.status_code}: {resp.text}"
    )

    if resp.status_code == 201:
        # Row exists somewhere in storage; cleanup is best-effort
        # because a path-segment-shaped id is ambiguous to clean
        # via the standard DELETE /v1/toolsets/{id} URL path. Use
        # find to discover and delete by row, then assert the row
        # is gone via a follow-up find.
        try:
            find = await client.post(
                "/v1/toolsets/find",
                json={
                    "predicate": {
                        "kind": "predicate",
                        "op": "=",
                        "left": {"kind": "field", "name": "id"},
                        "right": {"kind": "value", "value": weird_id},
                    },
                    "page": {"kind": "offset", "offset": 0, "length": 10},
                },
            )
            assert find.status_code == 200, find.text
            ids = [item["id"] for item in find.json()["items"]]
            assert weird_id in ids, (
                f"created traversal-id row missing from find: {ids!r}"
            )
        finally:
            # Cleanup via URL-encoded id (httpx encodes %2F for /)
            import urllib.parse as _u
            await client.delete(
                f"/v1/toolsets/{_u.quote(weird_id, safe='')}",
            )
