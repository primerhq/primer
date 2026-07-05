"""REST tests for POST /v1/workspaces/{wid}/sessions/{sid}/interrupt.

Interrupt (Stop) preempts the in-flight turn but leaves the session ALIVE
(WAITING) for the next message — distinct from Cancel (which ends the run).
Mirrors ``tests/api/test_session_restart.py``'s convention: seed a
``WorkspaceSession`` row directly into ``fake_storage_provider`` and drive
the shared ``client``/``app`` fixtures from ``tests/api/conftest.py``. The
interrupt route only touches session storage + the event bus (no workspace
registry lookups), so no fake workspace is needed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Ctx:
    workspace_id: str
    session_id: str


async def _seed_session(fake_storage_provider, *, sid: str, wid: str, status):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason="completed" if status == SessionStatus.ENDED else None,
        ended_at=_now() if status == SessionStatus.ENDED else None,
        last_seq=1,
        turn_status="running" if status == SessionStatus.RUNNING else "idle",
        created_at=_now(),
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(sess)
    return sess


@pytest.fixture
async def running_session_client(client, app, fake_storage_provider):
    """A workspace with a RUNNING agent session."""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-interrupt", "s-running"
    await _seed_session(fake_storage_provider, sid=sid, wid=wid, status=SessionStatus.RUNNING)
    yield client, _Ctx(workspace_id=wid, session_id=sid)


@pytest.fixture
async def ended_session_client(client, app, fake_storage_provider):
    """A workspace with an ENDED/completed agent session."""
    from primer.model.workspace_session import SessionStatus

    wid, sid = "ws-interrupt-ended", "s-ended"
    await _seed_session(fake_storage_provider, sid=sid, wid=wid, status=SessionStatus.ENDED)
    yield client, _Ctx(workspace_id=wid, session_id=sid)


async def test_interrupt_running_session_sets_flag(running_session_client):
    client, ctx = running_session_client
    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt",
        json={},
    )
    assert resp.status_code == 200, resp.text
    # The row records the interrupt request for the worker to observe.
    got = await client.get(f"/v1/sessions/{ctx.session_id}")
    assert got.json()["interrupt_requested"] is True


async def test_interrupt_ended_session_409(ended_session_client):
    client, ctx = ended_session_client
    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt",
        json={},
    )
    assert resp.status_code == 409
