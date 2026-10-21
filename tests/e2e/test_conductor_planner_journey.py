"""The conductor loop end to end: operator -> planner -> tool-runner.

Phase 1 of the base-agents design, rescripted for the S6 core-kit topology:
the seeded agents now all carry the SAME core kit (system__call_tool plus
the wiki, ask, and inform tools), so no capability is reached by a direct
grant - invoke_agent and the unified search alike go through
system__call_tool meta-dispatch. The offered tool set therefore no longer
separates the roles, and the scripts disambiguate by REQUEST MODEL name
instead (mock_llm.py keys its rules on the request model): one model per
role on the scripted provider.

The chain still proves the same four things at once: invoke_agent nesting
(planner and tool-runner both run inline in the operator's turn, through
call_tool), the planner grounding itself through the unified search before
planning, every role reaching capability through system__call_tool rather
than a direct grant, and the nested call landing on the unified search
with a mode_used report.

The journey also asserts the S6 grant topology of the seeded roster
before any repoint: every seeded agent carries exactly the core kit, the
operator adds the two workspaces messaging tools on top.
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
    """Poll the transcript for ``needle``; no gates fire in this journey."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        log = await client.get(
            f"/v1/sessions/{session_id}/messages", params={"limit": 500},
        )
        if log.status_code == 200 and needle in json.dumps(log.json()):
            return log.json()
        await asyncio.sleep(0.5)
    pytest.fail(f"{needle!r} never reached the transcript of {session_id}")


@pytest.mark.asyncio
async def test_operator_plans_then_executes_the_plan(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str,
) -> None:
    registry, mock_url = mock_llm
    op_model = f"p1-op-{unique_suffix}"
    planner_model = f"p1-planner-{unique_suffix}"
    tr_model = f"p1-tr-{unique_suffix}"
    provider_id = f"llm-p1-{unique_suffix}"
    op_profile = profile_id_for(provider_id, op_model)
    planner_profile = profile_id_for(provider_id, planner_model)
    tr_profile = profile_id_for(provider_id, tr_model)
    ran = f"RAN-{unique_suffix}"
    marker = f"CONDUCTED-{unique_suffix}"
    plan_step = "1. tool-runner: fetch the agents digest"

    # ----- The scripts. One model per role; FIRST-MATCH-WINS ----------
    registry.register(
        op_model,
        [
            # Operator, turn 3: the step ran; conclude.
            Rule(
                when_last_tool_result_contains=ran,
                emit_text=f"done {marker}",
            ),
            # Operator, turn 2: the plan arrived; execute its one step.
            Rule(
                when_last_tool_result_contains="tool-runner:",
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "system",
                    "tool_name": "invoke_agent",
                    "arguments": {
                        "agent_id": "tool-runner",
                        "prompt": "fetch the agents digest",
                    },
                },
                emit_tool_call_id="call_step1",
            ),
            # Operator, turn 1: multi-step work goes to the planner first.
            Rule(
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "system",
                    "tool_name": "invoke_agent",
                    "arguments": {
                        "agent_id": "planner",
                        "prompt": (
                            "Task: produce a digest of this install's agents. "
                            "Context: fresh install, roster seeded."
                        ),
                    },
                },
                emit_tool_call_id="call_plan",
            ),
        ],
    )
    registry.register(
        planner_model,
        [
            # Planner, turn 2: grounded; emit the plan.
            Rule(when_tool_result=True, emit_text=plan_step),
            # Planner, turn 1: ground in the system catalog first, through
            # the unified search reached via the meta-tool.
            Rule(
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "collections",
                    "tool_name": "search",
                    "arguments": {"collection": "system", "query": "agents digest"},
                },
                emit_tool_call_id="call_ground",
            ),
        ],
    )
    registry.register(
        tr_model,
        [
            # Tool-runner, turn 2: its nested call returned; report.
            Rule(when_tool_result=True, emit_text=ran),
            # Tool-runner, turn 1: reach the capability through the
            # meta-tool. No seeded agent is directly granted the unified
            # search anymore, so the transcript must carry a mode_used
            # from a call nobody is granted.
            Rule(
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "collections",
                    "tool_name": "search",
                    "arguments": {"collection": "system", "query": "agents"},
                },
                emit_tool_call_id="call_run",
            ),
        ],
    )

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
        (planner_profile, planner_model),
        (tr_profile, tr_model),
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

    # ----- Seed the roster, then repoint it at the script -------------
    seeded = await client.post("/v1/setup/seed")
    assert seeded.status_code == 200, seeded.text
    # The default workspace is "primer" on an install that predates default_workspace_id and "prime" on a fresh one: ask, never name it.
    wid = await default_workspace_id(client)

    originals: dict[str, dict] = {}
    try:
        for agent_id in ("operator", "planner", "tool-runner"):
            got = await client.get(f"/v1/agents/{agent_id}")
            assert got.status_code == 200, got.text
            row = got.json()
            originals[agent_id] = row
        # The S6 grant topology, before any repoint: everyone carries
        # exactly the core kit; the operator adds the messaging tools.
        assert originals["planner"]["tools"] == CORE_KIT_EXPECTED, (
            f"the planner must carry exactly the S6 core kit "
            f"(got {originals['planner']['tools']})"
        )
        assert originals["tool-runner"]["tools"] == CORE_KIT_EXPECTED, (
            f"the tool-runner must carry exactly the S6 core kit "
            f"(got {originals['tool-runner']['tools']})"
        )
        assert originals["operator"]["tools"] == (
            CORE_KIT_EXPECTED + OPERATOR_MESSAGING_EXPECTED
        ), (
            f"the operator must carry the S6 core kit plus the messaging tools "
            f"(got {originals['operator']['tools']})"
        )
        for agent_id, profile in (
            ("operator", op_profile),
            ("planner", planner_profile),
            ("tool-runner", tr_profile),
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
            json={"instruction": "I need a digest of this install's agents"},
        )
        assert steer.status_code in (200, 201, 202), steer.text

        log = await _wait_for(client, session_id, f"done {marker}")

        # ----- The loop's shape, not just its ending -------------------
        text = json.dumps(log)
        assert '"planner"' in text, "the operator must plan first"
        assert plan_step in text, "the planner's plan must reach the operator"
        assert '"tool-runner"' in text, "the named specialist must be invoked"
        assert "system__call_tool" in text, (
            "every role must reach capability through the meta-tool (S6 core kit)"
        )
        assert "mode_used" in text, (
            "the nested call must land on the unified search"
        )
    finally:
        for agent_id, row in originals.items():
            await client.put(f"/v1/agents/{agent_id}", json=row)
