"""SMK cross-cutting end-to-end journeys (docs/tests/15-cross-cutting-journeys).

A feature is verified through a real consumer and the downstream effect is
observed, not by inspecting endpoints. These are the hermetic journeys:
scripted mock LLM + the in-repo stdio/http MCP fixture servers (whose ``bump``
tool writes a marker file), so the "the remote server actually received the
call" assertion is concrete with no external dependency.
"""
from __future__ import annotations

import os

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_graph,
    make_local_workspace,
    make_scripted_agent,
    start_graph_session,
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


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
