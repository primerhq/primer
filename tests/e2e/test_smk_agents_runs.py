"""SMK agent run-based tests (Phase 1), driven by the scripted mock LLM.

Covers AGT-02, AGT-03, AGT-06, AGT-08. The reply text lives in the session's
messages.jsonl (read over the session WS); these tests assert on the turn-log
telemetry and on observable tool side effects, which are the simple, stable
surfaces that prove the scripted run executed.
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

pytestmark = pytest.mark.asyncio


@smk("SMK-AGT-02")
async def test_single_turn_run_on_workspace(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix,
        scenario=f"scripted:agt02-{unique_suffix}",
        rules=[Rule(emit_text="ALL DONE")],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"],
        instructions="say the magic words",
    )
    final = await wait_terminal(authed_client, sid)
    assert final.get("status") == "ended", final
    # the turn ran to completion via the scripted LLM
    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    items = tl.json()["items"]
    assert items, tl.text
    assert any(i.get("finish_reason") == "stop" for i in items), items


@smk("SMK-AGT-03")
async def test_tool_dispatch_within_a_turn(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    scenario = f"scripted:agt03-{unique_suffix}"
    # rule 1: when the write tool is offered, call it; rule 2: after the tool
    # result comes back, finish. The written file is the observable proof the
    # agent actually dispatched the tool.
    # No explicit tools: workspace file tools (read/write/...) are implicitly
    # available to any workspace-bound agent. Listing "workspace__write" in
    # agent.tools is wrong (that path does a toolset lookup and 404s).
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        rules=[
            Rule(
                when_tool_offered="write",
                when_tool_result=False,
                emit_tool="workspace__write",
                emit_args={"path": "out.txt", "content": "HELLO-FROM-TOOL"},
            ),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"],
        instructions="write the file",
    )
    final = await wait_terminal(authed_client, sid)
    assert final.get("status") == "ended", final
    # the tool actually ran: the file it wrote exists with the expected content
    read = await authed_client.get(
        f"/v1/workspaces/{wid}/files/read",
        params={"path": "out.txt", "encoding": "text"},
    )
    assert read.status_code == 200, read.text
    assert "HELLO-FROM-TOOL" in read.json()["content"]


@smk("SMK-AGT-06")
async def test_session_turn_log_endpoint(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix,
        scenario=f"scripted:agt06-{unique_suffix}", rules=[Rule(emit_text="ok")],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    await wait_terminal(authed_client, sid)
    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    body = tl.json()
    assert body["total"] >= 1
    assert body["items"], body


@smk("SMK-AGT-08")
async def test_structured_output_response_format(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    # The scripted model returns a JSON object; assert the run completes cleanly
    # with a stop finish (the structured-output shape itself is exercised by the
    # graph response_format path in the graph SMK tests).
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix,
        scenario=f"scripted:agt08-{unique_suffix}",
        rules=[Rule(emit_text='{"answer": "42"}')],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    final = await wait_terminal(authed_client, sid)
    assert final.get("status") == "ended", final
    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert any(i.get("finish_reason") == "stop" for i in tl.json()["items"])
