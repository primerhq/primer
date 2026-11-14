"""W6: the real operator plans, creates a workspace and launches its sessions.

Only model answers are scripted. Wiki reads, meta-dispatch, the live gate
resolver, workspace materialization, claiming, ask_user park/resume and
bookkeeper reads use the running server. The fixture provisions existing
worker agents; it never disables approval policies or injects session state.
CI owns execution, serially with the rest of the live-server suite.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.default_workspace import default_workspace_id
from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for


class _AfterTool(Rule):
    """Select the next scripted answer by the actual preceding call id."""

    def __init__(self, after: str, **kwargs):
        super().__init__(**kwargs)
        self.after = after

    def matches(self, request: dict) -> bool:
        last = next((m for m in reversed(request.get("messages", [])) if m.get("role") == "tool"), {})
        return super().matches(request) and last.get("tool_call_id") == self.after


def _results(registry, model: str) -> dict:
    out = {}
    for request in registry.requests:
        if request.get("model") != model:
            continue
        for message in request.get("messages", []):
            if message.get("role") == "tool":
                content = message.get("content", "")
                try:
                    out[message["tool_call_id"]] = json.loads(content)
                except json.JSONDecodeError:
                    out[message["tool_call_id"]] = content
    return out


async def _wait_result(registry, model: str, call_id: str):
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        results = _results(registry, model)
        if call_id in results:
            return results[call_id]
        await asyncio.sleep(0.25)
    pytest.fail(f"No actual result for {model}/{call_id}: {_results(registry, model)}")


async def _wait_text(client: httpx.AsyncClient, sid: str, marker: str) -> None:
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/sessions/{sid}/messages", params={"limit": 500})
        assert response.status_code == 200, response.text
        if marker in json.dumps(response.json()):
            return
        await asyncio.sleep(0.25)
    state = await client.get(f"/v1/sessions/{sid}")
    pytest.fail(f"{marker} never reached session {sid}: {state.text}")


@pytest.mark.asyncio
async def test_operator_moderates_then_rereads_bookkeeper_state(client: httpx.AsyncClient, mock_llm, unique_suffix: str):
    registry, mock_url = mock_llm
    target = f"w6-request-{unique_suffix}"
    provider = f"w6-llm-{unique_suffix}"
    models = {role: f"w6-{role}-{unique_suffix}" for role in ("operator", "planner", "parked", "running")}
    profiles = {role: profile_id_for(provider, model) for role, model in models.items()}
    parked_agent, running_agent = f"w6-parked-{unique_suffix}", f"w6-running-{unique_suffix}"
    moderate = f"MODERATE-{unique_suffix}"
    survey = f"BOOKKEEP-{unique_suffix}"
    moderated, surveyed, resumed = (f"{name}-{unique_suffix}" for name in ("MODERATED", "SURVEYED", "RESUMED"))
    plan = f"PLAN-{unique_suffix}: 1. local-default; 2. {parked_agent}; 3. {running_agent}"

    def step(after, call_id, toolset, tool, arguments, request=moderate):
        return _AfterTool(
            after, when_last_user_contains=request, emit_tool="system__call_tool",
            emit_args={"toolset_id": toolset, "tool_name": tool, "arguments": arguments},
            emit_tool_call_id=call_id,
        )

    registry.register(models["operator"], [
        _AfterTool("w6_running_read", when_last_user_contains=survey, emit_text=surveyed),
        step("w6_parked_read", "w6_running_read", "workspaces", "find_sessions", {"state": "running", "limit": 20}, survey),
        step("w6_workspaces", "w6_parked_read", "workspaces", "find_sessions", {"state": "parked", "limit": 20}, survey),
        step("w6_status_wiki", "w6_workspaces", "workspaces", "list_workspaces", {"limit": 200, "order_by": ["created_at:desc"]}, survey),
        Rule(when_last_user_contains=survey, emit_tool="system__wiki_read",
             emit_args={"path": "runbooks/workspace-status"}, emit_tool_call_id="w6_status_wiki"),
        _AfterTool("w6_launched", when_last_user_contains=moderate, emit_text=moderated),
        step("w6_running_session", "w6_launched", "workspaces", "list_workspace_sessions", {"workspace_id": target, "limit": 20}),
        step("w6_parked_session", "w6_running_session", "workspaces", "create_workspace_session", {
            "workspace_id": target, "binding": {"kind": "agent", "agent_id": running_agent},
            "initial_instructions": "Work until cancelled", "autonomous": True, "auto_start": True,
        }),
        step("w6_workspace", "w6_parked_session", "workspaces", "create_workspace_session", {
            "workspace_id": target, "binding": {"kind": "agent", "agent_id": parked_agent},
            "initial_instructions": "Ask for the user's decision", "autonomous": True, "auto_start": True,
        }),
        step("w6_templates", "w6_workspace", "workspaces", "create_workspace", {"id": target, "template_id": "local-default"}),
        step("w6_gate", "w6_templates", "workspaces", "list_workspace_templates", {"limit": 200}),
        step("w6_plan", "w6_gate", "system", "describe_tool", {"toolset_id": "workspaces", "tool_name": "create_workspace"}),
        step("w6_moderator_wiki", "w6_plan", "system", "invoke_agent", {
            "agent_id": "planner", "prompt": f"{moderate}: plan a separate workspace using existing workers. Context: local-default; {parked_agent}; {running_agent}.",
        }),
        Rule(when_last_user_contains=moderate, emit_tool="system__wiki_read",
             emit_args={"path": "runbooks/moderate-a-request"}, emit_tool_call_id="w6_moderator_wiki"),
    ])
    registry.register(models["planner"], [
        _AfterTool("w6_planner_ground", emit_text=plan),
        Rule(emit_tool="system__wiki_read", emit_args={"path": "purpose-map"}, emit_tool_call_id="w6_planner_ground"),
    ])
    registry.register(models["parked"], [
        Rule(when_tool_result=True, emit_text=resumed),
        Rule(emit_tool="system__ask_user", emit_args={"prompt": f"Approve the next step? {unique_suffix}"}, emit_tool_call_id="w6_question"),
    ])
    registry.register(models["running"], [Rule(emit_text="working " * 160, text_chunk_words=1, chunk_delay_s=0.5)])

    response = await client.post("/v1/llm_providers", json={
        "id": provider, "provider": "openchat", "config": {"url": mock_url, "flavor": "lmstudio"},
        "limits": {"max_concurrency": 4},
    })
    assert response.status_code in (200, 201), response.text
    for role, model in models.items():
        response = await client.post("/v1/model_profiles", json={
            "id": profiles[role], "description": "W6 scripted profile", "provider_id": provider,
            "model_name": model, "context_length": 32000,
        })
        assert response.status_code in (200, 201), response.text
    seeded = await client.post("/v1/setup/seed")
    assert seeded.status_code == 200, seeded.text
    origin = await default_workspace_id(client)
    assert origin != target
    template = await client.get("/v1/workspace_templates/local-default")
    assert template.status_code == 200, template.text
    originals = {}
    operator_sid = None
    worker_rows = {}
    try:
        for agent_id in ("operator", "planner"):
            response = await client.get(f"/v1/agents/{agent_id}")
            assert response.status_code == 200, response.text
            originals[agent_id] = response.json()
        operator_prompt = "\n".join(originals["operator"]["system_prompt"])
        assert "runbooks/moderate-a-request" in operator_prompt
        assert "runbooks/workspace-status" in operator_prompt
        for agent_id, row in originals.items():
            response = await client.put(f"/v1/agents/{agent_id}", json={**row, "model": {"profile_id": profiles[agent_id]}})
            assert response.status_code == 200, response.text
        for agent_id, role in ((parked_agent, "parked"), (running_agent, "running")):
            response = await client.post("/v1/agents", json={
                "id": agent_id, "description": "Existing worker in the moderator's plan",
                "model": {"profile_id": profiles[role]}, "system_prompt": ["Follow this session's request."],
                "tools": originals["planner"]["tools"],
            })
            assert response.status_code == 201, response.text
        response = await client.post(f"/v1/workspaces/{origin}/sessions", json={"binding": {"kind": "agent", "agent_id": "operator"}})
        assert response.status_code in (200, 201), response.text
        operator_sid = response.json()["id"]
        response = await client.post(f"/v1/workspaces/{origin}/sessions/{operator_sid}/steer", json={"instruction": moderate})
        assert response.status_code in (200, 201, 202), response.text
        observed = await _wait_result(registry, models["operator"], "w6_gate")
        assert observed["scoped_id"] == "workspaces__create_workspace"
        assert observed["approval"] == {"gated": False}, observed  # observed live state, not a presumed approval park
        await _wait_text(client, operator_sid, moderated)
        results = _results(registry, models["operator"])
        assert plan in json.dumps(results["w6_plan"])
        assert results["w6_workspace"]["id"] == target
        assert {item["binding"]["agent_id"] for item in results["w6_launched"]["items"]} == {parked_agent, running_agent}

        # Reread persisted rows independently of the operator's context.
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get("/v1/sessions", params={"workspace_id": target, "limit": 20})
            assert response.status_code == 200, response.text
            worker_rows = {item["binding"]["agent_id"]: item for item in response.json()["items"]}
            if (worker_rows.get(parked_agent, {}).get("parked_event_key", "") or "").startswith("ask_user:") and worker_rows.get(running_agent, {}).get("session_state") == "running":
                break
            await asyncio.sleep(0.25)
        assert worker_rows[parked_agent]["session_state"] == "parked", worker_rows
        assert worker_rows[parked_agent]["parked_event_key"].startswith("ask_user:"), worker_rows
        assert worker_rows[running_agent]["session_state"] == "running", worker_rows
        response = await client.post(f"/v1/workspaces/{origin}/sessions/{operator_sid}/steer", json={"instruction": survey})
        assert response.status_code in (200, 201, 202), response.text
        await _wait_text(client, operator_sid, surveyed)
        results = _results(registry, models["operator"])
        assert target in {item["id"] for item in results["w6_workspaces"]["items"]}
        for role, call_id in ((parked_agent, "w6_parked_read"), (running_agent, "w6_running_read")):
            found = next(item for item in results[call_id]["items"] if item["id"] == worker_rows[role]["id"])
            assert found["workspace_id"] == target
            assert found["session_state"] == worker_rows[role]["session_state"]
            if role == parked_agent:
                assert found["parked_event_key"] == worker_rows[role]["parked_event_key"]

        parked_sid = worker_rows[parked_agent]["id"]
        pending = await client.get(f"/v1/sessions/{parked_sid}/ask_user/pending")
        assert pending.status_code == 200, pending.text
        response = await client.post(f"/v1/sessions/{parked_sid}/ask_user/respond", json={
            "tool_call_id": pending.json()["tool_call_id"], "response": "Continue",
        })
        assert response.status_code == 202, response.text
        await _wait_text(client, parked_sid, resumed)
        state = await client.get(f"/v1/sessions/{parked_sid}")
        assert state.status_code == 200, state.text
        assert state.json().get("parked_event_key") is None, state.text
    finally:
        if operator_sid:
            await client.post(f"/v1/workspaces/{origin}/sessions/{operator_sid}/cancel")
        rows = await client.get("/v1/sessions", params={"workspace_id": target, "limit": 200})
        if rows.status_code == 200:
            for row in rows.json()["items"]:
                await client.post(f"/v1/workspaces/{target}/sessions/{row['id']}/cancel")
        await client.delete(f"/v1/workspaces/{target}")
        for agent_id, row in originals.items():
            restored = await client.put(f"/v1/agents/{agent_id}", json=row)
            assert restored.status_code == 200, restored.text
        for agent_id in (parked_agent, running_agent):
            await client.delete(f"/v1/agents/{agent_id}")
