"""Scenario 2 — InvalidationBus cross-process cache delivery.

Creates an ``LLMProvider`` via API#0, fetches it on API#1 (which
populates API#1's local in-process registry cache), then updates the
provider via API#0 and immediately reads it back via API#1.

After the update the bus broadcasts an invalidation event over
Postgres LISTEN/NOTIFY.  API#1's subscription handler evicts the
stale entry from its registry cache.  On the next ``GET`` the registry
re-reads from storage and returns the updated value.

``wait_for`` is used to tolerate the small propagation lag inherent in
asynchronous LISTEN/NOTIFY delivery; the default 10-second timeout is
generous for a local Postgres instance.

Requires:
- A live Postgres container + Docker for testcontainers.
- The distributed marker (``pytest -m distributed``).

The test uses ``LLMProvider`` with an ``openresponses`` (OpenAI-
compatible) backend so no real API key is needed — the entity is
stored and read as data, never used for live LLM calls.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from tests.distributed.cluster import TestCluster
from tests._support.smk import smk


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cluster_2x2_bus(postgres_container: str, db_schema: str):
    """2 API + 2 worker cluster for the invalidation-bus scenario."""
    cluster = TestCluster(
        postgres_url=postgres_container,
        api_count=2,
        worker_count=2,
        start_port=8310,
        schema=db_schema,
    )
    await cluster.start()
    try:
        yield cluster
    finally:
        await cluster.stop()


# ---------------------------------------------------------------------------
# Scenario 2
# ---------------------------------------------------------------------------


@smk("SMK-DST-07")
@pytest.mark.distributed
@pytest.mark.asyncio
async def test_provider_patch_invalidates_other_api_cache(
    cluster_2x2_bus: TestCluster,
) -> None:
    """PUT on API#0 must be visible via API#1 after cache invalidation.

    Steps:
    1. POST a new ``LLMProvider`` via API#0.
    2. GET it via API#1 — this populates API#1's in-process registry cache.
    3. PUT the provider with an updated ``models`` list via API#0.
    4. Poll API#1 until the GET response reflects the new model name (the
       bus has delivered the invalidation and the cache has been evicted).
    """
    cluster = cluster_2x2_bus
    await cluster.authenticate()
    provider_id = f"test-llm-{uuid.uuid4().hex[:8]}"

    original_model = "original-model-v1"
    updated_model = "updated-model-v2"

    provider_body = {
        "id": provider_id,
        "provider": "openresponses",
        "models": [{"name": original_model, "context_length": 4096}],
        "config": {
            "url": "http://localhost:11434/v1",
            "api_key": None,
            "flavor": "other",
        },
        "limits": {"max_concurrency": 4},
    }

    # ------------------------------------------------------------------
    # 1. Create the provider via API#0
    # ------------------------------------------------------------------
    async with cluster.client(0) as c0:
        resp = await c0.post("/v1/llm_providers", json=provider_body)
        assert resp.status_code == 201, (
            f"POST /v1/llm_providers returned {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # 2. Read it via API#1 to warm the cache
    # ------------------------------------------------------------------
    async with cluster.client(1) as c1:
        resp = await c1.get(f"/v1/llm_providers/{provider_id}")
        assert resp.status_code == 200, (
            f"GET /v1/llm_providers/{provider_id} on api-1 returned"
            f" {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert any(
            m["name"] == original_model for m in data["models"]
        ), f"api-1 does not see {original_model!r} in {data['models']}"

    # ------------------------------------------------------------------
    # 3. Update the provider (new model name) via API#0
    # ------------------------------------------------------------------
    updated_body = {
        **provider_body,
        "models": [{"name": updated_model, "context_length": 8192}],
    }
    async with cluster.client(0) as c0:
        resp = await c0.put(
            f"/v1/llm_providers/{provider_id}", json=updated_body
        )
        assert resp.status_code == 200, (
            f"PUT /v1/llm_providers/{provider_id} returned"
            f" {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # 4. Poll API#1 until it returns the updated model name.
    #    The bus delivers the invalidation asynchronously; we give it
    #    up to 10 seconds.
    # ------------------------------------------------------------------
    last_response: dict = {}

    async def _api1_sees_update() -> bool:
        nonlocal last_response
        async with cluster.client(1) as c1:
            resp = await c1.get(f"/v1/llm_providers/{provider_id}")
            if resp.status_code != 200:
                return False
            last_response = resp.json()
        return any(
            m["name"] == updated_model
            for m in last_response.get("models", [])
        )

    await cluster.wait_for(_api1_sees_update, timeout_s=10.0, interval_s=0.2)

    assert any(
        m["name"] == updated_model
        for m in last_response.get("models", [])
    ), (
        f"API#1 still returns stale model list after invalidation."
        f" Last response: {last_response}"
    )
