"""TimeoutSweeper must be loud about a park it can never time out.

Its sweeps select on ``parked_until`` and ``parked_event_key``, so a parked row missing
either was skipped silently: nothing recorded that the session was stuck. No current park
writer produces such a row (``ParkRequest.parked_until`` is typed optional and a test pins
that), so this is a tripwire for a future writer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from primer.bus.scheduler_tasks import TimeoutSweeper
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


class _Bus:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, key, payload=None, **_):
        self.published.append(key)


def _parked(sid, **over):
    base = dict(
        id=sid,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        parked_status="parked",
        parked_event_key=f"tool_approval:{sid}:call_0",
        parked_until=datetime.now(timezone.utc) + timedelta(hours=1),
        parked_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return WorkspaceSession(**base)


def _errors(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.ERROR and "yield-timeout-sweeper" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_a_park_with_no_deadline_is_reported_once(fake_storage_provider, caplog):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked("se-nodeadline", parked_until=None))
    sweeper = TimeoutSweeper(bus=_Bus(), session_storage=storage)
    caplog.set_level(logging.ERROR, logger="primer.bus.scheduler_tasks")

    await sweeper._tick()
    await sweeper._tick()

    msgs = _errors(caplog)
    assert len(msgs) == 1, msgs
    assert "se-nodeadline" in msgs[0] and "parked_until" in msgs[0]


@pytest.mark.asyncio
async def test_a_park_with_no_event_key_is_reported(fake_storage_provider, caplog):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked("se-nokey", parked_event_key=None))
    caplog.set_level(logging.ERROR, logger="primer.bus.scheduler_tasks")

    await TimeoutSweeper(bus=_Bus(), session_storage=storage)._tick()

    msgs = _errors(caplog)
    assert len(msgs) == 1 and "se-nokey" in msgs[0] and "parked_event_key" in msgs[0]


@pytest.mark.asyncio
async def test_an_ordinary_park_is_not_reported(fake_storage_provider, caplog):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked("se-fine"))
    await storage.create(_parked("se-resumable", parked_status="resumable", parked_until=None))
    caplog.set_level(logging.ERROR, logger="primer.bus.scheduler_tasks")

    await TimeoutSweeper(bus=_Bus(), session_storage=storage)._tick()

    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_a_repaired_park_that_breaks_again_is_reported_again(
    fake_storage_provider, caplog,
):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked("se-flaky", parked_until=None))
    sweeper = TimeoutSweeper(bus=_Bus(), session_storage=storage)
    caplog.set_level(logging.ERROR, logger="primer.bus.scheduler_tasks")

    await sweeper._tick()
    row = await storage.get("se-flaky")
    await storage.update(row.model_copy(update={
        "parked_until": datetime.now(timezone.utc) + timedelta(hours=1),
    }))
    await sweeper._tick()
    row = await storage.get("se-flaky")
    await storage.update(row.model_copy(update={"parked_until": None}))
    await sweeper._tick()

    assert len(_errors(caplog)) == 2


@pytest.mark.asyncio
async def test_the_timeout_sweep_itself_is_unchanged(fake_storage_provider):
    """The tripwire adds a report; an expired park is still timed out exactly as before."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked(
        "se-expired", parked_until=datetime.now(timezone.utc) - timedelta(seconds=5),
    ))
    bus = _Bus()

    await TimeoutSweeper(bus=bus, session_storage=storage)._tick()

    assert bus.published == ["tool_approval:se-expired:call_0"]
