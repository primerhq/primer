"""The event dispatcher's delivery re-check judges the parked session's person by the role they hold NOW (final review F4)."""
from __future__ import annotations

from datetime import UTC, datetime

from primer.events.dispatcher import EventDispatcher
from primer.model.access import Access
from primer.model.event import Event
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def _ws(wid, owners):
    return Workspace(id=wid, template_id="t", provider_id="p", created_at=NOW, runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"),
                     access=Access(owners=owners))


async def test_a_demoted_admins_park_is_not_woken_by_a_workspace_they_cannot_read(fake_storage_provider):
    sp = fake_storage_provider
    await sp.get_storage(User).create(User(id="u-ex", username="ex", created_at=NOW, role="user"))  # demoted since
    await sp.get_storage(Workspace).create(_ws("ws-ex", ["u-ex"]))
    await sp.get_storage(Workspace).create(_ws("ws-hr", ["u-other"]))  # private; the ex-admin cannot read it now
    ref = PrincipalRef(type="user", id="u-ex", display="ex", role="admin", source="local", user_id="u-ex")
    row = WorkspaceSession(id="se-ex", workspace_id="ws-ex", binding=AgentSessionBinding(agent_id="operator"),
                           status=SessionStatus.WAITING, created_at=NOW, initiated_by=ref, turn_person=ref)
    ev = Event(id=7, event_type="session.message_appended", occurred_at=NOW, workspace_id="ws-hr", payload={"text": "salary table"})
    assert await EventDispatcher(storage_provider=sp)._read_blocked(row, ev) is True
