"""What a ``max_tool_turns`` trip leaves behind at the session level: a RESTING row, never a RUNNING one.

The cap trip's last model event is ``Done(stop_reason="tool_use")``, and ``tool_use`` maps to RUNNING ("the executor will
queue the next turn itself"). Nothing queues one after a cap trip, so the row came to rest RUNNING with no lease. Boot
recovery (``recover_sessions``) re-arms EVERY RUNNING row, so on each restart or redeploy the session resumed the model
with no user input, ran up to ``max_tool_turns - 1`` more rounds (possibly destructive), tripped the cap again and rested
RUNNING again. The safety cap had become "pause until the next deploy".

The executor now reports the trip as ``last_done_reason == "tool_turn_cap"``; an interactive session rests WAITING (the
user can send another message) and an autonomous one ENDS with ``ended_reason="tool_turn_cap"`` (nobody is there to resume
it). A resting WAITING/idle row is not re-armed at boot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from primer.api._app_lifespan_phases import recover_sessions
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, _post_turn_status, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)


class _CapTrippedExecutor:
    """The executor's side of a cap trip: the last model event was tool_use, and the executor publishes the trip."""

    last_done_reason = "tool_turn_cap"

    async def invoke(self, messages: list[Any], **kwargs: Any):
        yield TextDelta(text="working on it", index=0)
        yield Done(stop_reason="tool_use", raw_reason="tool_use")


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[str] = []

    async def upsert(self, kind: Any, entity_id: str) -> None:
        self.upserted.append(entity_id)


class _RecordingScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


async def _run_turn(storage, io, bus, executor, sid: str):
    async def build(_session: WorkspaceSession):
        return executor

    deps = SessionDispatchDeps(storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build)
    return await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 3.0)


class TestThePostTurnStatusOfACapTrip:
    def test_an_interactive_session_rests_waiting(self) -> None:
        assert _post_turn_status("tool_turn_cap", None, autonomous=False) == (SessionStatus.WAITING, None)

    def test_an_autonomous_session_ends_with_its_own_reason(self) -> None:
        assert _post_turn_status("tool_turn_cap", None, autonomous=True) == (SessionStatus.ENDED, "tool_turn_cap")

    def test_a_plain_tool_use_still_leaves_the_session_running(self) -> None:
        """The mapping the cap used to fall into is unchanged for a turn that really is mid-chain."""
        assert _post_turn_status("tool_use", None, autonomous=False) == (SessionStatus.RUNNING, None)

    def test_an_executor_set_end_still_wins_over_the_cap(self) -> None:
        status, reason = _post_turn_status("tool_turn_cap", SessionStatus.ENDED, autonomous=False)
        assert status == SessionStatus.ENDED


class TestACapTripThroughTheDispatch:
    async def test_an_interactive_session_rests_waiting_and_is_not_rearmed_at_boot(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id

        outcome = await _run_turn(fake_storage_provider, fake_workspace_io, fake_event_bus, _CapTrippedExecutor(), sid)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert outcome.success is True
        assert row.status == SessionStatus.WAITING and row.ended_reason is None, (
            f"a cap trip left the session {row.status!r}: RUNNING is re-armed at every boot"
        )
        engine, scheduler = _RecordingClaimEngine(), _RecordingScheduler()
        await recover_sessions(engine, scheduler, fake_storage_provider)
        assert engine.upserted == [] and scheduler.enqueued == [], "boot recovery re-armed the capped session"

    async def test_an_autonomous_session_ends_and_is_not_rearmed_at_boot(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        sessions = fake_storage_provider.get_storage(WorkspaceSession)
        row = await sessions.get(sid)
        row.autonomous = True
        await sessions.update(row)

        await _run_turn(fake_storage_provider, fake_workspace_io, fake_event_bus, _CapTrippedExecutor(), sid)

        row = await sessions.get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "tool_turn_cap"
        engine, scheduler = _RecordingClaimEngine(), _RecordingScheduler()
        await recover_sessions(engine, scheduler, fake_storage_provider)
        assert engine.upserted == [] and scheduler.enqueued == []

    async def test_without_the_signal_the_old_mapping_leaves_the_session_running_and_rearmed(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """The control that shows why the signal matters: the same turn reported as a plain tool_use rests RUNNING,
        and boot recovery re-arms it."""

        class _PlainToolUse(_CapTrippedExecutor):
            last_done_reason = "tool_use"

        sid = seeded_session.id

        await _run_turn(fake_storage_provider, fake_workspace_io, fake_event_bus, _PlainToolUse(), sid)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.RUNNING
        engine, scheduler = _RecordingClaimEngine(), _RecordingScheduler()
        await recover_sessions(engine, scheduler, fake_storage_provider)
        assert engine.upserted == [sid]
