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


async def test_a_gone_users_park_is_not_woken(fake_storage_provider):
    """A park whose turn person was deleted or disabled takes no event: its resumed turn would be refused, ending the session failed."""
    sp = fake_storage_provider
    await sp.get_storage(User).create(User(id="u-off", username="off", created_at=NOW, role="user", disabled=True))
    await sp.get_storage(Workspace).create(_ws("ws-team", ["u-lead"]).model_copy(update={"access": Access(owners=["u-lead"], grants={"u-off": "read", "u-del": "read"})}))
    d = EventDispatcher(storage_provider=sp)
    for uid in ("u-off", "u-del"):  # u-del has no user row at all
        ref = PrincipalRef(type="user", id=uid, display=uid, role="user", source="local", user_id=uid)
        row = WorkspaceSession(id=f"se-{uid}", workspace_id="ws-team", binding=AgentSessionBinding(agent_id="operator"),
                               status=SessionStatus.WAITING, created_at=NOW, initiated_by=ref)
        ev = Event(id=9, event_type="session.message_appended", occurred_at=NOW, workspace_id="ws-team", payload={})
        assert await d._read_blocked(row, ev) is True, uid


async def test_a_live_users_park_is_still_woken_by_a_workspace_they_read(fake_storage_provider):
    sp = fake_storage_provider
    await sp.get_storage(User).create(User(id="u-ok", username="ok", created_at=NOW, role="user"))
    await sp.get_storage(Workspace).create(_ws("ws-team", ["u-lead"]).model_copy(update={"access": Access(owners=["u-lead"], grants={"u-ok": "read"})}))
    ref = PrincipalRef(type="user", id="u-ok", display="ok", role="user", source="local", user_id="u-ok")
    row = WorkspaceSession(id="se-ok", workspace_id="ws-team", binding=AgentSessionBinding(agent_id="operator"),
                           status=SessionStatus.WAITING, created_at=NOW, initiated_by=ref)
    ev = Event(id=9, event_type="session.message_appended", occurred_at=NOW, workspace_id="ws-team", payload={})
    assert await EventDispatcher(storage_provider=sp)._read_blocked(row, ev) is False
