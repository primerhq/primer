"""Two sessions in one workspace message each other (pivot P3, plan task 10).

The full pipeline, live: the steered agent A lists its own workspace's
sessions with workspaces__list_my_sessions (its own item is marked
``self: true``), finds agent B's session and sends it a message with
workspaces__message_session. The message wakes idle B as a USER_INPUT that
starts with the ``[from session ... (agent ...)]`` prefix and carries
``payload.peer`` (hops 1); B's reply lands while A's turn is still open, so
it is queued and realized at A's next turn boundary, and the hop count
survives the queue (hops 2). Every delivery emits ``session.messaged``
(envelope ``session_id`` = the target).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for

POLL_S = 0.5


async def _wait_for(
    client: httpx.AsyncClient,
    session_id: str,
    needle: str,
    *,
    timeout_s: float = 120.0,
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        log = await client.get(
            f"/v1/sessions/{session_id}/messages", params={"limit": 500},
        )
        if log.status_code == 200 and needle in json.dumps(log.json()):
            return log.json()
        await asyncio.sleep(POLL_S)
    pytest.fail(f"{needle!r} never reached the transcript of {session_id}")


def _user_inputs(log: dict, needle: str) -> list[dict]:
    return [
        rec["payload"]
        for rec in log.get("items", [])
        if rec.get("kind") == "user_input"
        and needle in str(rec.get("payload", {}).get("text", ""))
    ]


def _tool_outputs(log: dict) -> list:
    """The parsed tool_result payloads of a transcript (unparsed ones stay strings)."""
    out = []
    for rec in log.get("items", []):
        if rec.get("kind") != "tool_result":
            continue
        raw = rec.get("payload", {}).get("output")
        if isinstance(raw, str):
            try:
                out.append(json.loads(raw))
                continue
            except json.JSONDecodeError:
                pass
        out.append(raw)
    return out


@pytest.mark.asyncio
async def test_two_sessions_in_one_workspace_message_each_other(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    registry, mock_url = mock_llm
    suffix = unique_suffix
    provider_id = f"llm-pm-{suffix}"
    models = {role: f"pm-{role}-{suffix}" for role in ("a", "b")}
    profiles = {
        role: profile_id_for(provider_id, models[role])
        for role in models
    }
    agents = {role: f"pm-{role}-{suffix}" for role in ("a", "b")}
    tools = ["workspaces__list_my_sessions", "workspaces__message_session"]

    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "openchat",
            "config": {"url": mock_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        },
    )
    assert pr.status_code in (200, 201), pr.text
    for role in models:
        prof = await client.post(
            "/v1/model_profiles",
            json={
                "id": profiles[role],
                "description": f"e2e session-messaging profile {role}",
                "provider_id": provider_id,
                "model_name": models[role],
                "context_length": 32000,
            },
        )
        assert prof.status_code in (200, 201), prof.text
    for role in models:
        ag = await client.post(
            "/v1/agents",
            json={
                "id": agents[role],
                "description": f"e2e session-messaging agent {role}",
                "model": {"profile_id": profiles[role]},
                "tools": tools,
                "prompt": "Find the other session and say ping, or answer it.",
            },
        )
        assert ag.status_code in (200, 201), ag.text

    try:
        # A NON-default workspace, so the journey does not depend on the
        # default workspace's id (pivot P2's prime).
        ws = await client.post(
            "/v1/workspaces", json={"template_id": "local-default"},
        )
        assert ws.status_code == 201, ws.text
        wid = ws.json()["id"]

        # Session B first (the plan's order), then A; neither gets
        # instructions: A is steered below, B is woken by A's message.
        sess_b = await client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={"binding": {"kind": "agent", "agent_id": agents["b"]}},
        )
        assert sess_b.status_code in (200, 201), sess_b.text
        sid_b = sess_b.json()["id"]
        sess_a = await client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={"binding": {"kind": "agent", "agent_id": agents["a"]}},
        )
        assert sess_a.status_code in (200, 201), sess_a.text
        sid_a = sess_a.json()["id"]

        # One scripted model per session. A's rules, in match order: the
        # first turn lists the workspace's sessions (the tool is offered);
        # once the listing (which names B) is the latest tool result, A
        # messages B; once message_session's result (which carries
        # "delivery") is the latest tool result, A reports and the turn
        # ends; the reply that reaches A as its next user input is
        # acknowledged. B's rules: the steered-in ping asks for a reply
        # through message_session; once that result is in, the turn ends.
        registry.register(models["a"], [
            Rule(when_last_user_contains=f"pong-{suffix}",
                 emit_text=f"GOT-PONG-{suffix}"),
            Rule(when_last_tool_result_contains='"delivery"',
                 emit_text=f"SENT-{suffix}"),
            Rule(when_last_tool_result_contains=sid_b,
                 emit_tool="workspaces__message_session",
                 emit_args={"session_id": sid_b, "text": f"ping-{suffix}"},
                 emit_tool_call_id="call_msg"),
            Rule(when_tool_offered="workspaces__list_my_sessions",
                 emit_tool="workspaces__list_my_sessions",
                 emit_args={},
                 emit_tool_call_id="call_list"),
        ])
        registry.register(models["b"], [
            Rule(when_tool_result=True, emit_text=f"B-DONE-{suffix}"),
            Rule(when_last_user_contains=f"ping-{suffix}",
                 emit_tool="workspaces__message_session",
                 emit_args={"session_id": sid_a, "text": f"pong-{suffix}"},
                 emit_tool_call_id="call_pong"),
        ])

        steer = await client.post(
            f"/v1/workspaces/{wid}/sessions/{sid_a}/steer",
            json={"instruction": "Find the other session and say ping."},
        )
        assert steer.status_code in (200, 201, 202), steer.text

        # A reads the reply when its turn ends; B finishes its reply turn.
        a_log = await _wait_for(client, sid_a, f"GOT-PONG-{suffix}")
        b_log = await _wait_for(client, sid_b, f"B-DONE-{suffix}")

        # B saw A's ping as a user message with the attribution prefix.
        pings = _user_inputs(b_log, f"ping-{suffix}")
        assert pings, "A's ping never reached B's transcript"
        ping = pings[0]
        assert ping["text"].startswith(
            f"[from session {sid_a} (agent {agents['a']})]"
        ), ping["text"]
        assert ping["peer"] == {
            "from_session_id": sid_a,
            "from_workspace_id": wid,
            "from_agent_id": agents["a"],
            "from_graph_id": None,
            "hops": 1,
        }

        # A saw B's pong as a user message; the hop count survived the
        # queue (B replied while A's turn was still open).
        pongs = _user_inputs(a_log, f"pong-{suffix}")
        assert pongs, "B's pong never reached A's transcript"
        pong = pongs[0]
        assert pong["text"].startswith(
            f"[from session {sid_b} (agent {agents['b']})]"
        ), pong["text"]
        assert pong["peer"]["from_session_id"] == sid_b
        assert pong["peer"]["hops"] == 2, pong["peer"]

        # A's list_my_sessions result named B (not itself) as the other
        # session and marked its own item self: true.
        listings = [
            out for out in _tool_outputs(a_log)
            if isinstance(out, dict)
            and isinstance(out.get("items"), list)
            and any(
                isinstance(i, dict) and "self" in i
                for i in out["items"]
            )
            and out.get("workspace_id") == wid
        ]
        assert listings, "A never listed its workspace's sessions"
        by_id = {i["id"]: i for i in listings[0]["items"]}
        assert by_id[sid_a]["self"] is True
        assert by_id[sid_b]["self"] is False

        # Every delivery emitted session.messaged, envelope session_id =
        # the target, payload naming the sender.
        events = await client.get(
            "/v1/events", params={"event_type": "session.messaged"},
        )
        assert events.status_code == 200, events.text
        items = events.json()["items"]
        assert any(
            e["session_id"] == sid_b
            and e["payload"].get("from_session_id") == sid_a
            for e in items
        ), items
        assert any(
            e["session_id"] == sid_a
            and e["payload"].get("from_session_id") == sid_b
            for e in items
        ), items
    finally:
        await client.delete(f"/v1/agents/{agents['a']}")
        await client.delete(f"/v1/agents/{agents['b']}")
        await client.delete(f"/v1/workspaces/{wid}")
        for role in models:
            await client.delete(f"/v1/model_profiles/{profiles[role]}")
        await client.delete(f"/v1/llm_providers/{provider_id}")
