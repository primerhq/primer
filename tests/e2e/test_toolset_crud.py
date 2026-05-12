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
