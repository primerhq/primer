"""How a session ended has ONE truth, the durable row, on every route that tells it (architecture review A1).

RUN on k3s (sess-852add518082): ``GET /v1/sessions/{sid}`` said ended / ``failed`` / ``never_started`` (the row), while
``GET /v1/workspaces/{wid}/sessions/{sid}`` said ended / ``completed`` with no detail. The second route serves the runtime-side
slot (``.state/sessions/<sid>/session.json``) as it is, with no look at the row, and the slot can disagree with the row in three
ways, each pinned below: it can say ``completed`` for a row that ended otherwise (the dispatch mirror writes ``completed`` for any
reason it does not know, and a closed workspace handle ends every cached slot as ``completed``); it can still say ``running`` for a
row that was ended by a path that never touches the slot (the stuck-session sweeper); and it can say ``ended`` for a row that is
alive (a handle closed under a parked session). The row wins in every case: a slot is only what is left of the session on disk.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import SessionStatus, WorkspaceSession

# Re-export so pytest can resolve the fixtures (fake workspace backend, seeded workspace and agent, client).
from tests.api.test_sessions import app, seeded_agent, seeded_workspace, sessions_client  # noqa: F401

SLOT_ENDED_AT = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
ROW_ENDED_AT = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)


async def _new_session(sessions_client, seeded_workspace, seeded_agent) -> str:
    resp = await sessions_client.post(
        f"/v1/workspaces/{seeded_workspace.id}/sessions",
        json={"binding": {"kind": "agent", "agent_id": seeded_agent.id}},
    )
    assert resp.status_code < 300, resp.text
    return resp.json()["id"]


async def _row_says(app, sid: str, **fields) -> None:
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    await storage.update(row.model_copy(update=fields))


async def _slot_says(app, workspace_id: str, sid: str, **fields) -> None:
    slot = await (await app.state.workspace_registry.get_workspace(workspace_id)).get_session(sid)
    slot._info = slot._info.model_copy(update=fields)


def _ended_failed_never_started() -> dict:
    return {
        "status": SessionStatus.ENDED, "ended_reason": "failed", "ended_detail": "never_started", "ended_at": ROW_ENDED_AT,
    }


async def _every_route(sessions_client, workspace_id: str, sid: str) -> dict[str, dict]:
    """What each route says about how the session ended, normalised to the same three keys."""
    row = (await sessions_client.get(f"/v1/sessions/{sid}")).json()
    nested = (await sessions_client.get(f"/v1/workspaces/{workspace_id}/sessions/{sid}")).json()
    listed = next(
        i for i in (await sessions_client.get(f"/v1/workspaces/{workspace_id}/sessions")).json()["items"]
        if i["session_id"] == sid
    )
    keys = ("status", "ended_reason", "ended_detail")
    return {
        "GET /v1/sessions/{sid}": {k: row.get(k) for k in keys},
        "GET /v1/workspaces/{wid}/sessions/{sid} (status)": {
            "status": nested["status"], "ended_reason": nested["info"].get("ended_reason"),
            "ended_detail": nested["info"].get("ended_detail"),
        },
        "GET /v1/workspaces/{wid}/sessions/{sid} (info)": {k: nested["info"].get(k) for k in keys},
        "GET /v1/workspaces/{wid}/sessions (item)": {k: listed.get(k) for k in keys},
    }


@pytest.mark.asyncio
async def test_a_slot_that_says_completed_does_not_outvote_a_row_that_says_failed(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    await _row_says(app, sid, **_ended_failed_never_started())
    await _slot_says(app, seeded_workspace.id, sid, status=SessionStatus.ENDED, ended_reason="completed", ended_at=SLOT_ENDED_AT)

    views = await _every_route(sessions_client, seeded_workspace.id, sid)

    expected = {"status": "ended", "ended_reason": "failed", "ended_detail": "never_started"}
    assert views == {route: expected for route in views}


@pytest.mark.asyncio
async def test_a_slot_still_running_under_an_ended_row_reads_ended(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    """The stuck-session sweeper ends the row and never touches the slot."""
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    await _row_says(app, sid, **_ended_failed_never_started())

    views = await _every_route(sessions_client, seeded_workspace.id, sid)

    expected = {"status": "ended", "ended_reason": "failed", "ended_detail": "never_started"}
    assert views == {route: expected for route in views}


@pytest.mark.asyncio
async def test_a_slot_that_says_ended_does_not_end_a_live_row(
    sessions_client, seeded_workspace, seeded_agent, app,
):
    """Closing a workspace handle ends every cached slot as completed; the row of a parked session is still waiting."""
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    await _row_says(app, sid, status=SessionStatus.WAITING)
    await _slot_says(app, seeded_workspace.id, sid, status=SessionStatus.ENDED, ended_reason="completed", ended_at=SLOT_ENDED_AT)

    views = await _every_route(sessions_client, seeded_workspace.id, sid)

    expected = {"status": "waiting", "ended_reason": None, "ended_detail": None}
    assert views == {route: expected for route in views}
    nested = (await sessions_client.get(f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}")).json()
    assert nested["info"]["ended_at"] is None, "a session that has not ended has no end time"


@pytest.mark.asyncio
async def test_the_end_time_is_the_rows(sessions_client, seeded_workspace, seeded_agent, app):
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    await _row_says(app, sid, **_ended_failed_never_started())
    await _slot_says(app, seeded_workspace.id, sid, status=SessionStatus.ENDED, ended_reason="completed", ended_at=SLOT_ENDED_AT)

    nested = (await sessions_client.get(f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}")).json()

    assert datetime.fromisoformat(nested["info"]["ended_at"]) == ROW_ENDED_AT


@pytest.mark.asyncio
async def test_when_the_slot_and_the_row_agree_nothing_changes(sessions_client, seeded_workspace, seeded_agent, app):
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    await _row_says(app, sid, status=SessionStatus.ENDED, ended_reason="completed", ended_at=ROW_ENDED_AT)
    await _slot_says(app, seeded_workspace.id, sid, status=SessionStatus.ENDED, ended_reason="completed", ended_at=ROW_ENDED_AT)

    views = await _every_route(sessions_client, seeded_workspace.id, sid)

    expected = {"status": "ended", "ended_reason": "completed", "ended_detail": None}
    assert views == {route: expected for route in views}


@pytest.mark.asyncio
async def test_a_slot_with_no_row_is_served_as_it_is(sessions_client, seeded_workspace, app):
    """An on-disk-only session has nothing to be derived from."""
    ws = await app.state.workspace_registry.get_workspace(seeded_workspace.id)
    await ws.start_session({"kind": "agent", "agent_id": "ag1"}, id="slot-only")
    await _slot_says(app, seeded_workspace.id, "slot-only", status=SessionStatus.ENDED, ended_reason="cancelled", ended_at=SLOT_ENDED_AT)

    nested = await sessions_client.get(f"/v1/workspaces/{seeded_workspace.id}/sessions/slot-only")

    assert nested.status_code == 200, nested.text
    assert nested.json()["status"] == "ended" and nested.json()["info"]["ended_reason"] == "cancelled"


@pytest.mark.asyncio
async def test_a_row_of_another_workspace_is_not_applied(sessions_client, seeded_workspace, seeded_agent, app):
    sid = await _new_session(sessions_client, seeded_workspace, seeded_agent)
    await _row_says(app, sid, workspace_id="some-other-workspace", **_ended_failed_never_started())

    nested = (await sessions_client.get(f"/v1/workspaces/{seeded_workspace.id}/sessions/{sid}")).json()

    assert nested["status"] == "running" and nested["info"]["ended_reason"] is None
