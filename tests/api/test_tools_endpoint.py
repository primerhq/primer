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
        # y/w/r/n capability badges (primer.model.chat.Tool.yields /
        # .requires_workspace / .tool_class / .required_role) -- excluded
        # from Tool's own wire serialisation but re-added explicitly here.
        assert isinstance(item["yields"], bool)
        assert isinstance(item["requires_workspace"], bool)
        assert item["tool_class"] in ("standard", "notifying")
        assert item["required_role"] is None or isinstance(
            item["required_role"], str
        )


@pytest.mark.asyncio
async def test_catalogue_badges_reflect_tool_metadata(client) -> None:
    """The badges are not just present -- they carry each tool's real
    metadata, matching what ``make_tool`` declared at the call site."""
    resp = await client.get("/v1/tools/catalogue")
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()["items"]}

    # ask_user: yielding, ordinary (no workspace requirement).
    ask_user = by_id["system__ask_user"]
    assert ask_user["yields"] is True
    assert ask_user["requires_workspace"] is False

    # wait_for_event: yielding AND workspace-only.
    wait_for_event = by_id["system__wait_for_event"]
    assert wait_for_event["yields"] is True
    assert wait_for_event["requires_workspace"] is True

    # Not every tool yields -- the flag must vary, not be hardcoded True.
    assert any(not item["yields"] for item in by_id.values())


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
