"""E2E: full ask_user park -> respond -> resume cycle (real engine path).

Sibling to T0861 (the inline ``_approval`` resume branch). This test
exercises the OTHER branch: the generic registry-driven
``get_resume_hook(tool_name)`` path that serves ``sleep``,
``ask_user``, ``watch_files``, and ``mcp_task``.

Both branches share the same ClaimEngine / event-bus / session-
dispatch plumbing but route through different resume code:

  * ``_approval`` -> inline approval resume re-dispatches the original
    call with ``bypass_approval=True``.
  * everything else -> ``get_resume_hook(tool_name)(metadata, payload)``
    synthesises the tool_result directly from metadata + the event
    payload (no re-dispatch).

T0862 picks ``ask_user`` as the canonical representative because it's
HTTP-driveable (``POST /v1/sessions/{sid}/ask_user/respond`` publishes
the resume event onto the bus) and LM-Studio-free.

Park is driven through the REAL path: a scripted mock-LLM agent emits
an ``ask_user`` tool call, the session-dispatch / ClaimEngine path
runs the turn, the tool yields, and the engine parks the session
(drops the lease + writes the park columns). No DB injection. The
prior revision seeded a now-deleted ``session_leases`` row via
asyncpg; the active ClaimEngine holds its lease in the engine (in
memory for the in-process bus), so a DB-only seed could never re-arm
it.

The cycle:
  1. Drive a scripted ask_user turn -> engine parks the session.
  2. GET /v1/sessions/{sid}/ask_user/pending -> 200 sanity check.
  3. POST /v1/sessions/{sid}/ask_user/respond {tool_call_id,
     response} -> 202. The router publishes onto the bus.
  4. Poll GET /v1/sessions/{sid} until parked_status=None.
  5. Assert parked columns cleared AND turn_no advanced.

Multi-subsystem in one test:
  * scripted agent runs a real turn through session-dispatch
  * misc.ask_user yields -> engine parks (lease dropped + park columns)
  * ask_user respond router (publishes onto the event bus)
  * event_bus + YieldEventListener (flip parked->resumable + re-arm)
  * ClaimEngine claim loop picks up the resumable row
  * yield_resume_registry.get_resume_hook("ask_user")
  * primer.toolset.misc.ask_user_resume (the synthesiser)
  * storage (clear_park + turn advance)

Covers backlog item T0862.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests._support.smk import smk
from tests._support.yield_journeys import drive_park_on_tool, wait_for_resume


# ===========================================================================
# T0862 -- full ask_user park -> respond -> resume cycle (generic-hook branch)
# ===========================================================================


@smk("SMK-EVT-01", "SMK-EVT-02")
@pytest.mark.asyncio
async def test_t0862_ask_user_park_respond_resume_clears_park_and_advances_turn(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0862 -- End-to-end ask_user resume cycle: park, respond,
    engine resumes via the GENERIC registry hook
    (primer.toolset.misc.ask_user_resume), parked columns clear,
    turn_no advances.

    Pinned invariants:
      * A real ask_user tool call yields and the engine parks the
        session (drops the lease, writes park columns).
      * ``/ask_user/respond`` publishes onto the event bus.
      * The YieldEventListener flips parked->resumable + re-arms
        the engine lease.
      * The ClaimEngine claim loop picks up the resumable row.
      * Resume looks up ``ask_user_resume`` via
        ``yield_resume_registry.get_resume_hook("ask_user")`` (NOT
        the ``_approval`` inline branch T0861 covers).
      * Hook returns a ToolCallResult; the resume path persists the
        synthesised tool_result and advances the turn.
      * Observable: parked_status=None, parked_state cleared,
        turn_no > parked_turn.
    """
    registry, base_url = mock_llm
    prompt = "What is the airspeed velocity of an unladen swallow?"

    # ----- 1. Drive a real ask_user turn until the engine parks ------
    sid, _scenario, parked = await drive_park_on_tool(
        client, registry, base_url,
        suffix=unique_suffix,
        tool="system__ask_user",
        tool_args={"prompt": prompt},
        root=tmp_path,
    )
    initial_turn_no = parked["turn_no"]

    # ----- 2. Sanity: /ask_user/pending sees the park ---------------
    r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
    assert r.status_code == 200, r.text
    pending = r.json()
    tool_call_id = pending["tool_call_id"]
    assert prompt in pending["prompt"], pending

    # ----- 3. POST /respond -- operator answers ---------------------
    operator_answer = "African or European?"
    r = await client.post(
        f"/v1/sessions/{sid}/ask_user/respond",
        json={"tool_call_id": tool_call_id, "response": operator_answer},
    )
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "accepted"}, r.text

    # ----- 4. Poll until resume cycle clears parked_status ----------
    body = await wait_for_resume(client, sid, min_turn_no=initial_turn_no + 1)

    # parked_state fully cleared (clear_park drops every column).
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
