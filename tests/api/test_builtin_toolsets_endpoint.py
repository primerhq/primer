"""HTTP-surface tests for GET /v1/toolsets/builtin."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_builtin_toolsets_returns_five_items(client):
    r = await client.get("/v1/toolsets/builtin")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    ids = [it["id"] for it in items]
    assert ids == ["system", "workspaces", "search", "misc", "web"]


@pytest.mark.asyncio
async def test_builtin_toolsets_search_unavailable_when_ic_off(client):
    r = await client.get("/v1/toolsets/builtin")
    assert r.status_code == 200, r.text
    by_id = {it["id"]: it for it in r.json()["items"]}
    # In-process test fixture has no IC config row → search is unavailable
    assert by_id["search"]["available"] is False
    # Always-on built-ins are available
    assert by_id["system"]["available"] is True
    assert by_id["workspaces"]["available"] is True
    assert by_id["misc"]["available"] is True
    assert by_id["web"]["available"] is True


@pytest.mark.asyncio
async def test_builtin_toolsets_each_item_has_required_fields(client):
    r = await client.get("/v1/toolsets/builtin")
    items = r.json()["items"]
    for it in items:
        assert isinstance(it["id"], str) and it["id"]
        assert isinstance(it["tagline"], str) and it["tagline"]
        assert isinstance(it["icon"], str) and it["icon"]
        assert isinstance(it["always_on"], bool)
        assert isinstance(it["available"], bool)


@pytest.mark.asyncio
async def test_builtin_toolsets_route_not_shadowed_by_crud_get_by_id(client):
    # Defence against a regression where someone refactors and breaks
    # the route-registration order: /toolsets/builtin must NOT be
    # interpreted as toolset_id=builtin by the CRUD router (which
    # would return 404 because no such row exists).
    r = await client.get("/v1/toolsets/builtin")
    assert r.status_code == 200, r.text
    # The response shape is the builtin list, not a single Toolset row.
    body = r.json()
    assert "items" in body
    assert "config" not in body  # not the CRUD Toolset row shape
