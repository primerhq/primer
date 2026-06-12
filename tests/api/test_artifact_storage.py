"""HTTP-surface tests for /v1/artifact_storage_providers CRUD."""

from __future__ import annotations

import pytest

from primer.api.registries.artifact_storage_registry import (
    DEFAULT_ARTIFACT_PROVIDER_ID,
)


@pytest.mark.asyncio
async def test_artifact_provider_crud_round_trip(client):
    body = {"id": "asp-rt", "provider": "db", "config": {}}
    r = await client.post("/v1/artifact_storage_providers", json=body)
    assert r.status_code == 201, r.text
    try:
        r = await client.get("/v1/artifact_storage_providers/asp-rt")
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["id"] == "asp-rt"
        assert got["provider"] == "db"
    finally:
        r = await client.delete("/v1/artifact_storage_providers/asp-rt")
        assert r.status_code in (200, 204), r.text


@pytest.mark.asyncio
async def test_reserved_default_seeded_and_protected(client):
    r = await client.get(
        f"/v1/artifact_storage_providers/{DEFAULT_ARTIFACT_PROVIDER_ID}")
    assert r.status_code == 200, r.text
    # Deleting the reserved default is forbidden.
    r = await client.delete(
        f"/v1/artifact_storage_providers/{DEFAULT_ARTIFACT_PROVIDER_ID}")
    assert r.status_code == 403, r.text
