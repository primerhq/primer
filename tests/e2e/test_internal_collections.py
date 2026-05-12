"""E2E: internal-collections subsystem gating.

Covers backlog item T0020 — when the subsystem hasn't been activated
(no config row), ``GET /v1/internal_collections/config`` returns 404
with the ``/errors/not-found`` envelope.

Bringup runs against a freshly-created database, so the config row is
guaranteed absent at the start of every iteration. This test does NOT
create the config (that would interfere with sibling tests that rely
on the subsystem being inactive); instead it only asserts the absence
behaviour.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0020_internal_collections_config_404_when_inactive(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/internal_collections/config")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["type"] == "/errors/not-found"
    assert body["status"] == 404
    # detail should point operators at the activation path.
    assert "PUT" in body["detail"] or "configure" in body["detail"].lower()
