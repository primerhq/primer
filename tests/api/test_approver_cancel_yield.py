"""Cancelling a yield cannot reject a gate the caller may not decide (follow-up of #536).

``POST /v1/sessions/{id}/yields/{tcid}/cancel`` publishes a cancel payload to ANY park's event key, including an ``_approval`` gate, where
``classify_approval_payload`` turns it into a REJECTION. So any user could reject a gate routed to alice, whatever the policy said. The route
now judges an approval gate by the same ``may_decide`` as the respond route (403 ``approver_mismatch``); every other kind of yield is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.api.test_approver_routing import _login_user, _parked_session, _register_admin


class _Published:
    """Records what the cancel route puts on the event bus."""

    def __init__(self, bus) -> None:
        self.events: list[tuple[str, dict]] = []
        real = bus.publish

        async def spy(event_key, payload):
            self.events.append((event_key, payload))
            return await real(event_key, payload)

        bus.publish = spy


def _cancel_url(session_id: str, tool_call_id: str) -> str:
    return f"/v1/sessions/{session_id}/yields/{tool_call_id}/cancel"


@pytest.mark.asyncio
async def test_a_user_who_may_not_decide_an_approval_gate_cannot_reject_it_by_cancelling_its_yield(client, app) -> None:
    await _register_admin(client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked_session(session_id="cy-1", tool_call_id="tc-1", approvers={"kind": "users", "users": ["alice"]}))
    published = _Published(app.state.event_bus)

    await _login_user(client, app, "bob")
    refused = await client.post(_cancel_url("cy-1", "tc-1"), json={"reason": "no"})

    assert refused.status_code == 403, f"bob rejected a gate routed to alice by cancelling it: {refused.text}"
    assert refused.json()["extensions"]["error"] == "approver_mismatch"
    assert published.events == [], "a refused cancel reached the event bus"

    await _login_user(client, app, "alice")
    accepted = await client.post(_cancel_url("cy-1", "tc-1"), json={"reason": "no"})
    assert accepted.status_code == 202, accepted.text
    assert [key for key, _ in published.events] == ["tool_approval:cy-1:tc-1"]


@pytest.mark.asyncio
async def test_an_unrestricted_approval_gate_can_still_be_cancelled_by_any_user(client, app) -> None:
    await _register_admin(client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _parked_session(session_id="cy-2", tool_call_id="tc-2", approvers=None),
    )

    await _login_user(client, app, "carol")
    accepted = await client.post(_cancel_url("cy-2", "tc-2"), json={})

    assert accepted.status_code == 202, accepted.text


@pytest.mark.asyncio
async def test_cancelling_a_yield_that_is_not_an_approval_is_unchanged(client, app) -> None:
    """An ask_user park has no approver routing: any user can cancel it, as before."""
    await _register_admin(client)
    now = datetime.now(UTC)
    event_key = "ask_user:cy-3:tc-3"
    await app.state.storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="cy-3", workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=event_key,
        parked_state={
            "tool_call_id": "tc-3",
            "yielded": {"tool_name": "ask_user", "event_key": event_key, "resume_metadata": {"prompt": "?"}},
        },
    ))

    await _login_user(client, app, "bob")
    accepted = await client.post(_cancel_url("cy-3", "tc-3"), json={})

    assert accepted.status_code == 202, accepted.text
