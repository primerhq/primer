"""``completed_turn_no``: a turn that ran records itself complete before its release, and only such a turn does.

The marker is the last write of the two terminal lock blocks of a turn that ran: the clean completion and the
Stop/Cancel exit. It is written unconditionally (also when the drain cursor does not move) and fenced on the turn's
own ``turn_no``. ``_end_turn_failed`` (a failed release does not bump ``turn_no``, so a reopened session would match its
own marker), a park and the early exits write none. A failing marker write never fails the turn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
)
from tests.session.test_dispatch_interrupt import _StopAwareExecutor

SID = "s-marker"
TURN = 3


class _Clean(FakeExecutor):
    last_done_reason = "stop"     # an interactive clean stop: the session rests WAITING


async def _seed(storage_provider, **fields) -> WorkspaceSession:
    from tests.session.test_dispatch import _seed_session

    await _seed_session(storage_provider, SID)
    sessions = storage_provider.get_storage(WorkspaceSession)
    row = await sessions.get(SID)
    return await sessions.update(row.model_copy(update={"turn_no": TURN, **fields}))


async def _run(storage_provider, io, bus, executor):
    async def build(_session):
        return executor

    deps = SessionDispatchDeps(storage_provider=storage_provider, workspace_io=io, event_bus=bus, build_executor=build)
    return await run_one_session_turn(_make_lease(SID), deps)


async def _row(storage_provider) -> WorkspaceSession:
    return await storage_provider.get_storage(WorkspaceSession).get(SID)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_ahead", [False, True], ids=["cursor-moves", "cursor-already-ahead"])
async def test_a_clean_completion_records_its_turn_as_completed(
    fake_storage_provider, fake_workspace_io, fake_event_bus, cursor_ahead,
):
    await _seed(fake_storage_provider, next_unprocessed_seq=1000 if cursor_ahead else 0)
    outcome = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _Clean([
        TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop"),
    ]))
    assert outcome.success is True and outcome.drop_lease is True
    row = await _row(fake_storage_provider)
    assert row.status == SessionStatus.WAITING
    assert row.completed_turn_no == TURN
    assert row.turn_no == TURN, "the marker write never bumps turn_no: only the release does"
    assert row.next_unprocessed_seq == (1000 if cursor_ahead else row.last_seq + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_ahead", [False, True], ids=["cursor-moves", "cursor-already-ahead"])
async def test_a_stopped_turn_records_its_turn_as_completed(
    fake_storage_provider, fake_workspace_io, fake_event_bus, cursor_ahead,
):
    await _seed(
        fake_storage_provider, interrupt_requested=True, next_unprocessed_seq=1000 if cursor_ahead else 0,
    )
    outcome = await _run(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        _StopAwareExecutor([TextDelta(text="partial", index=0), "BLOCK"]),
    )
    assert outcome.success is True
    row = await _row(fake_storage_provider)
    assert row.status == SessionStatus.WAITING and row.interrupt_requested is False   # the Stop exit ran
    assert row.completed_turn_no == TURN


@pytest.mark.asyncio
async def test_a_cancelled_turn_records_its_turn_as_completed(
    fake_storage_provider, fake_workspace_io, fake_event_bus,
):
    await _seed(fake_storage_provider)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)

    async def cancel_mid_turn():
        await asyncio.sleep(0.1)             # mid-turn: the watcher has subscribed (the bus does not buffer)
        row = await sessions.get(SID)
        await sessions.update(row.model_copy(update={"cancel_requested": True}))
        await fake_event_bus.publish(f"session:{SID}:cancel", {})

    outcome = await asyncio.wait_for(_run(
        fake_storage_provider, fake_workspace_io, fake_event_bus,
        _StopAwareExecutor([TextDelta(text="partial", index=0), cancel_mid_turn, "BLOCK"]),
    ), 5.0)
    assert outcome.success is True
    row = await _row(fake_storage_provider)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled")
    assert row.completed_turn_no == TURN


@pytest.mark.asyncio
async def test_a_failed_turn_never_records_a_completed_turn(
    fake_storage_provider, fake_workspace_io, fake_event_bus,
):
    """``_end_turn_failed`` writes no marker: the stored row keeps the previous turn's (TURN - 1)."""
    await _seed(fake_storage_provider, completed_turn_no=TURN - 1)
    outcome = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, FakeExecutor([
        TextDelta(text="hi", index=0), RuntimeError("the executor blew up"),
    ]))
    assert outcome.success is False
    row = await _row(fake_storage_provider)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.completed_turn_no == TURN - 1
    assert row.next_unprocessed_seq == row.last_seq + 1, "the failed exit does advance the cursor"


@pytest.mark.asyncio
async def test_a_park_records_no_completed_turn(fake_storage_provider, fake_workspace_io, fake_event_bus):
    await _seed(fake_storage_provider)

    class _Yielding:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            yield TextDelta(text="thinking", index=0)
            raise YieldToWorker(Yielded(tool_name="wait_tool", event_key="timer:tc1"), tool_call_id="tc1")

    outcome = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _Yielding())
    assert outcome.park is not None
    assert (await _row(fake_storage_provider)).completed_turn_no is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["ended", "cancel_requested", "pause_requested"])
async def test_the_early_exits_record_no_completed_turn(
    fake_storage_provider, fake_workspace_io, fake_event_bus, flag,
):
    fields = {"status": SessionStatus.ENDED, "ended_reason": "completed"} if flag == "ended" else {flag: True}
    await _seed(fake_storage_provider, **fields)
    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _Clean([Done(stop_reason="stop", raw_reason="stop")]))
    assert (await _row(fake_storage_provider)).completed_turn_no is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "rejected"])
async def test_a_marker_write_that_fails_does_not_fail_the_turn(
    fake_storage_provider, fake_workspace_io, fake_event_bus, caplog, failure,
):
    await _seed(fake_storage_provider)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    real_patch_if = sessions.patch_if

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        if patch and "completed_turn_no" in patch:
            if failure == "raises":
                raise ConnectionError("the database went away")
            return None
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    sessions.patch_if = patch_if  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
        outcome = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _Clean([
            TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop"),
        ]))
    assert outcome.success is True and outcome.drop_lease is True
    row = await _row(fake_storage_provider)
    assert row.status == SessionStatus.WAITING and row.completed_turn_no is None
    assert any("completed" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


@pytest.mark.asyncio
async def test_the_marker_is_fenced_on_the_turns_own_turn_no(fake_storage_provider, fake_workspace_io, fake_event_bus):
    """A row that moved on to another turn while this one ran is not marked with this turn's number."""
    await _seed(fake_storage_provider)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)

    async def bump():
        row = await sessions.get(SID)
        await sessions.update(row.model_copy(update={"turn_no": TURN + 1}))

    class _Bumping(_Clean):
        async def invoke(self, messages, **kwargs):
            await bump()
            async for ev in super().invoke(messages, **kwargs):
                yield ev

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _Bumping([
        Done(stop_reason="stop", raw_reason="stop"),
    ]))
    assert (await _row(fake_storage_provider)).completed_turn_no is None
