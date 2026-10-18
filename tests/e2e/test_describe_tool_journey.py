"""E2E: ``system__describe_tool`` through a real agent session (pivot P1, task 6).

An agent that carries only ``describe_tool`` is driven by the scripted mock LLM to describe two tools in one turn, and the tool results the model is handed
(read back from the mock's captured requests) are the contract:

* a built-in toolset's tool, with a policy an operator just created on it: the answer names the policy (the LIVE gate, read from the resolver the gates use,
  after the operator's ``/invalidate``) and says to send the call through ``call_tool``;
* a session file tool: a direct call by scoped id, because ``call_tool`` cannot reach the tools of a session.

Unit coverage: ``tests/toolset/test_describe_tool.py`` and ``tests/toolset/test_tool_catalog.py``. This journey pins the wiring only: the tool is registered on
the live server, a session dispatches it, and the document reaches the model.
"""
from __future__ import annotations

import json

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_completed,
)

pytestmark = pytest.mark.asyncio


async def test_describe_tool_reports_the_live_gate_and_the_right_door(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    sc = f"scripted:describe-{unique_suffix}"
    policy_id = f"tap-describe-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=["system__describe_tool"],
        rules=[
            Rule(when_tool_offered="describe_tool", when_tool_result=False,
                 emit_tool="system__describe_tool", emit_args={"toolset_id": "misc", "tool_name": "uuid_v4"}),
            # the second call is chosen by what the first came back with
            Rule(when_last_tool_result_contains="misc__uuid_v4",
                 emit_tool="system__describe_tool", emit_args={"toolset_id": "workspace", "tool_name": "read"}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)

    made = await authed_client.post(
        "/v1/tool_approval_policies",
        json={"id": policy_id, "toolset_id": "misc", "tool_name": "uuid_v4", "enabled": True, "approval": {"type": "required"}},
    )
    assert made.status_code == 201, made.text
    try:
        # The resolver caches verdicts in-process; invalidate so the new policy is the one the running server answers with.
        invalidated = await authed_client.post("/v1/tool_approval_policies/invalidate")
        assert invalidated.status_code == 202, invalidated.text
        sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
        assert (await wait_completed(authed_client, sid)).get("session_state") == "parked"
    finally:
        await authed_client.delete(f"/v1/tool_approval_policies/{policy_id}")
        await authed_client.post("/v1/tool_approval_policies/invalidate")

    # The last request carries the whole history: both tool results, in order.
    last = [r for r in registry.requests if r.get("model") == sc][-1]
    results = [json.loads(m["content"]) for m in last.get("messages") or [] if m.get("role") == "tool"]
    assert len(results) == 2, results
    gated, direct = results

    assert gated["scoped_id"] == "misc__uuid_v4" and gated["call"]["via"] == "call_tool"
    assert gated["call"]["example"]["toolset_id"] == "misc" and gated["call"]["example"]["tool_name"] == "uuid_v4"
    assert gated["approval"] == {"gated": True, "type": "required", "policy_id": policy_id}, gated["approval"]
    assert gated["reference"] == "reference/tools/misc/uuid_v4"

    assert direct["scoped_id"] == "workspace__read" and direct["required_role"] == "user" and direct["needs_workspace"] is True
    assert direct["call"] == {"via": "direct", "example": {"name": "workspace__read", "arguments": {"path": "README.md"}}}
    assert set(direct) == set(gated), "both answers have the same keys"
