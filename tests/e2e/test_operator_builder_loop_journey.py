"""E2E: the S5 vision loop, end to end against a live server.

The user asks for a capability that does not exist. The operator greps the
system collection, delegates to the builder inside its own turn
(invoke_agent, S1 C1 inline delegation), the builder's create_agent call
trips the seeded approval gate, an operator approves it, and the operator
finally invokes the freshly created agent and reports its output.

Two scenarios, because the scripted mock keys its rules on the REQUEST
MODEL name (mock_llm.py:95-96):

* ``<suffix>-loop``    - the operator AND the builder. They share one
  profile, so they are separated by WHICH TOOLS ARE OFFERED: only the
  builder is granted ``crud__create_agent``.
* ``<suffix>-weather`` - the agent the builder creates. Its own scenario,
  so its reply cannot be confused with an operator or builder turn.

The journey repoints the two SEEDED agents at the scripted provider and
restores their original profiles in a finally: the e2e server is shared
across the session, so leaving 'operator' bound to a mock would poison
every later module.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for


async def _wait_for(
    client: httpx.AsyncClient,
    session_id: str,
    needle: str,
    *,
    timeout_s: float = 120.0,
) -> dict:
    """Poll the transcript for ``needle``, approving any gate that parks.

    The builder runs INLINE inside the operator's turn (C1), so its gated
    create_agent parks the OPERATOR's session; there is one session id to
    watch and one to approve on.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    approvals = 0
    while loop.time() < deadline:
        pending = await client.get(
            f"/v1/sessions/{session_id}/tool_approval/pending",
        )
        if pending.status_code == 200:
            body = pending.json()
            resp = await client.post(
                f"/v1/sessions/{session_id}/tool_approval/respond",
                json={
                    "tool_call_id": body["tool_call_id"],
                    "decision": "approved",
                },
            )
            assert resp.status_code == 202, resp.text
            approvals += 1
        log = await client.get(
            f"/v1/sessions/{session_id}/messages", params={"limit": 500},
        )
        if log.status_code == 200 and needle in json.dumps(log.json()):
            return {"log": log.json(), "approvals": approvals}
        await asyncio.sleep(0.5)
    pytest.fail(
        f"{needle!r} never reached the transcript of {session_id} "
        f"(approvals granted: {approvals})"
    )


@pytest.mark.asyncio
async def test_operator_delegates_construction_and_invokes_the_result(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    registry, mock_url = mock_llm
    loop_model = f"s5-loop-{unique_suffix}"
    weather_model = f"s5-weather-{unique_suffix}"
    provider_id = f"llm-s5-{unique_suffix}"
    loop_profile = profile_id_for(provider_id, loop_model)
    weather_profile = profile_id_for(provider_id, weather_model)
    new_agent_id = f"agent-weather-{unique_suffix}"
    built = f"BUILT-{unique_suffix}"
    marker = f"CAPABILITY-{unique_suffix}"

    # ----- The scripts. Rule order is FIRST-MATCH-WINS ----------------
    registry.register(
        loop_model,
        [
            # Builder, turn 2: it has a tool result, so it reports back.
            Rule(
                when_tool_offered="crud__create_agent",
                when_tool_result=True,
                emit_text=built,
            ),
            # Builder, turn 1: the only agent offered create_agent.
            Rule(
                when_tool_offered="crud__create_agent",
                emit_tool="crud__create_agent",
                emit_args={
                    "entity": {
                        "id": new_agent_id,
                        "description": "answers weather questions",
                        "model": {"profile_id": weather_profile},
                        "system_prompt": ["Answer weather questions."],
                    }
                },
                emit_tool_call_id="call_build",
            ),
            # Operator, turn 4: the new agent has answered.
            Rule(when_last_tool_result_contains=marker, emit_text=f"done {marker}"),
            # Operator, turn 3: the builder reported back; invoke what it made.
            Rule(
                when_last_tool_result_contains=built,
                emit_tool="system__invoke_agent",
                emit_args={"agent_id": new_agent_id, "prompt": "weather?"},
                emit_tool_call_id="call_invoke",
            ),
            # Operator, turn 2: the grep came back empty; delegate.
            Rule(
                when_tool_result=True,
                emit_tool="system__invoke_agent",
                emit_args={
                    "agent_id": "builder",
                    "prompt": "create an agent that answers weather questions",
                },
                emit_tool_call_id="call_delegate",
            ),
            # Operator, turn 1: consult the index first.
            Rule(
                emit_tool="collections__grep_collection",
                emit_args={"collection": "system", "pattern": "weather"},
                emit_tool_call_id="call_grep",
            ),
        ],
    )
    registry.register(weather_model, [Rule(emit_text=marker)])

    # ----- Point this install's provider at the scripted mock ---------
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
    for pid, model in ((loop_profile, loop_model), (weather_profile, weather_model)):
        prof = await client.post(
            "/v1/model_profiles",
            json={
                "id": pid,
                "description": "e2e scripted profile",
                "provider_id": provider_id,
                "model_name": model,
                "context_length": 32000,
            },
        )
        assert prof.status_code in (200, 201), prof.text

    # ----- Seed the world, then repoint the seeded agents -------------
    seeded = await client.post("/v1/setup/seed")
    assert seeded.status_code == 200, seeded.text

    originals: dict[str, dict] = {}
    session_id: str | None = None
    try:
        for agent_id in ("operator", "builder"):
            got = await client.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 200, got.text
            row = got.json()
            originals[agent_id] = row
            upd = await client.put(
                f"/v1/agents/{agent_id}",
                json={**row, "model": {"profile_id": loop_profile}},
            )
            assert upd.status_code in (200, 201), upd.text

        # ----- Drive the loop -----------------------------------------
        sess = await client.post(
            "/v1/workspaces/primer/sessions",
            json={"binding": {"kind": "agent", "agent_id": "operator"}},
        )
        assert sess.status_code in (200, 201), sess.text
        session_id = sess.json()["id"]

        steer = await client.post(
            f"/v1/workspaces/primer/sessions/{session_id}/steer",
            json={"instruction": "I need something that answers weather questions"},
        )
        assert steer.status_code in (200, 201, 202), steer.text

        outcome = await _wait_for(client, session_id, f"done {marker}")

        # ----- The loop's shape, not just its ending -------------------
        text = json.dumps(outcome["log"])
        assert "grep_collection" in text, "the operator must consult the index first"
        assert built in text, "the builder must have reported what it created"
        assert outcome["approvals"] >= 1, "create_agent must have tripped the gate"

        created = await client.get(f"/v1/agents/{new_agent_id}")
        assert created.status_code == 200, created.text
    finally:
        for agent_id, row in originals.items():
            await client.put(f"/v1/agents/{agent_id}", json=row)
        await client.delete(f"/v1/agents/{new_agent_id}")
        for pid in (loop_profile, weather_profile):
            await client.delete(f"/v1/model_profiles/{pid}")
        await client.delete(f"/v1/llm_providers/{provider_id}")
