"""E2E: the S5 vision loop, end to end against a live server.

The user asks for a capability that does not exist. The operator consults
the system collection, delegates to the builder inside its own turn
(inline delegation), the builder's create_agent call trips the seeded
approval gate, the operator approves it, and the operator finally invokes
the freshly created agent and reports its output.

S6 core-kit topology: every seeded agent now carries the SAME core kit
(system__call_tool plus the wiki, ask, and inform tools), so no capability
is reached by a direct grant: the operator's index consult, both
delegations, and the builder's construction all go through
system__call_tool meta-dispatch. The offered tool set therefore no longer
separates the roles, and the scripts disambiguate by REQUEST MODEL name
instead (mock_llm.py keys its rules on the request model):

* ``s5-op-<suffix>``      - the operator.
* ``s5-builder-<suffix>`` - the builder.
* ``s5-weather-<suffix>`` - the agent the builder creates. Its own model,
  so its reply cannot be confused with an operator or builder turn.

The call_tool -> crud__create_agent leg pins the gate semantics: the
meta-dispatch resolves the INNER tool's shipped approval policy and parks
the OUTER session (the builder runs inline in the operator's turn), so
the journey's single-session approval watch is unchanged.

The journey also asserts the S6 grant topology of the seeded roster
before any repoint: every seeded agent carries exactly the core kit, the
operator adds the two workspaces messaging tools on top.

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

from tests._support.default_workspace import default_workspace_id
from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for

#: The S6 core kit as the seeded agents carry it (source:
#: primer/toolset/core_kit.py; grants: primer/bootstrap/operator_defaults.py).
CORE_KIT_EXPECTED = [
    "system__call_tool",
    "system__wiki_read",
    "system__wiki_search",
    "system__ask_user",
    "misc__inform_user",
]

#: The two workspaces messaging tools the operator adds on top of the kit
#: (source: OPERATOR_MESSAGING_TOOLS in primer/bootstrap/operator_defaults.py).
OPERATOR_MESSAGING_EXPECTED = [
    "workspaces__list_my_sessions",
    "workspaces__message_session",
]


async def _wait_for(
    client: httpx.AsyncClient,
    session_id: str,
    needle: str,
    *,
    timeout_s: float = 120.0,
) -> dict:
    """Poll the transcript for ``needle``, approving any gate that parks.

    The builder's gated create_agent - reached through call_tool, which
    resolves the inner tool's policy - parks the OPERATOR's session (the
    builder runs INLINE inside the operator's turn); there is one session
    id to watch and one to approve on.

    A gate is answered once: the poller keeps the gates it answered
    (``tool_call_id`` with ``gate_id``: the provider id alone repeats
    across rounds, a gate id does not) and skips one served again
    (ticket 01a16ca6: the pending read served a decided gate until a
    worker claimed the row, and the second answer was a 404).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    approvals = 0
    answered: set[tuple[str, str | None]] = set()
    while loop.time() < deadline:
        pending = await client.get(
            f"/v1/sessions/{session_id}/tool_approval/pending",
        )
        body = pending.json() if pending.status_code == 200 else None
        gate = (body["tool_call_id"], body.get("gate_id")) if body else None
        if body is not None and gate not in answered:
            answered.add(gate)
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
    op_model = f"s5-op-{unique_suffix}"
    builder_model = f"s5-builder-{unique_suffix}"
    weather_model = f"s5-weather-{unique_suffix}"
    provider_id = f"llm-s5-{unique_suffix}"
    op_profile = profile_id_for(provider_id, op_model)
    builder_profile = profile_id_for(provider_id, builder_model)
    weather_profile = profile_id_for(provider_id, weather_model)
    new_agent_id = f"agent-weather-{unique_suffix}"
    built = f"BUILT-{unique_suffix}"
    marker = f"CAPABILITY-{unique_suffix}"

    # ----- The scripts. One model per role; FIRST-MATCH-WINS ----------
    registry.register(
        op_model,
        [
            # Operator, turn 4: the new agent has answered.
            Rule(when_last_tool_result_contains=marker, emit_text=f"done {marker}"),
            # Operator, turn 3: the builder reported back; invoke what it made.
            Rule(
                when_last_tool_result_contains=built,
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "system",
                    "tool_name": "invoke_agent",
                    "arguments": {"agent_id": new_agent_id, "prompt": "weather?"},
                },
                emit_tool_call_id="call_invoke",
            ),
            # Operator, turn 2: the index consult came back; delegate.
            Rule(
                when_tool_result=True,
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "system",
                    "tool_name": "invoke_agent",
                    "arguments": {
                        "agent_id": "builder",
                        "prompt": "create an agent that answers weather questions",
                    },
                },
                emit_tool_call_id="call_delegate",
            ),
            # Operator, turn 1: consult the index first, through the
            # unified search reached via the meta-tool (mode auto -> the
            # fulltext floor, which on this lane exercises the Postgres
            # FTS path for real).
            Rule(
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "collections",
                    "tool_name": "search",
                    "arguments": {"collection": "system", "query": "weather"},
                },
                emit_tool_call_id="call_grep",
            ),
        ],
    )
    registry.register(
        builder_model,
        [
            # Builder, turn 2: its create_agent was approved and ran; report.
            Rule(when_tool_result=True, emit_text=built),
            # Builder, turn 1: construct through the meta-tool. The inner
            # crud__create_agent carries the shipped approval policy, so
            # this call parks the outer (operator) session for approval.
            Rule(
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "crud",
                    "tool_name": "create_agent",
                    "arguments": {
                        "entity": {
                            "id": new_agent_id,
                            "description": "answers weather questions",
                            "model": {"profile_id": weather_profile},
                            "system_prompt": ["Answer weather questions."],
                        }
                    },
                },
                emit_tool_call_id="call_build",
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
    for pid, model in (
        (op_profile, op_model),
        (builder_profile, builder_model),
        (weather_profile, weather_model),
    ):
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
    # The default workspace is "primer" on an install that predates default_workspace_id and "prime" on a fresh one: ask, never name it.
    wid = await default_workspace_id(client)

    originals: dict[str, dict] = {}
    session_id: str | None = None
    try:
        for agent_id in ("operator", "builder"):
            got = await client.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 200, got.text
            row = got.json()
            originals[agent_id] = row
        # The S6 grant topology, before any repoint: everyone carries
        # exactly the core kit; the operator adds the messaging tools.
        assert originals["builder"]["tools"] == CORE_KIT_EXPECTED, (
            f"the builder must carry exactly the S6 core kit "
            f"(got {originals['builder']['tools']})"
        )
        assert originals["operator"]["tools"] == (
            CORE_KIT_EXPECTED + OPERATOR_MESSAGING_EXPECTED
        ), (
            f"the operator must carry the S6 core kit plus the messaging tools "
            f"(got {originals['operator']['tools']})"
        )
        for agent_id, profile in (
            ("operator", op_profile),
            ("builder", builder_profile),
        ):
            row = originals[agent_id]
            upd = await client.put(
                f"/v1/agents/{agent_id}",
                json={**row, "model": {"profile_id": profile}},
            )
            assert upd.status_code in (200, 201), upd.text

        # ----- Drive the loop -----------------------------------------
        sess = await client.post(
            f"/v1/workspaces/{wid}/sessions",
            json={"binding": {"kind": "agent", "agent_id": "operator"}},
        )
        assert sess.status_code in (200, 201), sess.text
        session_id = sess.json()["id"]

        steer = await client.post(
            f"/v1/workspaces/{wid}/sessions/{session_id}/steer",
            json={"instruction": "I need something that answers weather questions"},
        )
        assert steer.status_code in (200, 201, 202), steer.text

        outcome = await _wait_for(client, session_id, f"done {marker}")

        # ----- The loop's shape, not just its ending -------------------
        text = json.dumps(outcome["log"])
        assert "system__call_tool" in text, (
            "every step of the loop must run through the meta-tool (S6 core kit)"
        )
        assert (
            '"search"' in text
            and '"invoke_agent"' in text
            and '"create_agent"' in text
        ), (
            "the index consult, both delegations, and the construction must "
            "all reach their tools by id through call_tool"
        )
        assert "mode_used" in text and "fulltext" in text, (
            "the unified search must have run and reported its rung"
        )
        assert built in text, "the builder must have reported what it created"
        assert outcome["approvals"] >= 1, (
            "create_agent, reached through call_tool, must have tripped the gate"
        )

        created = await client.get(f"/v1/agents/{new_agent_id}")
        assert created.status_code == 200, created.text
    finally:
        for agent_id, row in originals.items():
            await client.put(f"/v1/agents/{agent_id}", json=row)
        await client.delete(f"/v1/agents/{new_agent_id}")
        for pid in (op_profile, builder_profile, weather_profile):
            await client.delete(f"/v1/model_profiles/{pid}")
        await client.delete(f"/v1/llm_providers/{provider_id}")
