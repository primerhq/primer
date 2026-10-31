"""A turn runs only while its person still holds full on the session's workspace (access-control P2 gap 2; Review Focus 1).

A grant revoked after a peer's steer was queued must stop the turn that steer starts: the worker re-checks the turn
person's level on the session's workspace when it builds the executor, and refuses the run (``WorkspaceAccessRevoked``,
a ``RunRefused``) when it is below full. The real queue (``store_pending_steer`` then ``realize_next_pending``) arms the
turn as the peer; the real ``build_agent_executor`` refuses it. The owner's turn, an admin's and a pool with no storage
still build.
"""

from __future__ import annotations

import pytest

from primer.authz import RunRefused, WorkspaceAccessRevoked
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus
from primer.session.pending_messages import realize_next_pending, store_pending_steer
from tests.session._peer_fakes import make_world, seed_session, seed_workspace
from tests.worker.test_run_principal_current_role import _pool, _user, _Workspace

CAROL = PrincipalRef(type="user", id="u-carol", display="carol", role="user", source="local", user_id="u-carol")
DAVE = PrincipalRef(type="user", id="u-dave", display="dave", role="user", source="local", user_id="u-dave")
AG1 = AgentSessionBinding(agent_id="ag-1")
ADA = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local", user_id="u-ada")


async def _world(monkeypatch):
    world = make_world(with_bus=True)
    await _user(world.sp, "u-carol", "user")
    await _user(world.sp, "u-dave", "user")
    await _user(world.sp, "u-ada", "admin")
    await seed_workspace(world, owners=["u-carol"], grants={"u-dave": "full"})
    pool = await _pool(monkeypatch, world.sp, [])
    return world, pool


async def _revoke(world, grants: dict[str, str]) -> None:
    row = await world.sp.get_storage(Workspace).get("ws-1")
    await world.sp.get_storage(Workspace).update(row.model_copy(update={"access": Access(owners=["u-carol"], grants=grants)}))


async def test_a_grant_revoked_while_the_peers_steer_is_queued_refuses_the_turn(monkeypatch):
    world, pool = await _world(monkeypatch)
    row = await seed_session(
        world, sid="ses-1", status=SessionStatus.RUNNING, turn_status="running", binding=AG1, initiated_by=CAROL, turn_person=CAROL,
    )
    await store_pending_steer(
        storage_provider=world.sp, session=row, text="from dave", workspace_registry=world.registry, event_bus=world.bus, by=DAVE,
    )
    await _revoke(world, {})  # carol takes dave's grant away while his steer waits
    await world.sessions.update(row.model_copy(update={"status": SessionStatus.WAITING, "turn_status": "idle"}))
    assert await realize_next_pending(storage_provider=world.sp, workspace_id="ws-1", session_id="ses-1", wake_deps=world.wake_deps())
    armed = await world.sessions.get("ses-1")
    assert armed.turn_person == DAVE

    with pytest.raises(WorkspaceAccessRevoked) as refused:
        await pool._build_agent_executor(armed, _Workspace("ses-1"))
    assert isinstance(refused.value, RunRefused), "the existing refused-run exits must handle it"
    assert "no longer has full access" in str(refused.value)


@pytest.mark.parametrize("grant", ["read", None])
async def test_a_turn_person_below_full_is_refused(monkeypatch, grant):
    world, pool = await _world(monkeypatch)
    await _revoke(world, {"u-dave": grant} if grant else {})
    session = await seed_session(world, sid="ses-1", binding=AG1, initiated_by=CAROL, turn_person=DAVE)
    with pytest.raises(WorkspaceAccessRevoked):
        await pool._build_agent_executor(session, _Workspace("ses-1"))


@pytest.mark.parametrize("person", [CAROL, DAVE, ADA], ids=["owner", "full", "admin"])
async def test_a_turn_person_with_full_or_admin_builds(monkeypatch, person):
    world, pool = await _world(monkeypatch)
    session = await seed_session(world, sid="ses-1", binding=AG1, initiated_by=CAROL, turn_person=person)
    assert await pool._build_agent_executor(session, _Workspace("ses-1"))


async def test_a_resume_after_the_grant_was_revoked_ends_with_the_access_record(monkeypatch):
    """An approval answered after the turn person lost the grant does not resume: the session ends failed and says why in fixed words."""
    import json

    from primer.authz import WORKSPACE_ACCESS_REVOKED_CODE, WORKSPACE_ACCESS_REVOKED_MESSAGE
    from tests.worker.test_run_principal_current_role import _parked_on_an_approval, _resume

    world, pool = await _world(monkeypatch)
    workspace = _Workspace("ses-1")

    class _Registry:
        async def get_workspace(self, _wid):
            return workspace

        async def check_workspace_allowed(self, _wid):
            return None

    pool._workspace_registry = _Registry()
    await world.sessions.create(_parked_on_an_approval("ses-1").model_copy(update={"initiated_by": CAROL, "turn_person": DAVE}))
    await _revoke(world, {})

    assert await _resume(monkeypatch, world.sp, pool, "ses-1") == []
    row = await world.sessions.get("ses-1")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.last_turn_error is not None and row.last_turn_error.code == WORKSPACE_ACCESS_REVOKED_CODE
    records = [json.loads(line) for blob in workspace.lines for line in blob.decode().splitlines() if line.strip()]
    assert {"message": WORKSPACE_ACCESS_REVOKED_MESSAGE, "code": WORKSPACE_ACCESS_REVOKED_CODE} in [
        r["payload"] for r in records if r["kind"] == "error"
    ]
