"""E2E: parked session does not re-claim (turn_no stays bounded while parked).

Regression test for the no-loop invariant: once a session is PARKED its
lease is dropped and the ClaimEngine must NOT re-claim it. The prior bug
caused the engine to re-claim a parked session in a tight loop, driving
turn_no into the thousands.

This test drives a session to park on ask_user, records the turn_no at
park, waits ~3-4 seconds (enough for any erroneous re-claim loop to
manifest), polls the session several times, and asserts that turn_no has
NOT climbed. The session is then cancelled so it does not leave a stale
parked row for other tests.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests._support.yield_journeys import drive_park_on_tool


@pytest.mark.asyncio
async def test_parked_session_turn_no_does_not_climb(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """Parked session must not be re-claimed by the ClaimEngine.

    Pinned invariants:
      * A real ask_user tool call yields and the engine parks the session
        (drops the lease, writes park columns).
      * Over a 3-4 second observation window the session remains parked
        (parked_status == "parked") and turn_no does NOT increase.
      * The session is NOT in "ended" status during the observation window.
      * After observation the session is cancelled via
        POST /v1/sessions/{sid}/yields/{tcid}/cancel so no stale row
        remains for sibling tests.
    """
    registry, base_url = mock_llm

    # ----- 1. Drive a real ask_user turn until the engine parks ------
    sid, _scenario, parked = await drive_park_on_tool(
        client, registry, base_url,
        suffix=unique_suffix,
        tool="misc__ask_user",
        tool_args={"prompt": "no-loop probe: please wait"},
        root=tmp_path,
    )
    turn_at_park = parked["turn_no"]

    # ----- 2. Observe for ~3.5 seconds, polling several times --------
    # Any erroneous re-claim loop would drive turn_no up within this window.
    poll_results: list[dict] = []
    for _ in range(7):
        await asyncio.sleep(0.5)
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            poll_results.append(r.json())

    # ----- 3. Assertions ---------------------------------------------
    for body in poll_results:
        current_turn = body.get("turn_no", turn_at_park)
        assert current_turn <= turn_at_park + 1, (
            f"turn_no climbed while parked (no-loop regression!): "
            f"turn_at_park={turn_at_park}, current_turn_no={current_turn}, "
            f"body={body!r}"
        )
        assert body.get("status") != "ended", (
            f"parked session unexpectedly ended during observation: body={body!r}"
        )
        assert body.get("parked_status") == "parked", (
            f"parked_status changed unexpectedly during observation window: "
            f"body={body!r}"
        )

    # ----- 4. Cancel the parked session (cleanup) --------------------
    r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
    if r.status_code == 200:
        tool_call_id = r.json().get("tool_call_id")
        if tool_call_id:
            await client.post(
                f"/v1/sessions/{sid}/yields/{tool_call_id}/cancel",
                json={"reason": "no-loop test cleanup"},
            )
