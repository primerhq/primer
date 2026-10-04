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


async def test_interrupt_publish_failure_is_still_200_but_is_logged_and_counted(
    running_session_client, app, monkeypatch, caplog,
):
    """The Stop is recorded on the row BEFORE the publish, and the worker that runs the turn polls the
    row, so a failed publish delays the Stop instead of losing it: 200 is honest. It must not be
    silent, though: the cause is logged and counted, so a bus that drops Stops is visible."""
    import logging

    import primer.observability.metrics as metrics

    metrics.reset_for_test()

    async def broken_publish(*_a, **_k):
        raise RuntimeError("bus is down")

    monkeypatch.setattr(app.state.event_bus, "publish", broken_publish)
    client, ctx = running_session_client

    with caplog.at_level(logging.WARNING, logger="primer.api.routers.workspaces"):
        resp = await client.post(
            f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt", json={},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["interrupt_requested"] is True
    assert metrics.session_interrupt_publish_failures_total._value.get() == 1.0
    assert any(
        ctx.session_id in r.getMessage() and "bus is down" in r.getMessage()
        for r in caplog.records
    ), "the failed publish must name the session and the cause"


async def test_interrupt_publish_success_counts_no_failure(running_session_client):
    import primer.observability.metrics as metrics

    metrics.reset_for_test()
    client, ctx = running_session_client

    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt", json={},
    )

    assert resp.status_code == 200
    assert metrics.session_interrupt_publish_failures_total._value.get() == 0.0


@pytest.mark.parametrize("parked_status", ["parked", "resumable"])
async def test_interrupt_on_a_parked_session_is_refused_and_records_nothing(
    running_session_client, fake_storage_provider, parked_status,
):
    """No turn is running while a session is parked (it waits for a human decision, an answer or a
    timer), so there is nothing for a Stop to stop. Recording the flag anyway would let it outlive
    the park and kill the continuation after a later approval: Stop while parked on an approval,
    approve hours later, the approved tool RUNS, then the continuation is killed before its first
    token. A later explicit human action wins over an earlier Stop, so the Stop is refused."""
    from primer.model.workspace_session import WorkspaceSession

    client, ctx = running_session_client
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(ctx.session_id)
    row.parked_status = parked_status
    await sessions.update(row)

    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt", json={},
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json().get("detail", "")
    assert "Cancel" in detail, detail
    if parked_status == "parked":
        assert "no turn is running" in detail, detail
    else:
        # A graph or subagent resume runs model calls inline, so "no turn is running" would be false.
        assert "no turn is running" not in detail, detail
        assert "resuming" in detail and "Stop is not available during a resume" in detail, detail
    after = await sessions.get(ctx.session_id)
    assert after.interrupt_requested is False, "a refused Stop must not leave a flag behind"


async def test_interrupt_on_a_queued_session_is_still_recorded(running_session_client, fake_storage_provider):
    """Queued, not parked: the next turn is about to run, so a Stop recorded now is honoured by the
    first poll of that turn (the behaviour this slice keeps)."""
    from primer.model.workspace_session import WorkspaceSession

    client, ctx = running_session_client
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(ctx.session_id)
    row.turn_status = "claimable"
    await sessions.update(row)

    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt", json={},
    )

    assert resp.status_code == 200, resp.text
    assert (await sessions.get(ctx.session_id)).interrupt_requested is True


async def test_interrupt_ended_session_409(ended_session_client):
    client, ctx = ended_session_client
    resp = await client.post(
        f"/v1/workspaces/{ctx.workspace_id}/sessions/{ctx.session_id}/interrupt",
        json={},
    )
    assert resp.status_code == 409
