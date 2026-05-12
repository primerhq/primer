"""E2E: idempotent operations per §17 invariant #5.

Covers backlog items T0026 (LLMProvider invalidate) and T0028 (worker
drain). DELETE is explicitly NOT idempotent — see T0009.
"""

from __future__ import annotations

import httpx
import pytest


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


@pytest.mark.asyncio
async def test_t0026_llm_provider_invalidate_idempotent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0026 — POST /v1/llm_providers/{id}/invalidate is idempotent.

    Two consecutive calls must both return 204; the second is a no-op
    because the cache is already cold after the first.
    """
    entity_id = f"llm-inv-{unique_suffix}"
    created = await client.post("/v1/llm_providers", json=_llm_body(entity_id))
    assert created.status_code == 201, created.text
    try:
        first = await client.post(f"/v1/llm_providers/{entity_id}/invalidate")
        assert first.status_code == 204, first.text
        second = await client.post(f"/v1/llm_providers/{entity_id}/invalidate")
        assert second.status_code == 204, (
            f"second invalidate expected 204 (idempotent), got "
            f"{second.status_code}: {second.text}"
        )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0028_worker_drain_idempotent(
    client: httpx.AsyncClient,
) -> None:
    """T0028 — POST /v1/workers/{id}/drain is idempotent.

    Both calls must return 204; afterwards GET /v1/workers must show
    the worker's status as ``draining`` (per WorkerInfo's literal
    field — the test does not check for a boolean ``draining`` key).
    """
    # 1. Discover the live worker (api+worker mode is what bringup
    #    starts, so exactly one worker is registered).
    listed = await client.get("/v1/workers")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items, (
        f"expected at least one registered worker, got: {listed.json()!r}"
    )
    worker_id = items[0]["id"]

    # 2. Drain twice — both must return 204.
    first = await client.post(f"/v1/workers/{worker_id}/drain")
    assert first.status_code == 204, first.text
    second = await client.post(f"/v1/workers/{worker_id}/drain")
    assert second.status_code == 204, (
        f"second drain expected 204 (idempotent), got "
        f"{second.status_code}: {second.text}"
    )

    # 3. GET /v1/workers shows status=draining for that worker.
    listed_after = await client.get("/v1/workers")
    assert listed_after.status_code == 200
    statuses = {w["id"]: w["status"] for w in listed_after.json()["items"]}
    assert statuses.get(worker_id) == "draining", (
        f"expected status=draining for {worker_id!r}, got {statuses!r}"
    )
