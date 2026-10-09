"""Cancelling a yield names the gate it cancels too (console review C-033, ticket 01a11f52-9d98).

``POST /v1/sessions/{id}/yields/{tool_call_id}/cancel`` is the console's "Skip" on an ask_user prompt, and cancelling an ``_approval`` gate is classified
as a rejection. Both are keyed on the provider's tool_call_id, which repeats across rounds, so a Skip left open for round 1's question cancelled whatever
was pending under the same id later. The body now takes the optional ``gate_id`` the pending response served, with the same rules as the respond routes:
a cancel naming a gate that is no longer the pending one is a 409 ``approval_stale`` that publishes nothing; one naming none is accepted and counted; a
malformed one is a 422. A yield that is not a human gate (sleep, watch_files) has no gate id and is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.api.test_gate_fence import G1, G2, _approval_session, _ask_user_session, _count, _fresh_metrics, _graph_two_gate_session  # noqa: F401


class _Published:
    def __init__(self, bus) -> None:
        self.events: list[tuple[str, dict]] = []
        real = bus.publish

        async def spy(event_key, payload):
            self.events.append((event_key, payload))
            return await real(event_key, payload)

        bus.publish = spy


def _url(session_id: str, tool_call_id: str) -> str:
    return f"/v1/sessions/{session_id}/yields/{tool_call_id}/cancel"


@pytest.mark.asyncio
async def test_a_cancel_naming_the_pending_ask_user_gate_is_accepted(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _ask_user_session(session_id="c-ok", tool_call_id="call_0", gate_id=G1))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("c-ok", "call_0"), json={"reason": "skip", "gate_id": G1})

    assert resp.status_code == 202, resp.text
    assert [k for k, _ in published.events] == ["ask_user:c-ok:call_0"]
    assert _count("ask_user", "matched") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_ask_user_session, _approval_session], ids=["ask_user", "approval"])
async def test_a_stale_cancel_for_a_repeated_raw_id_is_409_and_publishes_nothing(app, client, make):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        make(session_id="c-stale", tool_call_id="call_0", gate_id=G2))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("c-stale", "call_0"), json={"gate_id": G1})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"
    assert published.events == []
    assert _count("ask_user" if make is _ask_user_session else "approval", "stale") == 1


@pytest.mark.asyncio
async def test_a_tokenless_cancel_of_a_human_gate_is_accepted_and_counted(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _ask_user_session(session_id="c-bare", tool_call_id="call_0", gate_id=G1))

    resp = await client.post(_url("c-bare", "call_0"), json={})

    assert resp.status_code == 202, resp.text
    assert _count("ask_user", "absent") == 1


@pytest.mark.asyncio
async def test_a_malformed_gate_id_on_a_cancel_is_422(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _ask_user_session(session_id="c-bad", tool_call_id="call_0", gate_id=G1))

    resp = await client.post(_url("c-bad", "call_0"), json={"gate_id": "nope"})

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_a_yield_that_is_not_a_human_gate_is_unchanged_and_not_counted(app, client):
    now = datetime.now(UTC)
    key = "sleep:c-sleep:call_0"
    await app.state.storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="c-sleep", workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=key,
        parked_state={"tool_call_id": "call_0", "yielded": {"tool_name": "sleep", "event_key": key, "resume_metadata": {"duration_s": 5}}},
    ))

    resp = await client.post(_url("c-sleep", "call_0"), json={})

    assert resp.status_code == 202, resp.text
    assert _count("approval", "absent") == 0 and _count("ask_user", "absent") == 0


@pytest.mark.asyncio
async def test_a_graph_primary_gate_is_judged_by_its_own_entrys_id(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_two_gate_session(session_id="c-graph"))
    published = _Published(app.state.event_bus)

    stale = await client.post(_url("c-graph", "call-0"), json={"gate_id": G2})
    assert stale.status_code == 409, stale.text
    assert published.events == []

    ok = await client.post(_url("c-graph", "call-0"), json={"gate_id": G1})
    assert ok.status_code == 202, ok.text
    assert [k for k, _ in published.events] == ["tool_approval:c-graph:call-0"]
