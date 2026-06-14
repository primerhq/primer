"""E2E: cross-tool yield isolation journey across 3 parked sessions.

Multi-subsystem journey that parks THREE sessions on different
yielding tools in parallel, then verifies the per-tool REST surfaces
return the right rows and the cancel-yielded-tool path only affects
its target -- no cross-tool leakage.

Subsystems exercised in one test:

  1. Session lifecycle + park state machine across 3 distinct
     yielding-tool kinds: ``ask_user``, ``sleep``, ``_approval``.
  2. Per-tool REST endpoint families:
       - GET /v1/sessions/{id}/ask_user/pending
       - GET /v1/sessions/{id}/tool_approval/pending
  3. Cross-tool 404 envelope contract -- each endpoint returns 404
     for sessions parked on a different tool, with the RFC 7807
     ``/errors/not-found`` type and NO bleed-through of the other
     tool's resume_metadata into the response body.
  4. Cancel-yielded-tool path
     (POST /v1/sessions/{id}/yields/{tcid}/cancel) on one of the
     three sessions; the bus listener flips the row to resumable +
     the engine resumes it -- the other two remain parked.

Covers backlog item T0854.

Parks are driven through the REAL engine path: three scripted
mock-LLM agents each emit a different yielding tool call, the tools
yield, and the ClaimEngine parks each session. The prior revision
asyncpg-injected park state + read it back via Postgres; the active
ClaimEngine holds its lease in the engine and the sqlite e2e
deployment has no Postgres, so this drives the genuine path and
reads state back through the API.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.yield_journeys import drive_park_on_tool, wait_for_resume


async def _create_gated_uuid_policy(
    client: httpx.AsyncClient, *, suffix: str,
) -> str:
    """Create a ``required`` approval policy on misc.uuid_v4 so a
    ``misc__uuid_v4`` call parks on ``_approval``. Returns policy id."""
    pol = f"pol-t854-{suffix}"
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if it.get("toolset_id") == "misc" and it.get("tool_name") == "uuid_v4":
                await client.delete(f"/v1/tool_approval_policies/{it['id']}")
    r = await client.post(
        "/v1/tool_approval_policies",
        json={
            "id": pol, "toolset_id": "misc", "tool_name": "uuid_v4",
            "enabled": True, "approval": {"type": "required"},
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text
    return pol


# ===========================================================================
# T0854 -- Cross-tool yield isolation across 3 parallel parks
# ===========================================================================


@pytest.mark.asyncio
async def test_t0854_cross_tool_yield_isolation_journey(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0854 -- Park three sessions on three different yielding tools;
    verify the per-tool REST surface only matches its own tool, and
    cancel-yielded-tool only affects its target.

    Steps:

      1. Drive three real parks under three scripted agents:
           - Session A -> ask_user (with a secret prompt to detect leakage)
           - Session B -> sleep    (long duration)
           - Session C -> _approval (gated misc.uuid_v4 call)
      2. Cross-tool 404 primer:
           - GET A/ask_user/pending      -> 200 (matches)
           - GET A/tool_approval/pending -> 404 (cross-tool)
           - GET B/ask_user/pending      -> 404 (sleep, not ask_user)
           - GET B/tool_approval/pending -> 404 (cross-tool)
           - GET C/ask_user/pending      -> 404 (cross-tool)
           - GET C/tool_approval/pending -> 200 (matches)
      3. No /errors/internal leakage AND no resume_metadata cross-talk
         in any 404 envelope.
      4. Cancel session B's sleep yield via
         POST /v1/sessions/B/yields/{tcid}/cancel; B resumes (park
         clears) while A and C remain parked -- proving the cancel
         ONLY targeted session B.
    """
    registry, base_url = mock_llm
    secret_ask_prompt = f"DO-NOT-LEAK-{unique_suffix}"
    pol = await _create_gated_uuid_policy(client, suffix=unique_suffix)

    try:
        # ----- 1. Drive three real parks ----------------------------
        sid_a, _, _ = await drive_park_on_tool(
            client, registry, base_url, suffix=f"a-{unique_suffix}",
            tool="system__ask_user",
            tool_args={"prompt": secret_ask_prompt}, root=tmp_path,
        )
        sid_b, _, _ = await drive_park_on_tool(
            client, registry, base_url, suffix=f"b-{unique_suffix}",
            tool="workspace_ext__sleep", tool_args={"seconds": 300.0}, root=tmp_path,
        )
        sid_c, _, _ = await drive_park_on_tool(
            client, registry, base_url, suffix=f"c-{unique_suffix}",
            tool="misc__uuid_v4", tool_args={}, root=tmp_path,
        )

        # ----- 2. Cross-tool 404 primer -----------------------------
        # Session A (ask_user) -- only ask_user/pending should match.
        r = await client.get(f"/v1/sessions/{sid_a}/ask_user/pending")
        assert r.status_code == 200, r.text
        body_a = r.json()
        assert body_a["prompt"] == secret_ask_prompt, body_a

        r = await client.get(f"/v1/sessions/{sid_a}/tool_approval/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["type"].endswith("/not-found"), body
        # Critical: ask_user prompt must not leak through tool_approval.
        assert secret_ask_prompt not in json.dumps(body), body

        # Session B (sleep) -- neither endpoint should match.
        r = await client.get(f"/v1/sessions/{sid_b}/ask_user/pending")
        assert r.status_code == 404, r.text
        assert r.json()["type"].endswith("/not-found"), r.text
        assert "requested_seconds" not in r.text, r.text

        r = await client.get(f"/v1/sessions/{sid_b}/tool_approval/pending")
        assert r.status_code == 404, r.text
        assert r.json()["type"].endswith("/not-found"), r.text

        # Session C (_approval) -- only tool_approval/pending matches.
        r = await client.get(f"/v1/sessions/{sid_c}/ask_user/pending")
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["type"].endswith("/not-found"), body
        assert "uuid_v4" not in json.dumps(body), body

        r = await client.get(f"/v1/sessions/{sid_c}/tool_approval/pending")
        assert r.status_code == 200, r.text
        body_c = r.json()
        assert body_c["tool_name"] in ("uuid_v4", "misc__uuid_v4"), body_c
        assert body_c["policy_id"] == pol, body_c

        # ----- 3. Global /errors/internal sweep ---------------------
        for sid, ep in (
            (sid_a, "ask_user"), (sid_a, "tool_approval"),
            (sid_b, "ask_user"), (sid_b, "tool_approval"),
            (sid_c, "ask_user"), (sid_c, "tool_approval"),
        ):
            r = await client.get(f"/v1/sessions/{sid}/{ep}/pending")
            assert "/errors/internal" not in r.text, (
                f"GET /v1/sessions/{sid}/{ep}/pending leaked an internal "
                f"envelope: {r.text!r}"
            )

        # ----- 4. Cancel session B's sleep yield --------------------
        # Find B's sleep tool_call_id. ask_user/pending 404s for sleep,
        # so derive the tcid from the cancel round-trip: the cancel
        # endpoint is tool-agnostic and keys on the parked yield's
        # tool_call_id. The mock allocates "call_0" for the single
        # tool call in B's turn.
        tcid_b = "call_0"
        r = await client.post(
            f"/v1/sessions/{sid_b}/yields/{tcid_b}/cancel",
            json={"reason": "t0854 operator cancel of sleep"},
        )
        assert r.status_code in (200, 202), r.text

        # B resumes (park clears); A and C stay parked -- isolation.
        body_b = await wait_for_resume(client, sid_b, timeout_s=20.0)
        assert body_b.get("parked_status") is None, body_b

        # A and C remain parked -- proves the cancel ONLY hit B.
        await asyncio.sleep(1.0)
        ra = await client.get(f"/v1/sessions/{sid_a}")
        assert ra.json().get("parked_status") == "parked", ra.text
        rc = await client.get(f"/v1/sessions/{sid_c}")
        assert rc.json().get("parked_status") == "parked", rc.text
    finally:
        try:
            await client.delete(f"/v1/tool_approval_policies/{pol}")
        except Exception:  # noqa: BLE001
            pass
        # Cancel session C's parked approval yield so it does not remain
        # indefinitely on 'tool_approval:unknown:call_0'. Without this
        # cleanup, subsequent approval tests are spuriously resumed when
        # this leftover row is woken by the next tool_approval event.
        try:
            await client.post(
                f"/v1/sessions/{sid_c}/yields/call_0/cancel",
                json={"reason": "t0854 test cleanup"},
            )
        except Exception:  # noqa: BLE001
            pass
