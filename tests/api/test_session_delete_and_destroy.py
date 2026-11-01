"""T104 (GR-6 D16, GS-21): the DELETE route decides under the lifecycle lock on a fresh read, and a workspace destroy deletes its sessions."""

from __future__ import annotations

from primer.model.workspace_session import SessionStatus, WorkspaceSession
from tests.api.test_sessions import app, seeded_agent, seeded_workspace, sessions_client  # noqa: F401


async def _new_session(client, workspace, agent) -> str:
    made = await client.post(f"/v1/workspaces/{workspace.id}/sessions", json={"binding": {"kind": "agent", "agent_id": agent.id}})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


async def test_a_steer_that_makes_the_session_running_during_the_delete_gets_409_and_the_lease_survives(
    sessions_client, seeded_workspace, seeded_agent, app, monkeypatch,  # noqa: F811
):
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    leases: list[tuple] = []

    async def delete_lease(*args, **kwargs):
        leases.append(args)

    monkeypatch.setattr(app.state.claim_engine, "delete_lease", delete_lease, raising=False)

    async def a_steer_wins_the_gap(*, call_storage, session_id, reason):
        # What the route awaits between its read and its decision: the session turns RUNNING and a worker claims it.
        await storage.patch_if(session_id, {"status": SessionStatus.RUNNING.value}, where={"status": [SessionStatus.CREATED.value, SessionStatus.WAITING.value]})

    monkeypatch.setattr("primer.session.external_tools.cancel_pending_external", a_steer_wins_the_gap)
    resp = await sessions_client.delete(f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}")

    assert resp.status_code == 409, resp.text
    row = await storage.get(sid)
    assert row is not None and row.status == SessionStatus.RUNNING and row.ended_reason is None
    assert leases == [], "the worker's lease must not be deleted"


async def test_the_delete_route_still_removes_the_session_and_its_canvas(sessions_client, seeded_workspace, seeded_agent, app):  # noqa: F811
    from primer.model.canvas import SessionCanvas

    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    sp = app.state.storage_provider
    await sp.get_storage(SessionCanvas).create(SessionCanvas(id=sid, workspace_id=seeded_workspace.id))
    resp = await sessions_client.delete(f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}")
    assert resp.status_code == 204, resp.text
    assert await sp.get_storage(WorkspaceSession).get(sid) is None
    assert await sp.get_storage(SessionCanvas).get(sid) is None
