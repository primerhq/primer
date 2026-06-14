"""E2E: cancel-yielded-tool -> resume cycle for ask_user (real path).

Sibling to T0861 (operator approve), T0862 (ask_user respond), and
T0863 (timeout-as-rejection). T0864 closes the fourth corner of the
resume contract: operator-initiated cancel-yielded-tool fires the
resume cycle through the YieldCancelled payload path.

Park is driven through the REAL engine path: a scripted mock-LLM
agent emits an ``ask_user`` tool call, the tool yields, and the
ClaimEngine parks the session (drops the lease + writes the park
columns). The prior revision seeded a now-deleted ``session_leases``
row via asyncpg; the active ClaimEngine holds its lease in the engine
(in memory for the in-process bus), so a DB-only seed could never
re-arm it.

The cycle:
  1. Drive a scripted ask_user turn -> engine parks the session.
  2. POST /v1/sessions/{sid}/yields/{tcid}/cancel
     {reason: "..."} -> 202. The cancel router publishes
     make_cancelled_payload onto the bus.
  3. The YieldEventListener picks up the event, flips
     parked->resumable + re-arms the engine lease.
  4. The ClaimEngine claim loop wakes, claims the row.
  5. Resume -> classify_resume_payload detects the
     __yield_cancelled__ marker -> constructs a YieldCancelled
     instance -> get_resume_hook("ask_user") -> ask_user_resume
     synthesises a ToolCallResult carrying cancelled=True +
     cancel_reason.
  6. The synthesised tool_result is persisted and the turn advances.
  7. Poll /v1/sessions/{sid} until parked_status=None.
  8. Assert parked cleared + turn_no advanced.

Multi-subsystem in one test:
  * scripted agent runs a real turn through session-dispatch
  * misc.ask_user yields -> engine parks
  * cancel-yielded-tool router (publishes YieldCancelled marker)
  * event bus + YieldEventListener (flip parked->resumable + re-arm)
  * ClaimEngine claim loop
  * resume -> registry path (ask_user_resume)
  * yield_runtime.classify_resume_payload -> YieldCancelled instance
  * storage clear_park + turn advance

Covers backlog item T0864.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests._support.smk import smk
from tests._support.yield_journeys import drive_park_on_tool, wait_for_resume


# ===========================================================================
# T0864 -- cancel-yielded-tool -> resume cycle (YieldCancelled branch)
# ===========================================================================


@smk("SMK-EVT-05")
@pytest.mark.asyncio
async def test_t0864_cancel_yielded_tool_publishes_and_resumes_session(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0864 -- End-to-end cancel-yielded-tool cycle: park, POST
    /yields/{tcid}/cancel, the bus publishes the YieldCancelled
    marker, the engine resumes, the resume cycle clears the park.

    Pinned invariants:
      * A real ask_user tool call yields and the engine parks the
        session.
      * cancel-yielded-tool router POST /v1/sessions/{sid}/
        yields/{tcid}/cancel returns 202 {"status":"accepted"}.
      * The router publishes make_cancelled_payload({reason}) onto
        the bus.
      * The YieldEventListener flips parked->resumable + re-arms
        the engine lease.
      * The ClaimEngine claim loop wakes, claims the row.
      * Resume -> classify_resume_payload detects the
        __yield_cancelled__ marker -> constructs YieldCancelled with
        the reason.
      * get_resume_hook("ask_user") -> ask_user_resume synthesises a
        ToolCallResult carrying cancelled=True + cancel_reason.
      * The synthesised tool_result is persisted and the turn advances.
      * Observable: parked_status=None, turn_no advanced.

    The synthesised tool_result's content (cancel reason plumbed
    through to the agent) is unit-tested at the function level in
    tests/toolset/test_ask_user.py::test_resume_with_cancelled. This
    test pins the END-TO-END cycle: park -> HTTP -> bus -> engine ->
    resume -> clear_park.
    """
    registry, base_url = mock_llm
    cancel_reason = "user changed mind about the question"

    # ----- 1. Drive a real ask_user turn until the engine parks ------
    sid, _scenario, parked = await drive_park_on_tool(
        client, registry, base_url,
        suffix=unique_suffix,
        tool="system__ask_user",
        tool_args={"prompt": "What is your favourite colour?"},
        root=tmp_path,
    )
    initial_turn_no = parked["turn_no"]

    # ----- 2. Sanity: /ask_user/pending sees the park ---------------
    r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
    assert r.status_code == 200, r.text
    tool_call_id = r.json()["tool_call_id"]

    # ----- 3. POST /yields/{tcid}/cancel -> 202 --------------------
    r = await client.post(
        f"/v1/sessions/{sid}/yields/{tool_call_id}/cancel",
        json={"reason": cancel_reason},
    )
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "accepted"}, r.text

    # ----- 4. Poll until resume cycle completes ---------------------
    body = await wait_for_resume(client, sid, min_turn_no=initial_turn_no + 1)

    # parked_state cleared.
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
