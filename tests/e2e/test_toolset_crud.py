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
    model_validator (matrix/model/provider.py:548) enforces that the
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
    per matrix/model/provider.py:511. Pin that several legitimate
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
