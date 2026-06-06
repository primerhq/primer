"""SMK MCP integration tests.

Direction A (Primer as MCP client): mount the in-repo stdio/http fixture
servers as toolsets, select tools, call one end to end, and confirm failure
isolation. Direction B (Primer as server): the /v1/mcp exposure allowlist,
system-only floor, and disable. The external-client list/invoke (MCP-07/08)
and endpoint auth (MCP-10) need an MCP client speaking to /v1/mcp.
"""
from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


async def _mount_stdio(authed_client, command, suffix) -> str:
    tid = f"mcp-stdio-{suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "stdio", "config": {"command": command}}},
    )
    assert r.status_code in (200, 201), r.text
    return tid


@smk("SMK-MCP-01")
async def test_mount_stdio_lists_tools(authed_client, mcp_stdio_command, unique_suffix):
    tid = await _mount_stdio(authed_client, mcp_stdio_command, unique_suffix)
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    assert tools.status_code == 200, tools.text
    names = {t["id"] for t in tools.json().get("items", tools.json().get("tools", []))}
    assert {"echo", "bump"} <= names, names
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-02")
async def test_mount_http_lists_tools(authed_client, mcp_http_url, unique_suffix):
    tid = f"mcp-http-{unique_suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "http", "config": {"url": mcp_http_url}}},
    )
    assert r.status_code in (200, 201), r.text
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    assert tools.status_code == 200, tools.text
    names = {t["id"] for t in tools.json().get("items", tools.json().get("tools", []))}
    assert {"echo", "bump"} <= names, names
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-04", "SMK-MCP-03")
async def test_call_mounted_mcp_tool_end_to_end(
    authed_client, mock_llm, mcp_stdio_command, unique_suffix, tmp_path
):
    tid = await _mount_stdio(authed_client, mcp_stdio_command, unique_suffix)
    registry, base_url = mock_llm
    sc = f"scripted:mcp04-{unique_suffix}"
    marker = str(tmp_path / "mcp_marker.txt")
    # MCP-03: scope the agent to exactly one tool from the mounted server.
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=[f"{tid}__bump"],
        rules=[
            Rule(when_tool_offered="bump", when_tool_result=False,
                 emit_tool=f"{tid}__bump", emit_args={"marker_path": marker}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    final = await wait_terminal(authed_client, sid)
    assert final.get("status") == "ended", final
    # the external MCP server actually received the call: its marker file exists
    import os
    assert os.path.exists(marker), "MCP bump tool did not run"
    assert open(marker).read().strip() == "1"
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-05")
async def test_mounted_server_failure_isolated(authed_client, unique_suffix):
    tid = f"mcp-bad-{unique_suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "stdio",
                         "config": {"command": [f"nonexistent-xyz-{unique_suffix}"]}}},
    )
    assert r.status_code in (200, 201), r.text
    tools = await authed_client.get(f"/v1/toolsets/{tid}/tools")
    # a broken server yields a clean envelope, never /errors/internal, and the
    # platform stays healthy
    if tools.status_code >= 400:
        assert tools.json()["type"] != "/errors/internal", tools.text
    health = await authed_client.get("/v1/health")
    assert health.status_code == 200
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-06", "SMK-MCP-11")
async def test_exposure_enable_and_disable(authed_client):
    enable = await authed_client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": ["misc__uuid_v4", "system__call_tool"]},
    )
    assert enable.status_code in (200, 204), enable.text
    got = await authed_client.get("/v1/mcp_exposure")
    assert got.json()["enabled"] is True
    assert "misc__uuid_v4" in got.json()["allowed_tools"]
    disable = await authed_client.put("/v1/mcp_exposure", json={"enabled": False})
    assert disable.status_code in (200, 204), disable.text
    assert (await authed_client.get("/v1/mcp_exposure")).json()["enabled"] is False


@smk("SMK-MCP-09")
async def test_system_only_floor_rejects_user_toolset(authed_client, mcp_stdio_command, unique_suffix):
    tid = await _mount_stdio(authed_client, mcp_stdio_command, unique_suffix)
    # a tool from a user-defined toolset must not be allowlistable for exposure
    r = await authed_client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": [f"{tid}__echo"]},
    )
    assert r.status_code == 422, r.text
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-MCP-07", "SMK-MCP-08", "SMK-MCP-10", status="partial")
@requires("mcp:client")
async def test_external_mcp_client_roundtrip():
    pytest.skip("needs an external MCP client speaking to /v1/mcp (testconfig.mcp.client_bearer_token)")
