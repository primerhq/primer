"""E2E: cross-session ask_user park isolation + sequential resume journey.

Multi-subsystem user-journey that pins event isolation across
SIMULTANEOUSLY-PARKED sessions. Two independent sessions are each
driven through a REAL ask_user park (distinct prompts), then walked
through cross-session pending/respond mismatch probes and sequential
resume cycles: A's resume must not perturb B's park, then B's resume
completes independently.

Existing siblings cover orthogonal contracts:

  * T0813 -- same-session concurrent /respond traffic
  * T0862 -- single-session full ask_user resume cycle
  * T0760 -- tool_call_id mismatch on a SINGLE parked session

T0866 fills the cross-cutting gap: two distinct sessions parked at
the same instant, each with its own prompt, must remain fully
isolated. A respond directed at session A with a foreign tcid must
404; A's resume must not flip B's parked state.

Parks are driven through the REAL engine path: two scripted mock-LLM
agents each emit an ask_user call, the tools yield, and the
ClaimEngine parks each session. The prior revision asyncpg-injected
park state + a now-deleted ``session_leases`` row; the active
ClaimEngine holds its lease in the engine and the sqlite e2e
deployment has no Postgres, so this drives the genuine path and
reads state back through the API.

Pinned invariants:

  * Per-session park visibility -- GET /pending on A never returns
    B's prompt, even though both sessions are parked on ``ask_user``
    at the same time.
  * Foreign-tcid isolation -- POST /respond with a tcid that doesn't
    match the parked yield returns 404 (routing-layer NotFound, not
    /errors/internal) and leaves the row untouched.
  * Resume side-channel isolation -- after A's bus event flips A to
    resumable and the engine clears A's park, B's parked_status is
    still ``parked`` (B's row was never touched).
  * Per-session turn_no monotonicity -- A and B each advance their
    own turn_no; neither advances on the other's resume.
  * No /errors/internal envelope leaks at any HTTP step.

Covers backlog item T0866.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests._support.yield_journeys import drive_park_on_tool, wait_for_resume


# ===========================================================================
# T0866 -- cross-session ask_user park isolation + sequential resume
# ===========================================================================


@pytest.mark.asyncio
async def test_t0866_multi_session_ask_user_cross_isolation_journey(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0866 -- Two sessions parked on ``ask_user`` simultaneously
    must maintain full isolation across pending/respond/resume.

    Steps:

      1. Drive two real ask_user parks under two scripted agents with
         distinct prompts.
      2. GET /pending on each session -- assert each returns its own
         prompt (no cross-leak even though both rows live in the same
         table).
      3. POST /respond on A with a foreign tcid -> 404; B's parked
         state stays untouched.
      4. POST /respond on B with a foreign tcid -> 404; A's parked
         state stays untouched.
      5. POST /respond on A with A's tcid -> 202; wait for A to
         resume + clear its park.
      6. While/after A resumes, B's row stays parked with B's prompt +
         turn_no unchanged.
      7. POST /respond on B with B's tcid -> 202; wait for B to
         resume.
      8. Verify each session's turn_no advanced INDEPENDENTLY.

    No /errors/internal envelope at any step.
    """
    registry, base_url = mock_llm
    prompt_a = f"Session A asks: pick a colour. {unique_suffix}"
    prompt_b = f"Session B asks: name a constellation. {unique_suffix}"

    # ----- 1. Drive two real ask_user parks -------------------------
    sid_a, _, parked_a = await drive_park_on_tool(
        client, registry, base_url, suffix=f"a-{unique_suffix}",
        tool="misc__ask_user", tool_args={"prompt": prompt_a}, root=tmp_path,
    )
    sid_b, _, parked_b = await drive_park_on_tool(
        client, registry, base_url, suffix=f"b-{unique_suffix}",
        tool="misc__ask_user", tool_args={"prompt": prompt_b}, root=tmp_path,
    )
    initial_turn_a = parked_a["turn_no"]
    initial_turn_b = parked_b["turn_no"]

    # ----- 2. Per-session /pending isolation ------------------------
    r = await client.get(f"/v1/sessions/{sid_a}/ask_user/pending")
    assert r.status_code == 200, r.text
    pending_a = r.json()
    tcid_a = pending_a["tool_call_id"]
    assert prompt_a in pending_a["prompt"], pending_a

    r = await client.get(f"/v1/sessions/{sid_b}/ask_user/pending")
    assert r.status_code == 200, r.text
    pending_b = r.json()
    tcid_b = pending_b["tool_call_id"]
    assert prompt_b in pending_b["prompt"], pending_b
    # Different prompts proves the row lookup is session-scoped.
    assert pending_a["prompt"] != pending_b["prompt"], (
        f"both sessions returned the same prompt -- table-scan missing "
        f"WHERE id=$1: a={pending_a!r} b={pending_b!r}"
    )

    # ----- 3. Foreign-tcid mismatch on A -> 404 ---------------------
    foreign = "tc-does-not-exist"
    r = await client.post(
        f"/v1/sessions/{sid_a}/ask_user/respond",
        json={"tool_call_id": foreign, "response": "wrong-target"},
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body.get("type", "").endswith("/not-found"), body
    assert "/errors/internal" not in json.dumps(body), body
    # B's row stays parked, untouched.
    r = await client.get(f"/v1/sessions/{sid_b}")
    assert r.json()["parked_status"] == "parked", r.text

    # ----- 4. Foreign-tcid mismatch on B -> 404 ---------------------
    r = await client.post(
        f"/v1/sessions/{sid_b}/ask_user/respond",
        json={"tool_call_id": foreign, "response": "wrong-target"},
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body.get("type", "").endswith("/not-found"), body
    assert "/errors/internal" not in json.dumps(body), body
    # A's row stays parked, untouched.
    r = await client.get(f"/v1/sessions/{sid_a}")
    assert r.json()["parked_status"] == "parked", r.text

    # ----- 5. Real respond on A -> 202 + resume cycle ---------------
    r = await client.post(
        f"/v1/sessions/{sid_a}/ask_user/respond",
        json={"tool_call_id": tcid_a, "response": "blue"},
    )
    assert r.status_code == 202, r.text
    body_a_final = await wait_for_resume(client, sid_a, min_turn_no=initial_turn_a + 1)
    assert body_a_final["parked_status"] is None, body_a_final
    assert body_a_final.get("parked_state") in (None, {}), body_a_final
    assert body_a_final["turn_no"] > initial_turn_a, body_a_final
    assert "/errors/internal" not in json.dumps(body_a_final), body_a_final

    # ----- 6. B's row must still be parked + untouched --------------
    # Load-bearing isolation pin: A's bus event fired on event_key
    # 'ask_user:{sid_a}:{tcid_a}'; B's event_key must NOT have been
    # triggered. The listener + claim loop must skip B.
    r = await client.get(f"/v1/sessions/{sid_b}")
    assert r.status_code == 200, r.text
    b_mid = r.json()
    assert b_mid["parked_status"] == "parked", (
        f"B's parked_status changed during A's resume -- cross-session "
        f"listener leak suspected. b={b_mid!r}"
    )
    assert b_mid["turn_no"] == initial_turn_b, (
        f"B's turn_no advanced during A's resume -- turn counter "
        f"crosstalk. b={b_mid!r}"
    )

    r = await client.get(f"/v1/sessions/{sid_b}/ask_user/pending")
    assert r.status_code == 200, r.text
    pending_b_mid = r.json()
    assert prompt_b in pending_b_mid["prompt"], pending_b_mid

    # ----- 7. Real respond on B -> 202 + resume cycle ---------------
    r = await client.post(
        f"/v1/sessions/{sid_b}/ask_user/respond",
        json={"tool_call_id": tcid_b, "response": "orion"},
    )
    assert r.status_code == 202, r.text
    body_b_final = await wait_for_resume(client, sid_b, min_turn_no=initial_turn_b + 1)
    assert body_b_final["parked_status"] is None, body_b_final
    assert body_b_final.get("parked_state") in (None, {}), body_b_final
    assert body_b_final["turn_no"] > initial_turn_b, body_b_final
    assert "/errors/internal" not in json.dumps(body_b_final), body_b_final
