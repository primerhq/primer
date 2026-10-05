"""An ENDED row keeps the reason it was ended with: a turn's own terminal write never overwrites it.

``_transition_session_status`` is how a running turn writes its outcome. A turn can finish at the very moment
something else ends the session: a force-delete flags the row and writes ENDED/force_deleted, the pool's preempt
convergence writes ENDED/cancelled, the reconciler writes ENDED/workspace_lost. The first terminal reason wins.
Before this, the helper skipped only an IDENTICAL write, so a clean completion landing a moment later wrote
ENDED/completed over ENDED/force_deleted (or even resurrected the row to WAITING), hid why the session ended, and
mirrored the wrong reason onto the on-disk slot.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import primer.session.dispatch as dispatch
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _StopAwareExecutor, _build_returning


class _Slot:
    """The executor's on-disk AgentSession slot: records what the dispatch mirrors onto it."""

    def __init__(self) -> None:
        self.mirrored: list[tuple[SessionStatus, str | None]] = []

    async def status(self) -> SessionStatus:
        return SessionStatus.RUNNING

    async def set_status(self, status, *, ended_reason=None) -> None:
        self.mirrored.append((status, ended_reason))


async def _end_the_row(storage, session_id: str, reason: str) -> None:
    row = await storage.get(session_id)
    row.status = SessionStatus.ENDED
    row.ended_reason = reason
    row.cancel_requested = True                   # what a force-delete (or a Cancel) sets first
    await storage.update(row)


@pytest.mark.parametrize(
    "new_status, new_reason",
    [
        (SessionStatus.ENDED, "completed"),
        (SessionStatus.ENDED, "failed"),
        (SessionStatus.ENDED, "cancelled"),
        (SessionStatus.WAITING, None),
        (SessionStatus.RUNNING, None),
        (SessionStatus.PAUSED, None),
    ],
)
async def test_a_turns_terminal_write_never_changes_an_ended_row(
    seeded_session, fake_storage_provider, new_status, new_reason,
) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await _end_the_row(storage, sid, "force_deleted")
    slot = _Slot()

    await dispatch._transition_session_status(
        storage, SimpleNamespace(id=sid, workspace_id="w1"), new_status=new_status, ended_reason=new_reason,
        executor=SimpleNamespace(session=slot),
    )

    row = await storage.get(sid)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "force_deleted"), (
        f"the row was changed to {row.status}/{row.ended_reason} by a turn that finished after it ended"
    )
    assert slot.mirrored == [], f"the wrong outcome was mirrored onto the slot: {slot.mirrored}"


async def test_a_running_row_still_takes_the_turns_terminal_write_and_the_mirror(
    seeded_session, fake_storage_provider,
) -> None:
    """The control: the guard is about an ENDED row only."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    row.status = SessionStatus.RUNNING
    await storage.update(row)
    slot = _Slot()

    await dispatch._transition_session_status(
        storage, SimpleNamespace(id=sid, workspace_id="w1"), new_status=SessionStatus.ENDED,
        ended_reason="completed", executor=SimpleNamespace(session=slot),
    )

    row = await storage.get(sid)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "completed")
    assert slot.mirrored == [(SessionStatus.ENDED, "completed")]


async def test_a_repeated_identical_terminal_write_is_still_a_quiet_no_op(
    seeded_session, fake_storage_provider,
) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await _end_the_row(storage, sid, "completed")
    slot = _Slot()

    await dispatch._transition_session_status(
        storage, SimpleNamespace(id=sid, workspace_id="w1"), new_status=SessionStatus.ENDED,
        ended_reason="completed", executor=SimpleNamespace(session=slot),
    )

    assert (await storage.get(sid)).ended_reason == "completed" and slot.mirrored == []


async def test_a_clean_completion_that_races_a_force_delete_leaves_ended_force_deleted(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
) -> None:
    """The race through real dispatch: the model finishes cleanly, and between the end of the stream and the
    completion's lifecycle lock a force-delete flags the row and writes ENDED/force_deleted. The row is ENDED, so the
    late-cancel arm does not take it (it only takes a row that is not yet ENDED), and the completion's own terminal
    write used to land ENDED/completed on top of it."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    slot = _Slot()

    async def read_status_while_the_delete_lands(executor: Any):
        await _end_the_row(storage, sid, "force_deleted")
        return None

    monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_while_the_delete_lands)
    executor = _StopAwareExecutor([TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")])
    executor.session = slot
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor),
    )

    outcome = await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)

    row = await storage.get(sid)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "force_deleted"), (
        f"the completion wrote {row.status}/{row.ended_reason} over the force-delete"
    )
    assert slot.mirrored == [], f"the completion mirrored its outcome onto the slot of a deleted session: {slot.mirrored}"
    assert outcome.drop_lease, "the lease was not released"
