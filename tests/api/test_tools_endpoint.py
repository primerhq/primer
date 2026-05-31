"""GET /v1/tools/catalogue returns the platform's flat tool catalogue.

Spec B §3.4 — consumed by the Phase 9 graph editor's ToolCall picker.

Lives at ``/tools/catalogue`` (not the bare ``/tools``) to avoid colliding
with the pre-existing per-toolset-grouped catalogue at ``GET /v1/tools``
that the operator console's existing tool/agent pages already consume.
Phase 9's editor consumes the flat shape via the new path.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_tools_returns_flat_list(client) -> None:
    resp = await client.get("/v1/tools/catalogue")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    # The fake test app wires the always-on built-in toolsets, so the
    # catalogue should NOT be empty (system/workspaces/misc/web/harness
    # each expose at least one tool).
    assert len(body["items"]) > 0
    for item in body["items"]:
        assert "id" in item
        # scoped id (e.g. ``system__list_models``, ``web__search``)
        assert "__" in item["id"]
        assert "description" in item
        assert "input_schema" in item
        assert isinstance(item["input_schema"], dict)


@pytest.mark.asyncio
async def test_list_tools_ids_are_unique(client) -> None:
    """A scoped id should never appear twice — toolset_id + tool name
    is the picker's natural key, and the editor relies on it being
    unique across the catalogue."""
    resp = await client.get("/v1/tools/catalogue")
    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["items"]]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


@pytest.mark.asyncio
async def test_list_tools_requires_auth(raw_client) -> None:
    """Without the auth cookie, /v1/tools/catalogue rejects."""
    resp = await raw_client.get("/v1/tools/catalogue")
    # Existing pattern across the API: 401 from the auth dependency.
    assert resp.status_code in (401, 403)
