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
