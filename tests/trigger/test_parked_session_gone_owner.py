"""A ``parked_session`` subscription whose owner is deleted or disabled wakes nothing (final review F5, the same shape as ``session_append``).

The woken session resumes under its own person, but the delivery is the gone account's: the fire fails closed (``owner_unresolvable``), nothing
is published, and the row is kept (like the other refusals) until its park ends.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.trigger.subscribers.parked_session as ps
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.trigger import ParkedSessionSubConfig, Subscription
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider

NOW = datetime.now(UTC)
OWNER = PrincipalRef(type="user", id="u-gone", display="gone", role="user", source="local", user_id="u-gone")
SESSION, RAW = "s-park", "call_0"


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


@pytest.fixture(autouse=True)
def _no_rank_refusal(monkeypatch):
    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(ps, "refuse_steer_above_fire", _none)


async def _world(sp, *, user: str) -> Subscription:
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-g", template_id="t", provider_id="p", created_at=NOW, runtime_meta=WorkspaceRuntimeMeta(url="ws://g", token="t"),
        access=Access(owners=["u-gone"]),  # the owner still reads it statically: only the account state decides
    ))
    yielded = {"tool_name": "subscribe_to_trigger", "event_key": "trigger:tr-1", "resume_metadata": {"subscription_id": "sb-1", "trigger_id": "tr-1"}}
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=SESSION, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING, created_at=NOW,
        parked_status="parked", parked_at=NOW, parked_event_key="trigger:tr-1", parked_state={"tool_call_id": RAW, "yielded": yielded},
    ))
    if user == "disabled":
        await sp.get_storage(User).create(User(id="u-gone", username="gone", created_at=NOW, role="user", disabled=True))
    sub = Subscription(
        id="sb-1", trigger_id="tr-1", created_at=NOW, owner=OWNER,
        config=ParkedSessionSubConfig(session_id=SESSION, tool_call_id=RAW, parked_at=NOW),
    )
    await sp.get_storage(Subscription).create(sub)
    return sub


@pytest.mark.parametrize("user", ["deleted", "disabled"])
async def test_a_gone_owners_park_subscription_does_not_wake_the_session(user):
    sp, bus = _FakeStorageProvider(), _Bus()
    sub = await _world(sp, user=user)

    result = await ps.ParkedSessionDispatcher().dispatch(
        sub, rendered_payload="", fire_context={}, fire_id="f-1", deps=DispatchDeps(storage_provider=sp, claim_engine=None, event_bus=bus),
    )

    assert (result.ok, result.error_code) == (False, "owner_unresolvable"), result
    assert bus.published == []
    assert await sp.get_storage(Subscription).get("sb-1") is not None
    assert (await sp.get_storage(WorkspaceSession).get(SESSION)).parked_status == "parked"
