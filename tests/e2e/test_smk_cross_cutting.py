"""SMK cross-cutting end-to-end journeys (docs/tests/15-cross-cutting-journeys).

A feature is verified through a real consumer and the downstream effect is
observed, not by inspecting endpoints. These are the hermetic journeys:
scripted mock LLM + the in-repo stdio/http MCP fixture servers (whose ``bump``
tool writes a marker file), so the "the remote server actually received the
call" assertion is concrete with no external dependency.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_graph,
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    start_graph_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.yield_journeys import wait_for_resume

pytestmark = pytest.mark.asyncio


async def _mount_stdio_mcp(authed_client, command, suffix) -> str:
    tid = f"mcp-stdio-x01-{suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "stdio", "config": {"command": command}}},
    )
    assert r.status_code in (200, 201), r.text
    return tid


async def _mount_http_mcp(authed_client, url, suffix) -> str:
    tid = f"mcp-http-x-{suffix}"
    r = await authed_client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "mcp",
              "config": {"transport": "http", "config": {"url": url}}},
    )
    assert r.status_code in (200, 201), r.text
    return tid


@smk("SMK-X-02")
async def test_http_mcp_tool_driven_from_inside_a_graph(
    authed_client, mock_llm, mcp_http_url, unique_suffix, tmp_path
):
    """An agent node inside a graph calls an HTTP MCP tool; the remote server
    receives the call (marker file written) and the tool's effect flows
    through to the graph's end-node output."""
    registry, base_url = mock_llm
    tid = await _mount_http_mcp(authed_client, mcp_http_url, unique_suffix)
    sc = f"scripted:x02-{unique_suffix}"
    marker = str(tmp_path / "x02_marker.txt")
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=[f"{tid}__bump"],
        rules=[
            Rule(when_tool_offered="bump", when_tool_result=False,
                 emit_tool=f"{tid}__bump", emit_args={"marker_path": marker}),
            Rule(when_tool_result=True, emit_text="bumped"),
        ],
    )
    nodes = [
        {"kind": "begin", "id": "start"},
        {"kind": "agent", "id": "step", "agent_id": agent["agent_id"],
         "input_template": "call the bump tool"},
        {"kind": "end", "id": "done", "output_template": "{{ nodes.step.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "step"},
        {"kind": "static", "from_node": "step", "to_node": "done"},
    ]
    gid = await make_graph(authed_client, suffix=unique_suffix, nodes=nodes, edges=edges)
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_graph_session(authed_client, workspace_id=wid, graph_id=gid)
    final = await wait_terminal(authed_client, sid, timeout_s=90)
    assert final.get("status") == "ended", final
    # The HTTP MCP server actually received + executed the call.
    assert os.path.exists(marker), "HTTP MCP bump tool did not run from the graph"
    assert open(marker).read().strip() == "1"
    # The run produced a turn log for the graph.
    tl = await authed_client.get(f"/v1/graphs/{gid}/runs/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    await authed_client.delete(f"/v1/toolsets/{tid}")


@smk("SMK-X-01")
async def test_stdio_mcp_approval_park_resume(
    authed_client, mock_llm, mcp_stdio_command, unique_suffix, tmp_path
):
    """A stdio MCP tool gated by a required-approval policy: the agent calls
    ``bump``, the session PARKS at the approval gate (tool has not run), the
    operator approves via REST, the session RESUMES, and the real stdio MCP
    ``bump`` subprocess actually executes (writes its marker file) before the
    session ends. Proves the full park -> approve -> resume -> MCP-dispatch
    chain for a real external tool."""
    registry, base_url = mock_llm
    tid = await _mount_stdio_mcp(authed_client, mcp_stdio_command, unique_suffix)
    marker = str(tmp_path / "x01_marker.txt")

    # ----- Gate the mounted toolset's bump tool with a required policy -----
    pol = f"pol-x01-{unique_suffix}"
    # Policies are unique on (toolset_id, tool_name); clear any leftover pair.
    existing = await authed_client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if it.get("toolset_id") == tid and it.get("tool_name") == "bump":
                await authed_client.delete(
                    f"/v1/tool_approval_policies/{it['id']}")
    r = await authed_client.post(
        "/v1/tool_approval_policies",
        json={
            "id": pol,
            "toolset_id": tid,
            "tool_name": "bump",
            "enabled": True,
            "approval": {"type": "required"},
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await authed_client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text

    # ----- Scripted agent: offered bump -> call it; on result -> terminate ---
    sc = f"scripted:x01-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=[f"{tid}__bump"],
        rules=[
            Rule(when_tool_offered="bump", when_tool_result=False,
                 emit_tool=f"{tid}__bump", emit_args={"marker_path": marker}),
            Rule(when_tool_result=True, emit_text="bumped"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"])

    try:
        # ----- Drive until the session PARKS on the approval gate -----
        deadline = asyncio.get_event_loop().time() + 30.0
        parked: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            r = await authed_client.get(f"/v1/sessions/{sid}")
            if r.status_code == 200:
                parked = r.json()
                if parked.get("parked_status") == "parked":
                    break
                if parked.get("status") == "ended":
                    raise AssertionError(
                        f"session {sid} ended before parking on approval: "
                        f"reason={parked.get('ended_reason')!r} body={parked!r}")
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(
                f"session {sid} never parked on approval within 30s; "
                f"last_body={parked!r}")
        initial_turn_no = parked["turn_no"]

        # The MCP tool is gated: it must NOT have run yet.
        assert not os.path.exists(marker), (
            "stdio MCP bump ran before approval (gate did not hold)")

        # ----- Read the pending approval and approve it -----
        r = await authed_client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        tool_call_id = r.json()["tool_call_id"]

        r = await authed_client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={"tool_call_id": tool_call_id, "decision": "approved"},
        )
        assert r.status_code == 202, r.text

        # ----- Resume clears the park and advances the turn -----
        await wait_for_resume(
            authed_client, sid, min_turn_no=initial_turn_no + 1, timeout_s=90.0)

        # ----- Session reaches terminal -----
        final = await wait_terminal(authed_client, sid, timeout_s=90)
        assert final.get("status") == "ended", final

        # ----- The real stdio MCP bump tool executed after approval -----
        assert os.path.exists(marker), (
            "stdio MCP bump tool did not run after approval+resume")
        assert open(marker).read().strip() == "1"
    finally:
        try:
            await authed_client.delete(f"/v1/tool_approval_policies/{pol}")
        except Exception:  # noqa: BLE001
            pass
        await authed_client.delete(f"/v1/toolsets/{tid}")
