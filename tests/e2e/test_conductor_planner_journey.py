"""The conductor loop end to end: operator -> planner -> tool-runner.

Phase 1 of the base-agents design. One scripted model plays all three
roles; the rules disambiguate by which tools each agent is OFFERED,
which is itself an assertion that the grants differ: only the operator
carries invoke_agent, only the tool-runner carries call_tool, and the
planner reaches its rules precisely because it matches neither.

The chain proves four things at once: invoke_agent nesting (planner and
tool-runner both run inline in the operator's turn), the planner
grounding itself through the unified search before planning, the
tool-runner reaching a capability through system__call_tool rather than
a direct grant, and the nested call landing on the unified search with
a mode_used report.
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
    loop_model = f"p1-loop-{unique_suffix}"
    provider_id = f"llm-p1-{unique_suffix}"
    loop_profile = profile_id_for(provider_id, loop_model)
    ran = f"RAN-{unique_suffix}"
    marker = f"CONDUCTED-{unique_suffix}"
    plan_step = "1. tool-runner: fetch the agents digest"

    # ----- The script. Rule order is FIRST-MATCH-WINS -----------------
    registry.register(
        loop_model,
        [
            # Tool-runner, turn 2: its nested call returned; report.
            Rule(
                when_tool_offered="system__call_tool",
                when_tool_result=True,
                emit_text=ran,
            ),
            # Tool-runner, turn 1: the only agent offered call_tool.
            # The nested target is the unified search, so the transcript
            # must carry a mode_used from a call the runner was never
            # directly granted.
            Rule(
                when_tool_offered="system__call_tool",
                emit_tool="system__call_tool",
                emit_args={
                    "toolset_id": "collections",
                    "tool_name": "search",
                    "arguments": {"collection": "system", "query": "agents"},
                },
                emit_tool_call_id="call_run",
            ),
            # Operator, turn 3: the step ran; conclude.
            Rule(
                when_tool_offered="system__invoke_agent",
                when_last_tool_result_contains=ran,
                emit_text=f"done {marker}",
            ),
            # Operator, turn 2: the plan arrived; execute its one step.
            Rule(
                when_tool_offered="system__invoke_agent",
                when_last_tool_result_contains="tool-runner:",
                emit_tool="system__invoke_agent",
                emit_args={
                    "agent_id": "tool-runner",
                    "prompt": "fetch the agents digest",
                },
                emit_tool_call_id="call_step1",
            ),
            # Operator, turn 1: multi-step work goes to the planner first.
            Rule(
                when_tool_offered="system__invoke_agent",
                emit_tool="system__invoke_agent",
                emit_args={
                    "agent_id": "planner",
                    "prompt": (
                        "Task: produce a digest of this install's agents. "
                        "Context: fresh install, roster seeded."
                    ),
                },
                emit_tool_call_id="call_plan",
            ),
            # Planner, turn 2: grounded; emit the plan.
            Rule(when_tool_result=True, emit_text=plan_step),
            # Planner, turn 1: ground in the system catalog first.
            Rule(
                emit_tool="collections__search",
                emit_args={"collection": "system", "query": "agents digest"},
                emit_tool_call_id="call_ground",
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
    prof = await client.post(
        "/v1/model_profiles",
        json={
            "id": loop_profile,
            "description": "e2e scripted profile",
            "provider_id": provider_id,
            "model_name": loop_model,
            "context_length": 32000,
        },
    )
    assert prof.status_code in (200, 201), prof.text

    # ----- Seed the roster, then repoint it at the script -------------
    seeded = await client.post("/v1/setup/seed")
    assert seeded.status_code == 200, seeded.text

    originals: dict[str, dict] = {}
    try:
        for agent_id in ("operator", "planner", "tool-runner"):
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
            "the tool-runner must reach capability through the meta-tool"
        )
        assert "mode_used" in text, (
            "the nested call must land on the unified search"
        )
    finally:
        for agent_id, row in originals.items():
            await client.put(f"/v1/agents/{agent_id}", json=row)
