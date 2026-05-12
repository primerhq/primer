"""E2E: pagination contract per §4 of the app spec.

Covers backlog item T0010 (zero-items envelope shape).
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0010_agents_pagination_zero_items(
    client: httpx.AsyncClient,
) -> None:
    """T0010 — GET /v1/agents on a fresh DB returns an offset envelope
    with items=[], total=0, offset=0."""
    resp = await client.get("/v1/agents?limit=50&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["offset"] == 0
    # Spec §4: OffsetPageResponse carries 'length' as well.
    assert body["length"] == 0
