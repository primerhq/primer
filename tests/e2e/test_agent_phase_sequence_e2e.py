"""E2E regression harness for the agent_phase / session_state signals
(01a04d91-a7a0, PHASE 1 of the execution-lifecycle revamp).

Item 4 of the revamp's PHASE 1: the slow-stream mock provider
(tests/_support/mock_llm.py's slow_turn_with_mid_stream_tool_call)
becomes the permanent regression harness for this - a real HTTP
round-trip through the actual OpenChatLLM/AsyncOpenAI client and the
real dispatch/worker/tap pipeline, not a FakeExecutor stand-in
(tests/session/test_dispatch_turn_log.py already covers the phase-
inference logic in isolation at the unit level; this proves the SAME
sequence survives a genuine wire round-trip). This is exactly the shape
01a04d64-b4ba's live diagnosis needed and had no repeatable way to get
without a real, rate-limited, sometimes-unreachable provider.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests._support.mock_llm import Rule, slow_turn_with_mid_stream_tool_call
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session

# Matches tests/e2e/conftest.py's own e2e user - a second, independently
# authenticated client is how this test simulates "a refresh with zero
# live-tap/prior-request context", the exact condition the original
# refresh-mid-turn bug (01a04d64-b4ba) only reproduced under.
_E2E_USER = {"username": "e2e", "password": "e2e-password-123"}


async def _poll_phase_samples(
    client: httpx.AsyncClient, sid: str, *, timeout_s: float = 15.0,
) -> list[tuple[str | None, str | None, str | None]]:
    """Poll the session every 100ms; return (status, session_state,
    agent_phase) samples. Each GET is a fresh, stateless read - the same
    shape a page refresh makes - so this loop is itself already a
    repeated refresh-recovery proof, not just a liveness poll."""
    samples: list[tuple[str | None, str | None, str | None]] = []
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        body = r.json()
        samples.append(
            (body.get("status"), body.get("session_state"), body.get("agent_phase"))
        )
        if body.get("status") == "ended":
            break
        await asyncio.sleep(0.1)
    return samples


@pytest.mark.asyncio
async def test_phase_sequence_through_real_http_round_trip(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
):
    """A real tool-calling turn against the mock provider must visit
    session_state="running" and the agent_phase sequence
    thinking -> executing -> (thinking ->) responding, in that order,
    then land on session_state="ended" once the turn completes cleanly
    (_CLEAN_TURN_RESTS_PARKED defaults False - see dispatch.py - so a
    clean stop still ENDS the session today; this assertion is the
    regression trip-wire for whenever that flag flips)."""
    registry, base_url = mock_llm
    scenario = f"scripted:phase-{unique_suffix}"
    # Short but real delays (not 10-20s) - fast enough for the suite,
    # still genuinely asynchronous real time elapsing across two real
    # HTTP round-trips through the mock, unlike an instant FakeExecutor.
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        tools=["misc__uuid_v4"],
        rules=slow_turn_with_mid_stream_tool_call(
            tool_name="misc__uuid_v4", total_seconds=1.0,
        ),
    )
    wid = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(client, workspace_id=wid, agent_id=agent["agent_id"])

    samples = await _poll_phase_samples(client, sid)

    states_seen = [s for (_, s, _) in samples]
    phases_seen = [p for (_, _, p) in samples if p is not None]

    assert "running" in states_seen, samples
    assert states_seen[-1] == "ended", samples

    assert "thinking" in phases_seen, samples
    assert "executing" in phases_seen, samples
    assert "responding" in phases_seen, samples
    # The tool call must be observed BEFORE the final answer starts -
    # proves this is the real mid-stream-tool-call shape, not a
    # coincidental reordering.
    assert phases_seen.index("executing") < phases_seen.index("responding"), samples


async def _cold_snapshot(base_url: str, sid: str) -> dict:
    """A brand-new, independently-authenticated client's GET - zero
    shared connection, zero live tap/websocket history with the warm
    client that has been polling. This is the acceptance invariant's
    "hard refresh": display must be rebuilt ENTIRELY from served state
    (session_state + agent_phase + durable records), zero live-frame
    dependence (docs/superpowers/2026-08-29-execution-lifecycle-vetting-
    and-revamp.md, ACCEPTANCE INVARIANT)."""
    import contextlib

    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0),
    ) as cold_client:
        with contextlib.suppress(Exception):
            await cold_client.post("/v1/auth/register", json=_E2E_USER)
            await cold_client.post("/v1/auth/login", json=_E2E_USER)
        r = await cold_client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        return r.json()


# turn_no deliberately excluded: it is bumped by the claim engine's
# on_release hook in a SEPARATE commit from the terminal status write
# (tests/_support/runs.py's wait_turn_advanced docstring documents this
# exact, pre-existing gap - "a freshly-completed session can momentarily
# read status=ended with turn_no not yet incremented"), unrelated to
# session_state/agent_phase and out of this lane's scope. Comparing it
# here would assert against a known, already-documented architectural
# race, not a refresh-recovery bug.
_DISPLAY_FIELDS = ("status", "session_state", "agent_phase", "agent_phase_turn_no")


@pytest.mark.asyncio
async def test_refresh_mid_phase_recovers_from_a_cold_client(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path, base_url: str,
):
    """ACCEPTANCE INVARIANT (docs/superpowers/2026-08-29-execution-
    lifecycle-vetting-and-revamp.md, user-stated, binding for this lane):
    at any phase, a hard refresh renders the SAME statuses as pre-refresh,
    rebuilt entirely from served state. This drives a real tool-calling
    turn and, at EVERY distinct (session_state, agent_phase) pair the
    warm client observes, immediately takes a cold-client snapshot and
    asserts the two are identical - not just "some phase survives" (the
    original, narrower version of this test) but the SPECIFIC phase at
    that exact moment, for every phase a tool-calling turn actually
    visits: running+thinking, running+executing, running+responding, and
    ended. "parked" (a real yielding-tool park, not a clean stop) is
    covered separately below - a clean stop takes a structurally
    different path (_CLEAN_TURN_RESTS_PARKED is off by default) and
    never reaches parked_status at all."""
    registry, mock_base_url = mock_llm
    scenario = f"scripted:refresh-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, mock_base_url, suffix=unique_suffix, scenario=scenario,
        tools=["misc__uuid_v4"],
        rules=slow_turn_with_mid_stream_tool_call(
            tool_name="misc__uuid_v4", total_seconds=1.4,
        ),
    )
    wid = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(client, workspace_id=wid, agent_id=agent["agent_id"])

    seen_pairs: set[tuple[str | None, str | None]] = set()
    compared = 0
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        warm = r.json()
        pair = (warm.get("session_state"), warm.get("agent_phase"))
        # ("waiting", None) is the pre-claim instant - auto_start=True
        # claims almost immediately, so it can legitimately have already
        # advanced to "running" by the time a COLD client finishes its
        # own register+login round-trip (real overhead a warm,
        # already-authenticated GET doesn't pay). That is a test-timing
        # artifact, not a refresh-recovery failure - "waiting" pre-claim
        # is static, already-in-the-DB state with nothing ephemeral to
        # lose, and is covered properly (stably, no race) by
        # test_waiting_session_state_survives_a_hard_refresh below via
        # auto_start=False.
        if pair not in seen_pairs and pair != ("waiting", None):
            seen_pairs.add(pair)
            cold = await _cold_snapshot(base_url, sid)
            for field in _DISPLAY_FIELDS:
                assert cold.get(field) == warm.get(field), (
                    f"refresh mismatch at phase {pair} on field {field!r}: "
                    f"warm={warm.get(field)!r} cold={cold.get(field)!r}"
                )
            compared += 1
        if warm.get("status") == "ended":
            break
        await asyncio.sleep(0.1)

    assert compared >= 4, (
        f"only compared {compared} distinct phases, expected at least "
        f"thinking/executing/responding/ended: {seen_pairs}"
    )
    assert ("running", "executing") in seen_pairs, seen_pairs
    assert ("running", "responding") in seen_pairs, seen_pairs
    assert ("ended", None) in seen_pairs, seen_pairs


@pytest.mark.asyncio
async def test_waiting_session_state_survives_a_hard_refresh(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path, base_url: str,
):
    """The "waiting" phase, tested stably: a CREATED-but-not-started
    session (auto_start=False) sits at session_state="waiting"
    indefinitely with nothing racing it, unlike the auto_start=True case
    in the comprehensive test above (which claims almost instantly - a
    cold client's own auth round-trip can legitimately land after the
    claim already happened, a test-timing artifact rather than a
    refresh-recovery failure)."""
    registry, mock_base_url = mock_llm
    scenario = f"scripted:waiting-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, mock_base_url, suffix=unique_suffix, scenario=scenario,
        tools=[],
        rules=[Rule(emit_text="done")],
    )
    wid = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        client, workspace_id=wid, agent_id=agent["agent_id"], auto_start=False,
    )

    warm = (await client.get(f"/v1/sessions/{sid}")).json()
    assert warm.get("session_state") == "waiting", warm
    assert warm.get("status") == "created", warm

    cold = await _cold_snapshot(base_url, sid)
    for field in _DISPLAY_FIELDS:
        assert cold.get(field) == warm.get(field), (
            f"refresh mismatch on a waiting (never-started) session, "
            f"field {field!r}: warm={warm.get(field)!r} cold={cold.get(field)!r}"
        )


@pytest.mark.asyncio
async def test_parked_session_state_survives_a_hard_refresh(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path, base_url: str,
):
    """The one session_state value the tool-calling-turn test above
    cannot reach: a REAL yielding-tool park (a required-approval gate),
    which sets parked_status independently of turn_status/agent_phase.
    Must also survive a hard refresh via a cold client, per the same
    acceptance invariant.

    Approval policies are unique on (toolset_id, tool_name), and DELETE
    does not itself invalidate the resolver's cache (POST .../invalidate
    is a separate call - tests/e2e/test_resume_cycle_e2e_journey.py's
    own cleanup has the identical gap) - so a policy this test's own
    finally block deleted on a PRIOR run can still leave misc__uuid_v4
    gated for every OTHER test in the same long-lived e2e server process
    (confirmed live: two sibling tests in this file that expect a plain,
    non-gated tool call started parking instead, after this test had
    already run once). Defends against inheriting that debris the same
    way _drive_approval_park does: clear any leftover policy for the
    pair before creating this test's own."""
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if it.get("toolset_id") == "misc" and it.get("tool_name") == "uuid_v4":
                await client.delete(f"/v1/tool_approval_policies/{it['id']}")
        await client.post("/v1/tool_approval_policies/invalidate")

    pol_id = f"pol-refresh-{unique_suffix}"
    r = await client.post("/v1/tool_approval_policies", json={
        "id": pol_id, "toolset_id": "misc", "tool_name": "uuid_v4",
        "enabled": True, "approval": {"type": "required"},
    })
    assert r.status_code in (200, 201), r.text
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text

    try:
        registry, mock_base_url = mock_llm
        scenario = f"scripted:park-{unique_suffix}"
        agent = await make_scripted_agent(
            client, registry, mock_base_url, suffix=unique_suffix, scenario=scenario,
            tools=["misc__uuid_v4"],
            rules=[Rule(when_tool_result=False, emit_tool="misc__uuid_v4", emit_args={})],
        )
        wid = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
        sid = await start_agent_session(client, workspace_id=wid, agent_id=agent["agent_id"])

        deadline = asyncio.get_event_loop().time() + 15.0
        warm = {}
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"/v1/sessions/{sid}")
            warm = r.json()
            if warm.get("session_state") == "parked":
                break
            assert warm.get("status") != "ended", (
                f"session ended instead of parking: {warm}"
            )
            await asyncio.sleep(0.1)
        assert warm.get("session_state") == "parked", warm

        cold = await _cold_snapshot(base_url, sid)
        # turn_no excluded here too - see _DISPLAY_FIELDS' own comment.
        for field in ("status", "session_state", "parked_status"):
            assert cold.get(field) == warm.get(field), (
                f"refresh mismatch on parked session, field {field!r}: "
                f"warm={warm.get(field)!r} cold={cold.get(field)!r}"
            )
    finally:
        # invalidate after delete too, not just after create - closing
        # the gap this test's own docstring documents, rather than
        # leaving misc__uuid_v4 gated for whichever test runs next.
        await client.delete(f"/v1/tool_approval_policies/{pol_id}")
        await client.post("/v1/tool_approval_policies/invalidate")
