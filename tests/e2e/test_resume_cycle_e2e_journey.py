"""E2E: full park -> respond approved -> resume cycle for the approval gate.

Flagship test for the worker-pool / ClaimEngine resume wiring: an
operator answers the approval gate, the bus event fires, the engine
resumes the session, the parked columns clear, and turn_no advances.

Park is driven through the REAL engine path: a scripted mock-LLM
agent calls a gated tool (``misc__uuid_v4`` with an enabled
``required`` approval policy), the ToolExecutionManager's approval
gate raises ``YieldToWorker(_approval)``, and the ClaimEngine parks
the session (drops the lease + writes the park columns). The prior
revision seeded a now-deleted ``session_leases`` row via asyncpg; the
active ClaimEngine holds its lease in the engine (in memory for the
in-process bus), so a DB-only seed could never re-arm it.

The cycle:
  1. Drive a scripted gated-tool turn -> engine parks on ``_approval``.
  2. GET /v1/sessions/{id}/tool_approval/pending -> sanity check.
  3. POST /v1/sessions/{id}/tool_approval/respond
     {tool_call_id, decision: approved} -> 202. Publishes onto the
     event bus.
  4. The YieldEventListener flips parked->resumable + re-arms the
     engine lease; the ClaimEngine claim loop picks up the row.
  5. Resume runs the inline approval branch (re-dispatches the
     original call with bypass_approval=True), persists the
     tool_result, clears the park, and advances the turn.
  6. Poll GET /v1/sessions/{id} until parked_status is None.
  7. Assert parked_state cleared AND turn_no advanced.

Multi-subsystem in one test:
  * scripted agent runs a real turn through session-dispatch
  * approval gate (ApprovalResolver) -> YieldToWorker(_approval)
  * engine parks (lease dropped + park columns written)
  * tool_approval respond router (publishes onto the event bus)
  * event bus + YieldEventListener (flip parked->resumable + re-arm)
  * ClaimEngine claim loop
  * inline approval resume (bypass_approval re-dispatch)
  * storage (clear_park + turn advance)

Covers backlog item T0861.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
)
from tests._support.yield_journeys import wait_for_resume


async def _drive_approval_park(
    client: httpx.AsyncClient,
    registry,
    base_url: str,
    *,
    suffix: str,
    tmp_path,
) -> tuple[str, dict, str]:
    """Run a real turn that calls a gated tool until the engine parks
    on ``_approval``. Returns ``(session_id, parked_body, policy_id)``.
    """
    pol = f"pol-t861-{suffix}"
    # Approval policies are unique on (toolset_id, tool_name); clear any
    # leftover policy for the pair before creating ours.
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if it.get("toolset_id") == "misc" and it.get("tool_name") == "uuid_v4":
                await client.delete(f"/v1/tool_approval_policies/{it['id']}")
    r = await client.post(
        "/v1/tool_approval_policies",
        json={
            "id": pol,
            "toolset_id": "misc",
            "tool_name": "uuid_v4",
            "enabled": True,
            "approval": {"type": "required"},
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text

    scenario = f"scripted:t861-{suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=suffix, scenario=scenario,
        tools=["misc__uuid_v4"],
        rules=[
            Rule(when_tool_result=False, emit_tool="misc__uuid_v4",
                 emit_args={}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(client, suffix=suffix, root=tmp_path)
    sid = await start_agent_session(
        client, workspace_id=wid, agent_id=agent["agent_id"],
    )

    deadline = asyncio.get_event_loop().time() + 30.0
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()
            if last.get("parked_status") == "parked":
                return sid, last, pol
            if last.get("status") == "ended":
                raise AssertionError(
                    f"session {sid} ended before parking on _approval: "
                    f"reason={last.get('ended_reason')!r} body={last!r}"
                )
        await asyncio.sleep(0.25)
    raise AssertionError(
        f"session {sid} never parked on _approval within 30s; "
        f"last_body={last!r}"
    )


# ===========================================================================
# T0861 -- full park -> respond approved -> resume cycle clears park
# ===========================================================================


@pytest.mark.asyncio
async def test_t0861_approval_park_respond_resume_clears_park_and_advances_turn(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0861 -- End-to-end approval resume cycle: park, respond
    approved, the engine resumes, parked columns clear, turn_no
    advances.

    Pinned invariants:
      * A real gated tool call yields ``_approval`` and the engine
        parks the session.
      * The respond router publishes onto the event bus.
      * The YieldEventListener flips parked->resumable + re-arms the
        engine lease.
      * The ClaimEngine claim loop picks up the row.
      * Resume runs the inline approval branch (bypass_approval
        re-dispatch), persists the tool_result, clears the park, and
        advances the turn.
      * Observable state: parked_status=None, turn_no > parked_turn.
    """
    registry, base_url = mock_llm

    sid, parked, pol = await _drive_approval_park(
        client, registry, base_url,
        suffix=unique_suffix, tmp_path=tmp_path,
    )
    initial_turn_no = parked["turn_no"]

    try:
        # ----- Sanity: park is observable via /pending -----
        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        tool_call_id = r.json()["tool_call_id"]

        # ----- POST respond {approved} -----
        r = await client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={"tool_call_id": tool_call_id, "decision": "approved"},
        )
        assert r.status_code == 202, r.text

        # ----- Poll until parked_status clears (resume done) -----
        body = await wait_for_resume(client, sid, min_turn_no=initial_turn_no + 1, timeout_s=60.0)

        # parked_state cleared (clear_park drops every parked_* column).
        assert body["parked_status"] is None, body
        assert body.get("parked_state") in (None, {}), body
        assert body.get("parked_event_key") in (None, ""), body

        # turn_no advanced through the resume.
        final_turn_no = body["turn_no"]
        assert final_turn_no > initial_turn_no, (
            f"turn_no didn't advance through resume: "
            f"initial={initial_turn_no}, final={final_turn_no}"
        )

        # No /errors/internal envelope leak.
        assert "/errors/internal" not in json.dumps(body), body
    finally:
        try:
            await client.delete(f"/v1/tool_approval_policies/{pol}")
        except Exception:  # noqa: BLE001
            pass
