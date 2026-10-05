"""What a turn's terminal write reports, and how the guard that protects an ENDED row is enforced.

``_transition_session_status`` leaves an ALREADY-ENDED row alone (see ``test_transition_keeps_the_ended_reason``).
Three things follow from that, and they are pinned here:

* the callers announce the outcome, so the helper reports what the row really says (a ``skipped`` write must not be
  announced as the computed outcome, or the durable event log contradicts the row);
* the guard is a conditional write (``update_unless``), not a check on the helper's own snapshot, because a
  force-delete, the reconciler and the pool's ``_end_session`` write without the lifecycle lock;
* the on-disk slot follows the row's own reason when the slot accepts it, so a skipped write cannot leave
  ``session.json`` reading RUNNING for a session the row says is ENDED/cancelled.
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
from tests.session import test_transition_keeps_the_ended_reason as ended
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _StopAwareExecutor, _build_returning

_SESSION = SimpleNamespace(id="s1", workspace_id="w1")


async def _write(storage, sid: str, status, reason, executor=None):
    return await dispatch._transition_session_status(
        storage, SimpleNamespace(id=sid, workspace_id="w1"), new_status=status, ended_reason=reason, executor=executor,
    )


async def test_a_write_that_lands_says_so(seeded_session, fake_storage_provider) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    row.status = SessionStatus.RUNNING
    await storage.update(row)

    result = await _write(storage, sid, SessionStatus.ENDED, "completed")

    assert (result.landed, result.status, result.ended_reason) == (True, SessionStatus.ENDED, "completed")


async def test_a_skipped_write_reports_what_the_row_says_not_what_the_turn_computed(
    seeded_session, fake_storage_provider,
) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await ended._end_the_row(storage, sid, "force_deleted")

    result = await _write(storage, sid, SessionStatus.WAITING, None)

    assert (result.landed, result.status, result.ended_reason) == (False, SessionStatus.ENDED, "force_deleted")


async def test_an_identical_repeat_reports_the_turns_own_outcome(seeded_session, fake_storage_provider) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await ended._end_the_row(storage, sid, "completed")

    result = await _write(storage, sid, SessionStatus.ENDED, "completed")

    assert (result.landed, result.status, result.ended_reason) == (False, SessionStatus.ENDED, "completed")


async def test_a_row_that_ends_between_the_read_and_the_write_is_not_overwritten(
    seeded_session, fake_storage_provider,
) -> None:
    """The helper's own read says RUNNING; a writer that does not take the lifecycle lock (a force-delete, the
    reconciler, the pool's ``_end_session``) ends the row before the helper's write lands. A guard on the helper's
    snapshot cannot see that; a conditional write can."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    row.status = SessionStatus.RUNNING
    await storage.update(row)
    real_get, real_update, real_update_unless = storage.get, storage.update, storage.update_unless
    state = {"pending": False}

    async def get(entity_id, **kwargs):
        found = await real_get(entity_id, **kwargs)
        if entity_id == sid and found is not None:
            state["pending"] = True                     # the racing writer fires right after this read
            return found.model_copy()                   # the caller's snapshot is independent of the stored row
        return found

    async def end_it_now() -> None:
        if state["pending"]:
            state["pending"] = False
            stored = await real_get(sid)
            stored.status, stored.ended_reason = SessionStatus.ENDED, "force_deleted"

    async def update(entity, **kwargs):
        await end_it_now()
        return await real_update(entity, **kwargs)

    async def update_unless(entity, **kwargs):
        await end_it_now()
        return await real_update_unless(entity, **kwargs)

    storage.get, storage.update, storage.update_unless = get, update, update_unless

    result = await _write(storage, sid, SessionStatus.ENDED, "completed")

    stored = await real_get(sid)
    assert (stored.status, stored.ended_reason) == (SessionStatus.ENDED, "force_deleted"), (
        f"a row that ended between the read and the write was overwritten with {stored.status}/{stored.ended_reason}"
    )
    assert (result.landed, result.ended_reason) == (False, "force_deleted")


@pytest.mark.parametrize("row_reason", ["completed", "failed", "cancelled", "tool_turn_cap"])
async def test_a_skipped_write_mirrors_the_rows_own_reason_when_the_slot_accepts_it(
    seeded_session, fake_storage_provider, row_reason,
) -> None:
    """The pool's ``_end_session`` ends a row ENDED/cancelled without touching the slot; the turn's own write is then
    skipped, so nothing else would bring ``session.json`` out of RUNNING."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await ended._end_the_row(storage, sid, row_reason)
    slot = ended._Slot()

    await _write(storage, sid, SessionStatus.ENDED, "failed" if row_reason != "failed" else "completed",
                 executor=SimpleNamespace(session=slot))

    assert slot.mirrored == [(SessionStatus.ENDED, row_reason)]


@pytest.mark.parametrize("row_reason", ["force_deleted", "workspace_lost"])
async def test_a_skipped_write_mirrors_nothing_for_a_reason_the_slot_does_not_accept(
    seeded_session, fake_storage_provider, row_reason,
) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await ended._end_the_row(storage, sid, row_reason)
    slot = ended._Slot()

    await _write(storage, sid, SessionStatus.ENDED, "completed", executor=SimpleNamespace(session=slot))

    assert slot.mirrored == [], "an unknown reason would be written to the slot as 'completed'"


async def test_the_terminal_event_of_a_completion_that_lost_the_race_carries_the_rows_reason(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
) -> None:
    """The durable event log must not contradict the row: after the skipped write the turn announces what the ROW
    says (ENDED/force_deleted), not the outcome it computed (and the relay, which keys on that outcome, stays quiet)."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    terminal: list[dict] = []
    publish = fake_event_bus.publish

    async def spy_publish(key: str, payload: dict) -> None:
        if key == f"session:{sid}:terminal":
            terminal.append(dict(payload))
        await publish(key, payload)

    fake_event_bus.publish = spy_publish

    async def read_status_while_the_delete_lands(executor: Any):
        await ended._end_the_row(storage, sid, "force_deleted")
        return None

    monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_while_the_delete_lands)
    executor = _StopAwareExecutor([TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")])
    executor.session = ended._Slot()
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor),
    )

    await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)

    assert terminal == [{"status": "ended", "ended_reason": "force_deleted"}], (
        f"the terminal event contradicts the row: {terminal}"
    )
