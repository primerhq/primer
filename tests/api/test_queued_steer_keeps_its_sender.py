"""A REST steer queued behind an open turn keeps its sender (access-control spec 13 Q1; P2 gap 1).

A steer that finds a turn open is stored to the queue by the route itself. The queued row must carry the person who
sent it (``attribution["by"]``), so the drain that realizes it later arms the turn as that person: a full-access peer's
queued steer must never run as the person the session last ran as (the owner), which would lend the peer the owner's
access. Real routes and auth middleware, as tests/api/test_session_access_routes.py.
"""

from __future__ import annotations

from primer.access.person import person_from_ref
from primer.access.workspaces import workspace_level
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, PendingSessionMessage, SessionStatus, WorkspaceSession
from primer.session.enqueue import SessionWakeDeps
from primer.session.pending_messages import realize_next_pending
from primer.worker.executor_builders import session_run_principal
from tests.api.test_session_access_routes import NOW, SID, WS, _workspace, cast, world  # noqa: F401 - fixtures

PRIVATE_WS = "ws-carol-private"


async def test_a_peers_queued_steer_runs_as_the_peer(world, app, client, cast):  # noqa: F811 - fixtures
    sp = app.state.storage_provider
    carol = PrincipalRef(type="user", id=cast.carol_id, display="carol", role="user", source="local", user_id=cast.carol_id)
    await sp.get_storage(Workspace).create(_workspace(owners=[cast.carol_id], grants={cast.dave_id: "full"}))
    # A workspace only carol reaches: what the turn's tools can see tells whose access the turn runs with.
    await sp.get_storage(Workspace).create(
        _workspace(owners=[cast.carol_id], grants={}).model_copy(update={"id": PRIVATE_WS, "access": Access(owners=[cast.carol_id])})
    )
    # Carol's turn is open: a steer now waits in the queue.
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=SID, workspace_id=WS, binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.RUNNING,
        turn_status="running", turn_no=1, created_at=NOW, initiated_by=carol, turn_person=carol,
    ))

    queued = await cast.dave.post(f"/v1/workspaces/{WS}/sessions/{SID}/steer", json={"instruction": "from dave"})
    assert queued.status_code == 200, queued.text
    pending = (await sp.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=10))).items
    assert len(pending) == 1
    by = (pending[0].attribution or {}).get("by")
    assert by is not None and by["user_id"] == cast.dave_id, f"the queued steer lost its sender: {pending[0].attribution!r}"

    # Carol's turn ends; the drain realizes dave's steer into the next turn.
    row = await sp.get_storage(WorkspaceSession).get(SID)
    await sp.get_storage(WorkspaceSession).update(row.model_copy(update={"status": SessionStatus.WAITING, "turn_status": "idle"}))
    deps = SessionWakeDeps(
        storage_provider=sp, scheduler=app.state.scheduler, claim_engine=getattr(app.state, "claim_engine", None),
        workspace_registry=world, event_bus=None,
    )
    assert await realize_next_pending(storage_provider=sp, workspace_id=WS, session_id=SID, wake_deps=deps)
    realized = await sp.get_storage(WorkspaceSession).get(SID)
    assert realized.turn_person is not None and realized.turn_person.user_id == cast.dave_id, realized.turn_person
    person = person_from_ref(session_run_principal(realized))
    assert person.user_id == cast.dave_id
    assert await workspace_level(sp, person, PRIVATE_WS) is None, "the turn's tools would see carol's private workspace"

