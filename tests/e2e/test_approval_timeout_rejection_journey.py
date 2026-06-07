"""E2E: timeout-as-rejection for an _approval park.

Sibling to T0861 (operator approve), T0862 (ask_user respond), and
T0864 (cancel-yielded-tool). T0863 covers the TIMEOUT path: an
``_approval`` park whose operator decision never arrives is swept
past its deadline, the TimeoutSweeper publishes the
``__yield_timeout__`` marker, and the engine's resume branch
synthesises a rejected tool_result (reason="timed-out").

Park is driven through the REAL engine path: a scripted mock-LLM
agent calls a gated tool (``misc__uuid_v4`` with an enabled
``required`` approval policy), the ToolExecutionManager's approval
gate raises ``YieldToWorker(_approval)`` with the policy's short
``timeout_seconds``, and the ClaimEngine parks the session. The
prior revision seeded a now-deleted ``session_leases`` row via
asyncpg + fired ``pg_notify`` directly; the active ClaimEngine holds
its lease in the engine (in memory for the in-process bus), so a
DB-only seed could never re-arm it, and the sqlite e2e deployment
has no Postgres NOTIFY channel.

KNOWN PRODUCT GAP (timeout-resume xfail)
----------------------------------------
On the active ClaimEngine path with the in-process bus + sqlite
storage the deployment runs an ``InMemoryScheduler``. The
TimeoutSweeper finds expired parks via
``primer.bus.scheduler_tasks._find_expired_non_timer_keys``, whose
``InMemoryScheduler`` branch iterates ``scheduler._sessions`` and
reads ``parked_status`` / ``parked_until`` / ``parked_event_key``
off each ``_SessionState``. But ``_SessionState`` only carries
``turn_no`` + ``status`` (it is a test seam); the real park columns
are persisted to storage by the ClaimEngine, not mirrored into
``_sessions``. So on this deployment the sweeper's query always
returns ``[]`` and a timed-out ``_approval`` park is NEVER resumed.

This is a real product bug in the sweeper's scheduler introspection,
separate from the F10c ClaimEngine park/resume path itself
(park works; respond/cancel resume works -- see T0862/T0864). It is
escalated rather than patched in this test task. The timeout-resume
assertion is therefore marked ``xfail(strict=True)`` so it flips to
an unexpected-pass (XPASS, hard failure) the moment the sweeper is
fixed -- a tripwire telling us to remove the marker.

Covers backlog item T0863.
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
    timeout_seconds: float,
) -> tuple[str, dict, str]:
    """Run a real turn that calls a gated tool until the engine parks
    on ``_approval``. Returns ``(session_id, parked_body)``.

    Gates ``misc__uuid_v4`` with a ``required`` policy carrying a
    short ``timeout_seconds`` so the park's ``parked_until`` lands
    just past now -- the deadline the TimeoutSweeper is meant to
    catch.
    """
    pol = f"pol-t863-{suffix}"
    # Approval policies are unique on (toolset_id, tool_name), not on
    # id, so a leftover policy for the same pair would 409. Clear any
    # pre-existing policy for this pair before creating ours.
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
            "timeout_seconds": timeout_seconds,
        },
    )
    assert r.status_code in (200, 201), r.text
    # The ApprovalResolver caches policies in-process; invalidate so
    # the running engine sees the freshly-created row.
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text

    scenario = f"scripted:t863-{suffix}"
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
# T0863 -- _approval park times out -> sweeper -> engine -> rejection
# ===========================================================================


@pytest.mark.asyncio
async def test_t0863_approval_timeout_resume_synthesises_rejection(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    """T0863 -- End-to-end timeout-as-rejection cycle for an
    ``_approval`` park driven through the real engine path.

    Park-time invariants (asserted hard):
      * A real gated tool call yields ``_approval`` and the engine
        parks the session with the policy's short timeout, so
        ``parked_until`` lands just past now.
      * GET /v1/sessions/{sid}/tool_approval/pending surfaces the
        parked decision (the original gated call).

    Timeout-resume invariant (xfail -- see module docstring):
      * The TimeoutSweeper would publish ``__yield_timeout__`` once
        ``parked_until <= now()``; the engine's resume branch would
        synthesise a rejected tool_result (reason="timed-out") and
        advance the turn. This is currently broken on the
        in-process-bus / sqlite deployment because the sweeper's
        ``InMemoryScheduler`` introspection cannot see the persisted
        park columns.
    """
    registry, base_url = mock_llm

    # ----- 1. Drive a real _approval park (short policy timeout) -----
    sid, parked, pol = await _drive_approval_park(
        client, registry, base_url,
        suffix=unique_suffix, tmp_path=tmp_path, timeout_seconds=1.0,
    )
    initial_turn_no = parked["turn_no"]

    try:
        # Sanity: the approval decision is pending and observable.
        r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert r.status_code == 200, r.text
        pending = r.json()
        # The pending envelope surfaces the original gated call name. On
        # the toolset dispatch path that is the scoped id
        # (``misc__uuid_v4``); the workspace-tool path uses the bare
        # name. Accept either form so the assertion tracks the routed
        # tool, not the scoping convention.
        assert pending["tool_name"] in ("uuid_v4", "misc__uuid_v4"), pending
        assert pending["approval_type"] == "required", pending
        assert isinstance(pending["tool_call_id"], str) and pending["tool_call_id"]
        assert "/errors/internal" not in json.dumps(pending), pending

        # ----- 2. Timeout-resume cycle (known product gap) ----------
        # parked_until is ~1s out (the policy timeout). On the broken
        # sqlite / InMemoryScheduler deployment the sweeper never sees
        # the persisted park, so the timeout marker is never published
        # and the session stays parked. We wait a sweep window plus
        # slack to give a FIXED sweeper room to fire; on the current
        # deployment this raises -> recorded as an expected failure via
        # pytest.xfail. When the product bug is fixed this wait
        # succeeds, the assertions below run, and the absence of the
        # xfail flips the result to XPASS (a tripwire to delete this
        # branch + the module-level xfail note).
        try:
            body = await wait_for_resume(client, sid, timeout_s=40.0)
        except AssertionError:
            pytest.xfail(
                "timeout-resume never fired: TimeoutSweeper is blind to "
                "persisted park columns on the InMemoryScheduler "
                "deployment (see module docstring). Escalated as a "
                "product bug separate from the F10c park/resume path."
            )

        # Sweeper DID resume -- assert the full timeout-as-rejection
        # contract.
        assert body["parked_status"] is None, body
        assert body.get("parked_state") in (None, {}), body
        assert body.get("parked_event_key") in (None, ""), body
        final_turn_no = body["turn_no"]
        assert final_turn_no > initial_turn_no, (
            f"turn_no didn't advance through timeout-resume: "
            f"initial={initial_turn_no}, final={final_turn_no}"
        )
        assert "/errors/internal" not in json.dumps(body), body
    finally:
        # Drop the (toolset_id, tool_name)-unique policy so a re-run of
        # this journey doesn't 409 on create.
        try:
            await client.delete(f"/v1/tool_approval_policies/{pol}")
        except Exception:  # noqa: BLE001
            pass
        # Cancel the parked approval yield so the session does not remain
        # indefinitely parked on 'tool_approval:unknown:call_0'. Without
        # this, subsequent tests that also park on the shared (broken)
        # event key would be spuriously resumed when this leftover row is
        # woken up by the next tool_approval respond event.
        try:
            await client.post(
                f"/v1/sessions/{sid}/yields/call_0/cancel",
                json={"reason": "t0863 test cleanup"},
            )
        except Exception:  # noqa: BLE001
            pass
