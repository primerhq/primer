"""Toolset delete must 409 when a ToolApprovalPolicy still references it."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_toolset_delete_blocked_by_policy(client):
    # 1. Create a (user) toolset.
    r = await client.post(
        "/v1/toolsets",
        json={"id": "ts-cas", "provider": "internal"},
    )
    assert r.status_code == 201

    # 2. Create an approval policy referencing it.
    r = await client.post(
        "/v1/tool_approval_policies",
        json={
            "id": "p-cas",
            "toolset_id": "ts-cas",
            "tool_name": "any",
            "approval": {"type": "required"},
        },
    )
    assert r.status_code == 201

    # 3. Try to delete the toolset → 409.
    r = await client.delete("/v1/toolsets/ts-cas")
    assert r.status_code == 409, r.text

    # 4. Delete the policy first.
    r = await client.delete("/v1/tool_approval_policies/p-cas")
    assert r.status_code in (200, 204)

    # 5. Now the toolset deletes cleanly.
    r = await client.delete("/v1/toolsets/ts-cas")
    assert r.status_code in (200, 204)
