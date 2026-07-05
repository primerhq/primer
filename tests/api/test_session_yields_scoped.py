"""REST tests for GET /v1/workspaces/{wid}/sessions/{sid}/yields/pending.

Session-scoped variant of the aggregated ``/workspaces/{wid}/yields/pending``
(covered by ``tests/api/test_workspace_yields_pending.py``): returns the
pending yield for a single session so the run-view can render Approve/Deny /
respond affordances inline in the session's own stream (studio-agents-interact
§5.4, Task 10). Mirrors ``tests/api/test_session_interrupt.py``'s convention:
seed a ``WorkspaceSession`` row directly into ``fake_storage_provider`` and
drive the shared ``client``/``app`` fixtures from ``tests/api/conftest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest


def _now() -> datetime:
    return datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Ctx:
    workspace_id: str
    session_id: str


async def _seed_session(
    fake_storage_provider,
    *,
    sid: str,
    wid: str,
    status,
    parked_status: str | None = None,
    parked_state: dict | None = None,
    parked_at: datetime | None = None,
):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        created_at=_now(),
        parked_status=parked_status,
        parked_state=parked_state,
        parked_at=parked_at,
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)
    return sess


@pytest.fixture
async def parked_session_client(client, app, fake_storage_provider):
    """A workspace with a session parked on ``ask_user``."""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-yields-scoped", "s-parked"
    await _seed_session(
        fake_storage_provider,
        sid=sid,
        wid=wid,
        status=SessionStatus.WAITING,
        parked_status="parked",
        parked_at=_now(),
        parked_state={
            "tool_call_id": "tcid-ask-1",
            "yielded": {
                "tool_name": "ask_user",
                "event_key": f"ask_user:{sid}:tcid-ask-1",
                "resume_metadata": {"prompt": "What is your name?"},
            },
        },
    )
    yield client, _Ctx(workspace_id=wid, session_id=sid)


@pytest.fixture
async def running_session_client(client, app, fake_storage_provider):
    """A workspace with a RUNNING (not parked) session."""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-yields-scoped-running", "s-running"
    await _seed_session(fake_storage_provider, sid=sid, wid=wid, status=SessionStatus.RUNNING)
    yield client, _Ctx(workspace_id=wid, session_id=sid)


async def test_session_scoped_pending_yields(parked_session_client):
    client, ctx = parked_session_client  # a session parked on ask_user
    resp = await client.get(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/yields/pending"
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["session_id"] == ctx.session_id
    assert items[0]["kind"] in ("ask_user", "approval", "watch_files", "sleep")
    assert items[0]["prompt"] == "What is your name?"
    assert items[0]["tool_call_id"] == "tcid-ask-1"


async def test_session_scoped_pending_yields_empty_when_running(running_session_client):
    client, ctx = running_session_client
    resp = await client.get(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/yields/pending"
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_session_scoped_pending_yields_404_for_wrong_workspace(parked_session_client):
    """A session id that exists but belongs to a different workspace 404s."""
    client, ctx = parked_session_client
    resp = await client.get(
        f"/v1/workspaces/not-{ctx.workspace_id}/sessions/{ctx.session_id}/yields/pending"
    )
    assert resp.status_code == 404


async def test_session_scoped_pending_yields_404_for_unknown_session(client):
    resp = await client.get(
        "/v1/workspaces/ws-yields-scoped-unknown/sessions/no-such-session/yields/pending"
    )
    assert resp.status_code == 404
