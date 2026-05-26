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


# ----------------------------------------------------------------------------
# GET /v1/tools — the merged per-tool catalogue powering the agent picker
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tools_returns_one_entry_per_toolset(client):
    """The fan-out endpoint includes every built-in toolset (always-on
    + search) regardless of availability so the UI can render the
    grouped picker without a second round-trip."""
    r = await client.get("/v1/tools")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    # All five built-ins appear (search is gated but still listed).
    for required in ("system", "workspaces", "search", "misc", "web"):
        assert required in ids, f"missing built-in {required!r} in {ids!r}"


@pytest.mark.asyncio
async def test_all_tools_emits_scoped_ids(client):
    """Every tool in the catalogue carries a scoped_id of the form
    ``<toolset_id>__<tool_name>`` — the agent picker's allowlist
    uses scoped ids directly so a typo here breaks the round-trip."""
    r = await client.get("/v1/tools")
    body = r.json()
    for ts in body["items"]:
        if not ts.get("available"):
            continue
        for tool in ts["tools"]:
            assert tool["scoped_id"] == f"{ts['id']}__{tool['id']}", (
                f"bad scoped_id for {ts['id']}.{tool['id']}: {tool['scoped_id']!r}"
            )


@pytest.mark.asyncio
async def test_all_tools_marks_search_unavailable_without_ic(client):
    r = await client.get("/v1/tools")
    by_id = {it["id"]: it for it in r.json()["items"]}
    assert by_id["search"]["available"] is False
    assert "unavailable_reason" in by_id["search"]
    # Tools list stays empty for unavailable toolsets.
    assert by_id["search"]["tools"] == []
