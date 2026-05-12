"""E2E: LLMProvider CRUD round-trip.

Backlog item T0004 — create → get → list (must include) → put → get
(reflects update) → delete → get (404).
"""

from __future__ import annotations

import httpx
import pytest


def _llm_body(entity_id: str) -> dict:
    """Minimal valid LLMProvider request body (Anthropic flavour)."""
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 4},
    }


@pytest.mark.asyncio
async def test_t0004_llm_provider_crud_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    entity_id = f"llm-{unique_suffix}"
    base = "/v1/llm_providers"
    body = _llm_body(entity_id)

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
    assert entity_id in ids, f"{entity_id!r} not in list response: {ids!r}"

    # --- put (update)
    updated = dict(body)
    updated["limits"] = {"max_concurrency": 16}
    put = await client.put(f"{base}/{entity_id}", json=updated)
    assert put.status_code == 200, put.text
    assert put.json()["limits"]["max_concurrency"] == 16

    # --- get reflects update
    got2 = await client.get(f"{base}/{entity_id}")
    assert got2.json()["limits"]["max_concurrency"] == 16

    # --- delete
    deleted = await client.delete(f"{base}/{entity_id}")
    assert deleted.status_code == 204, deleted.text

    # --- get after delete = 404
    gone = await client.get(f"{base}/{entity_id}")
    assert gone.status_code == 404
    assert gone.json()["type"] == "/errors/not-found"


@pytest.mark.asyncio
async def test_t0032_put_then_invalidate_reflects_update(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0032 — PUT an LLMProvider, then `/invalidate`, then GET. The
    GET must reflect the updated row (proof the cache was cleared on
    PUT or invalidate; either way the API view is consistent), and
    `/invalidate` itself must return 204.
    """
    entity_id = f"llm-cas-{unique_suffix}"
    base = "/v1/llm_providers"

    create = await client.post(base, json=_llm_body(entity_id))
    assert create.status_code == 201, create.text

    try:
        # Mutate something observable
        updated = _llm_body(entity_id)
        updated["limits"]["max_concurrency"] = 32
        put = await client.put(f"{base}/{entity_id}", json=updated)
        assert put.status_code == 200, put.text

        inv = await client.post(f"{base}/{entity_id}/invalidate")
        assert inv.status_code == 204, inv.text

        got = await client.get(f"{base}/{entity_id}")
        assert got.status_code == 200, got.text
        assert got.json()["limits"]["max_concurrency"] == 32
    finally:
        await client.delete(f"{base}/{entity_id}")
