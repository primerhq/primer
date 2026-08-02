"""StuckSessionSweeper — ends sessions whose first turn never ran.

Nothing else reaps these: TimeoutSweeper only handles parks (a turn that started and is
waiting), and cancel just sets a flag a worker reads on its next step. A session that never
started has no worker, so it stays non-terminal forever — and a `parallelism="skip"`
subscription will not fire while any attributed session is non-terminal, so one stuck row
silently halts its trigger.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.scheduler_tasks import StuckSessionSweeper
from primer.model.workspace_session import (
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


def _session(sid, *, age_seconds=3600, turn_no=0, status=SessionStatus.RUNNING, **over):
    base = dict(
        id=sid,
        workspace_id="ws-1",
        binding=GraphSessionBinding(graph_id="g-1"),
        status=status,
        turn_status="idle",
        turn_no=turn_no,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )
    base.update(over)
    return WorkspaceSession(**base)


@pytest.mark.asyncio
async def test_ends_a_session_whose_first_turn_never_ran(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-stuck"))

    reaped = await StuckSessionSweeper(session_storage=storage)._tick()

    assert reaped == 1
    row = await storage.get("se-stuck")
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "failed"
    assert row.ended_detail == "never_started"
    assert row.ended_at is not None


@pytest.mark.asyncio
async def test_leaves_a_running_turn_alone_however_long_it_runs(fake_storage_provider):
    """The bound is on NEVER STARTING, never on duration.

    A started turn may legitimately take hours (a graph build, a long exec). Ending it from
    under the worker would be far worse than leaving it.
    """
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(
        _session("se-working", age_seconds=86_400, turn_no=4, turn_status="running")
    )

    reaped = await StuckSessionSweeper(session_storage=storage)._tick()

    assert reaped == 0
    assert (await storage.get("se-working")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_leaves_a_freshly_created_session_alone(fake_storage_provider):
    """Inside the grace window the claim is simply still in flight."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-fresh", age_seconds=5))

    reaped = await StuckSessionSweeper(session_storage=storage)._tick()

    assert reaped == 0
    assert (await storage.get("se-fresh")).status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_ignores_already_ended_sessions(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(
        _session("se-done", status=SessionStatus.ENDED, ended_reason="completed")
    )

    assert await StuckSessionSweeper(session_storage=storage)._tick() == 0
    assert (await storage.get("se-done")).ended_reason == "completed"


@pytest.mark.asyncio
async def test_grace_is_configurable(fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_session("se-recent", age_seconds=30))

    assert await StuckSessionSweeper(
        session_storage=storage, grace_seconds=3600,
    )._tick() == 0
    assert await StuckSessionSweeper(
        session_storage=storage, grace_seconds=10,
    )._tick() == 1
