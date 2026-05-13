"""E2E: built-in (always-on) toolset listings.

Covers backlog items T0140 (`_system` toolset) and T0141 (`_workspaces`
toolset). Spec §8 lists both as always-on; the lifespan handler builds
them at startup and registers them with the ProviderRegistry, so a
`GET /v1/toolsets/{id}/tools` against either id should succeed without
any provider row existing in storage.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0140_system_toolset_lists_tools(
    client: httpx.AsyncClient,
) -> None:
    """T0140 — `GET /v1/toolsets/_system/tools` returns 200 with a
    non-empty list whose entries carry the documented fields:
    ``id``, ``description``, ``schema``, ``toolset_id``.

    NB: tools expose an ``id`` (not ``name``) — this is the canonical
    invocation handle the model uses.
    """
    resp = await client.get("/v1/toolsets/_system/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tools = body.get("tools")
    assert isinstance(tools, list) and tools, body
    for tool in tools:
        assert "id" in tool, tool
        assert "description" in tool, tool
        assert isinstance(tool["id"], str) and tool["id"], tool
        # toolset_id should consistently identify the parent toolset
        assert tool.get("toolset_id") == "_system", tool


@pytest.mark.asyncio
async def test_t0141_workspaces_toolset_lists_tools(
    client: httpx.AsyncClient,
) -> None:
    """T0141 — `GET /v1/toolsets/_workspaces/tools` returns 200 with
    the workspace-tool family. The exact tool ids are implementation
    detail, but at minimum some tool's id or description should
    reference exec / file operations.
    """
    resp = await client.get("/v1/toolsets/_workspaces/tools")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tools = body.get("tools")
    assert isinstance(tools, list) and tools, body

    # Combine ids + descriptions into one searchable string
    haystack = " ".join(
        f"{t.get('id', '')} {t.get('description', '')}".lower()
        for t in tools
    )
    assert any(
        keyword in haystack
        for keyword in ("exec", "read", "write", "list", "file")
    ), f"no workspace-tool keyword found in tool list: {[t.get('id') for t in tools]!r}"

    # Each tool also reports the toolset_id consistently
    for tool in tools:
        assert tool.get("toolset_id") == "_workspaces", tool
