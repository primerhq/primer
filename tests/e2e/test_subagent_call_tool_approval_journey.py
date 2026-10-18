"""E2E: an approved ``call_tool`` gate inside a SUBAGENT completes the delegation (pivot P2's agent kit).

A parent agent delegates with ``system__invoke_agent``; the subagent asks ``system__call_tool`` for a tool whose approval policy is
``required``. The real handler parks the INNER call, inside the subagent, so the parent's session parks with the subagent's frame. An
operator approves it through ``/tool_approval/respond``, and the continuation walk (``apply_leaf``) re-runs the inner tool as the subagent
of this session: the subagent's next model request holds the inner tool's real output under its own ``call_tool`` call's id, and the
parent's next one holds the subagent's answer.

Before the fix ``apply_leaf`` sent the park's bare inner tool name to the subagent's manager, which could not route it: the inner tool never
ran and the parent's call was answered with an error (under the subagent's call id, which the parent's transcript does not have).

Two scenarios, one per agent, because the scripted mock keys its rules on the request model name. The gated tool is
``system::list_toolset_tools`` (role user, read-only), which no other journey uses; its policy is removed in the ``finally``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session, wait_completed


async def _clear_policies_for(client: httpx.AsyncClient, toolset_id: str, tool_name: str) -> None:
    """Remove any policy a previous run left on the pair, so this run gates it exactly once, then drop the resolver's cache."""
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for item in existing.json().get("items", []):
            if item.get("toolset_id") == toolset_id and item.get("tool_name") == tool_name:
                await client.delete(f"/v1/tool_approval_policies/{item['id']}")
    await client.post("/v1/tool_approval_policies/invalidate")


@pytest.mark.asyncio
async def test_an_approved_call_tool_gate_inside_a_subagent_runs_the_inner_tool_and_completes_the_delegation(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    registry, base_url = mock_llm
    sub_done = f"SUB-RAN-{unique_suffix}"
    parent_done = f"PARENT-DONE-{unique_suffix}"
    parent_failed = f"PARENT-SAW-A-FAILED-DELEGATION-{unique_suffix}"
    inner_arguments = {"toolset_id": "misc"}

    await _clear_policies_for(client, "system", "list_toolset_tools")
    policy_id = f"pol-sub-call-tool-{unique_suffix}"
    created = await client.post("/v1/tool_approval_policies", json={
        "id": policy_id, "toolset_id": "system", "tool_name": "list_toolset_tools",
        "enabled": True, "approval": {"type": "required"},
    })
    assert created.status_code in (200, 201), created.text
    invalidated = await client.post("/v1/tool_approval_policies/invalidate")
    assert invalidated.status_code == 202, invalidated.text

    try:
        # The subagent: ask call_tool for the gated tool; report only if its REAL output (the misc toolset's tools) came back.
        sub_scenario = f"scripted:sub-call-tool-{unique_suffix}"
        sub = await make_scripted_agent(
            client, registry, base_url, suffix=f"{unique_suffix}-sub", scenario=sub_scenario, tools=["system__call_tool"],
            rules=[
                Rule(when_last_tool_result_contains="uuid_v4", emit_text=sub_done),
                Rule(when_tool_result=True, emit_text=f"SUB-GOT-AN-ERROR-{unique_suffix}"),
                Rule(
                    emit_tool="system__call_tool",
                    emit_args={"toolset_id": "system", "tool_name": "list_toolset_tools", "arguments": inner_arguments},
                    emit_tool_call_id="call_sub_ct",
                ),
            ],
        )
        # The parent: delegate to the subagent; finish only if the subagent's answer came back.
        parent_scenario = f"scripted:parent-invoke-{unique_suffix}"
        parent = await make_scripted_agent(
            client, registry, base_url, suffix=f"{unique_suffix}-par", scenario=parent_scenario, tools=["system__invoke_agent"],
            rules=[
                Rule(when_last_tool_result_contains=sub_done, emit_text=parent_done),
                Rule(when_tool_result=True, emit_text=parent_failed),
                Rule(
                    emit_tool="system__invoke_agent",
                    emit_args={"agent_id": sub["agent_id"], "prompt": "List the tools of the misc toolset."},
                    emit_tool_call_id="call_par_invoke",
                ),
            ],
        )
        wid = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
        sid = await start_agent_session(client, workspace_id=wid, agent_id=parent["agent_id"])

        # ----- The gate the subagent's call_tool raised parks the PARENT's session; approve it -----
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60.0
        pending: dict = {}
        while loop.time() < deadline:
            r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
            if r.status_code == 200:
                pending = r.json()
                break
            await asyncio.sleep(0.5)
        assert pending, f"session {sid} never parked on the subagent's call_tool gate"
        assert "list_toolset_tools" in pending["tool_name"], ("the card is not the inner call's", pending)
        respond = await client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={"tool_call_id": pending["tool_call_id"], "gate_id": pending.get("gate_id"), "decision": "approved"},
        )
        assert respond.status_code == 202, respond.text

        # ----- The delegation completes: the parent reports the subagent's answer -----
        transcript = ""
        deadline = loop.time() + 90.0
        while loop.time() < deadline:
            log = await client.get(f"/v1/sessions/{sid}/messages", params={"limit": 500})
            if log.status_code == 200:
                transcript = json.dumps(log.json())
                if parent_done in transcript or parent_failed in transcript:
                    break
            await asyncio.sleep(0.5)
        assert parent_failed not in transcript, "the parent's invoke_agent call came back as a failure"
        assert parent_done in transcript, f"the delegation never completed for session {sid}"
        await wait_completed(client, sid)

        # ----- The subagent got the inner tool's REAL output, as the result of ITS call_tool call -----
        sub_messages = [m for req in registry.requests if req.get("model") == sub_scenario for m in req.get("messages") or []]
        results = [m for m in sub_messages if m.get("role") == "tool"]
        assert results, "the subagent was never handed its call_tool result"
        assert results[-1].get("tool_call_id") == "call_sub_ct", results[-1]
        assert "uuid_v4" in str(results[-1].get("content")), results[-1]
    finally:
        await client.delete(f"/v1/tool_approval_policies/{policy_id}")
        await client.post("/v1/tool_approval_policies/invalidate")
