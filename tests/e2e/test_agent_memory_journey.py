"""Journey: an agent's memory learns a playbook entry from its own session, the owner approves it, and the next session starts with it (agent memory).

A real server over HTTP and the real OpenChatLLM client against a scripted mock. The mock answers the reflection's structured call (the one request
whose last user message carries the reflection input's ``# Transcript`` section) with one ADD, and the session turns with a tool call and plain text.

1. Memory is turned on for the agent with ``promotion: approve`` and ``idle_minutes: 1``.
2. A two-turn session with a tool call runs, and is cancelled (a session that ends reflects at once; one left alone reflects a minute after its last turn).
3. ``memory.reflection.completed`` lands; the entry waits in ``candidates`` for the owner; the owner approves it.
4. A new session's system prompt (the first message of its model call, as the mock received it) holds the index line and the instructions.
5. ``GET /v1/collections`` does not list the agent's ``mem-`` collection.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session

_ADD = {
    "op": "add",
    "name": "Retry on 429",
    "description": "when a call answers 429, wait before retrying",
    "steps": ["Read the retry-after header", "Wait that long, then retry once"],
    "pitfalls": ["Retrying at once keeps the limit tripped"],
    "example": None,
    "topics": [],
}
_REFLECTION = {"run_succeeded": True, "deltas": [_ADD]}
_ENTRY = "retry-on-429"


async def _wait_for_log(client: httpx.AsyncClient, session_id: str, text: str, *, timeout_s: float = 15.0) -> None:
    """Poll the session's log until an assistant answer holds ``text``."""
    for _ in range(int(timeout_s / 0.5)):
        items = (await client.get(f"/v1/sessions/{session_id}/messages", params={"limit": 500})).json()["items"]
        if any(text in str(r.get("payload", {}).get("text", "")) for r in items if r.get("kind") == "assistant_token"):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"session {session_id} never answered {text!r}")


async def _wait_for_event(
    client: httpx.AsyncClient, event_type: str, agent_id: str, *, session_id: str, timeout_s: float = 120.0,
) -> dict:
    """Poll for an agent's event. The whole journey must fit pytest's 180 s per test, so a timeout fails with every memory.* event of the agent
    (a skipped reflection says why) and the session's row (its turn counters)."""
    for _ in range(int(timeout_s / 2)):
        resp = await client.get("/v1/events", params={"event_type": event_type, "entity_id": agent_id, "limit": 50})
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        if items:
            return items[-1]
        await asyncio.sleep(2)
    seen = (await client.get("/v1/events", params={"entity_id": agent_id, "limit": 200})).json().get("items", [])
    memory = [(e.get("event_type"), e.get("payload")) for e in seen if str(e.get("event_type", "")).startswith("memory.")]
    row = await client.get(f"/v1/sessions/{session_id}")
    raise AssertionError(
        f"no {event_type} event for agent {agent_id} within {timeout_s}s; memory.* events: {memory}; "
        f"session {row.status_code}: {row.text[:600]}"
    )


async def _activity_over_mcp(client, base_url, agent_id, suffix):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    previous = await client.get("/v1/mcp_exposure")
    assert previous.status_code == 200, previous.text
    token = None
    try:
        exposed = await client.put("/v1/mcp_exposure", json={
            "enabled": True, "mode": "allowlist", "allowed_tools": ["agent_memory__memory_activity"]})
        assert exposed.status_code in (200, 204), exposed.text
        issued = await client.post("/v1/auth/tokens", json={"name": f"memory-{suffix}", "scopes": ["mcp"]})
        assert issued.status_code == 201, issued.text
        token = issued.json()
        async with create_mcp_http_client(headers={"Authorization": "Bearer " + token["plaintext"]}) as http:
            async with streamable_http_client(f"{base_url}/v1/mcp/", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("call_tool", arguments={
                        "scoped_id": "agent_memory__memory_activity", "arguments": {"agent_id": agent_id}})
                    envelope = json.loads("".join(getattr(p, "text", "") for p in result.content))
                    assert not result.is_error and envelope["ok"] is True, envelope
                    return json.loads(envelope["output"])
    finally:
        try:
            if token is not None:
                removed = await client.delete("/v1/auth/tokens/" + token["id"])
                assert removed.status_code in (200, 204), removed.text
        finally:
            restored = await client.put("/v1/mcp_exposure", json={
                key: previous.json()[key] for key in ("enabled", "mode", "allowed_tools")})
            assert restored.status_code in (200, 204), restored.text


class _EvidenceRule(Rule):
    """A real HTTP mock reply derived from received source IDs, never a blind ADD."""
    def matches(self, req):
        if not super().matches(req):
            return False
        user = next(m["content"] for m in reversed(req["messages"]) if m["role"] == "user")
        rows = [json.loads(line) for line in user.splitlines()[1:]]
        facts = [{"observation": "EARLY_RETRY_CORRECTION: wait before retrying", "sources": [r["source"]]}
                 for r in rows if "EARLY_RETRY_CORRECTION" in r["text"]]
        self.emit_text = json.dumps({"facts": facts})
        return True


class _ReflectedRule(Rule):
    def matches(self, req):
        if not super().matches(req):
            return False
        user = next(m["content"] for m in reversed(req["messages"]) if m["role"] == "user")
        assert "lossy evidence summary" in user and "EARLY_RETRY_CORRECTION: wait before retrying" in user
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("compact_input", [False, True], ids=["direct", "compacted"])
async def test_memory_learns_from_a_session_and_the_next_session_starts_with_it(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path, compact_input: bool, base_url,
) -> None:
    registry, llm_url = mock_llm
    scenario = f"scripted:memory-journey-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, llm_url, suffix=unique_suffix, scenario=scenario, tools=["misc__uuid_v4"],
        rules=[
            # the reflection's structured call: its input is the only user message with a "# Transcript" section
            (_ReflectedRule if compact_input else Rule)(when_last_user_contains="# Transcript", emit_text=json.dumps(_REFLECTION)),
            _EvidenceRule(when_last_user_contains="# Memory evidence to summarize"),
            Rule(when_last_user_contains="Now produce the summary", emit_text="The first task is complete."),
            Rule(when_last_user_contains="first task", when_tool_result=False, emit_tool="misc__uuid_v4"),
            Rule(when_last_user_contains="first task", when_tool_result=True, emit_text="Done with the first task." + (" x" * 70000 if compact_input else "")),
            Rule(when_last_user_contains="second task", emit_text="Done with the second task."),
            Rule(when_last_user_contains="third task", emit_text="Noted for the third task."),
        ],
    )
    agent_id = agent["agent_id"]
    if compact_input:
        from tests._support.model_profiles import profile_id_for
        path = "/v1/model_profiles/" + profile_id_for(agent["provider_id"], scenario)
        profile = await client.get(path)
        assert profile.status_code == 200, profile.text
        updated = await client.put(path, json={**profile.json(), "context_length": 32768})
        assert updated.status_code == 200, updated.text
    workspace_id = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)

    # 1. turn memory on: entries wait for the owner, and a session reflects a minute after its last turn
    put = await client.put(
        f"/v1/agents/{agent_id}/memory/settings", json={"enabled": True, "promotion": "approve", "idle_minutes": 1},
    )
    assert put.status_code == 200, put.text
    assert put.json()["enabled"] is True and put.json()["promotion"] == "approve"

    # 2. two turns with a tool call, then the session ends
    sid = await start_agent_session(client, workspace_id=workspace_id, agent_id=agent_id, instructions="first task: make an id" + (" EARLY_RETRY_CORRECTION" if compact_input else ""))
    await _wait_for_log(client, sid, "Done with the first task.")
    steer = await client.post(f"/v1/workspaces/{workspace_id}/sessions/{sid}/steer", json={"instruction": "second task: say thanks"})
    assert steer.status_code in (200, 201, 202), steer.text
    await _wait_for_log(client, sid, "Done with the second task.")
    await client.post(f"/v1/workspaces/{workspace_id}/sessions/{sid}/cancel")

    # 3. the reflection lands one candidate, which waits for the owner (approve mode)
    done = await _wait_for_event(client, "memory.reflection.completed", agent_id, session_id=sid)
    assert done["payload"]["written"] == 1, done
    assert done["payload"]["input_mode"] == ("compacted" if compact_input else "direct")
    assert done["payload"]["input_estimate"] <= done["payload"]["input_budget"]
    if compact_input:
        assert 1 <= done["payload"]["summary_calls"] <= 4
        original = await client.get(f"/v1/sessions/{sid}/messages", params={"limit": 500})
        assert "EARLY_RETRY_CORRECTION" in original.text
        activity = await client.get(f"/v1/agents/{agent_id}/memory/activity")
        assert activity.status_code == 200
        event = next(e for e in activity.json()["items"] if e["type"] == "memory.reflection.completed")
        assert event["payload"]["input_mode"] == "compacted"
        mcp_activity = await _activity_over_mcp(client, base_url, agent_id, unique_suffix)
        mcp_event = next(e for e in mcp_activity["items"] if e["type"] == "memory.reflection.completed")
        assert mcp_event["payload"] == event["payload"]
    listed = await client.get(f"/v1/agents/{agent_id}/memory/entries", params={"status": "candidates"})
    assert listed.status_code == 200, listed.text
    [candidate] = listed.json()["items"]
    assert candidate["name"] == _ENTRY and candidate["meta"]["needs_approval"] is True, candidate
    approved = await client.post(f"/v1/agents/{agent_id}/memory/entries/{_ENTRY}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "active", approved.text

    # 4. a new session of the agent: its model call's system prompt carries the index line and the instructions
    sid2 = await start_agent_session(client, workspace_id=workspace_id, agent_id=agent_id, instructions="third task: hello")
    await _wait_for_log(client, sid2, "Noted for the third task.", timeout_s=10.0)
    third = [
        r for r in registry.requests
        if r.get("model") == scenario and "third task" in str((next((m for m in reversed(r["messages"]) if m.get("role") == "user"), {})).get("content", ""))
    ]
    assert third, "the mock never saw the new session's call"
    system = str(third[0]["messages"][0]["content"])
    assert f"- {_ENTRY}: {_ADD['description']}" in system, system
    assert f"mem-{agent_id}" in system and "Using your memory" in system, system
    offered = {t.get("function", {}).get("name") for t in third[0].get("tools", [])}
    assert {"collections__search", "collections__read_document"} <= offered, offered

    # 5. the agent's memory collection is in no listing
    colls = await client.get("/v1/collections", params={"limit": 200})
    assert colls.status_code == 200, colls.text
    assert f"mem-{agent_id}" not in [c["id"] for c in colls.json()["items"]]
