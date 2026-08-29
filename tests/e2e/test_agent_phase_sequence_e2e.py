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

from tests._support.mock_llm import slow_turn_with_mid_stream_tool_call
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


@pytest.mark.asyncio
async def test_refresh_mid_phase_recovers_from_a_cold_client(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path, base_url: str,
):
    """The exact scenario 01a04d64-b4ba's live diagnosis needed and
    never got a real turn long enough to observe: a SECOND, independently
    authenticated client - zero shared connection, zero live tap/websocket
    history, the same condition a browser refresh creates - must read the
    CORRECT current agent_phase directly off the session row while the
    first client's turn is still genuinely mid-flight."""
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

    # Wait until the FIRST client observes "executing" (the tool call),
    # then immediately probe with a brand-new client with no prior
    # requests against this session at all.
    deadline = asyncio.get_event_loop().time() + 10.0
    seen_executing = False
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.json().get("agent_phase") == "executing":
            seen_executing = True
            break
        await asyncio.sleep(0.05)
    assert seen_executing, "turn never reached the tool call in time"

    import contextlib
    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0),
    ) as cold_client:
        with contextlib.suppress(Exception):
            await cold_client.post("/v1/auth/register", json=_E2E_USER)
            await cold_client.post("/v1/auth/login", json=_E2E_USER)
        r = await cold_client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200, r.text
        cold_body = r.json()
        # Recovered entirely from the row - no live frame, no prior
        # request on this connection, exactly the refresh condition.
        assert cold_body["session_state"] == "running", cold_body
        assert cold_body["agent_phase"] in ("executing", "thinking", "responding"), (
            "a cold read mid-turn must see SOME real phase, not None/idle "
            f"leftover: {cold_body}"
        )
        assert cold_body["agent_phase_turn_no"] == cold_body["turn_no"], cold_body

    # Let the turn actually finish so mock_llm/workspace teardown (fixture
    # scope) doesn't race a still-in-flight turn.
    await _poll_phase_samples(client, sid, timeout_s=10.0)
