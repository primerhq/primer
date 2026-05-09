"""Smoke test for GET /v1/health."""

from __future__ import annotations

import pytest

from matrix.api.version import APP_VERSION


@pytest.mark.asyncio
async def test_health_returns_ok(client) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": APP_VERSION}
