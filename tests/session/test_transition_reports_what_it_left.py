"""What a turn's terminal write reports, and how the guard that protects an ENDED row is enforced.

``_transition_session_status`` leaves an ALREADY-ENDED row alone (see ``test_transition_keeps_the_ended_reason``).
Three things follow from that, and they are pinned here:

* the callers announce the outcome, so the helper reports what the row really says (a ``skipped`` write must not be
  announced as the computed outcome, or the durable event log contradicts the row);
* the guard is a conditional write (``update_unless``), not a check on the helper's own snapshot, because the
  lifecycle lock is process-local (a force-delete on another API process does not serialize with this worker)
  and the reconciler and the pool's ``_end_session`` do not take it at all;
* the on-disk slot follows the row's own reason when the slot accepts it, so a skipped write cannot leave
  ``session.json`` reading RUNNING for a session the row says is ENDED/cancelled.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import primer.session.dispatch as dispatch
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.chat import Done, TextDelta
from primer.model.envelope import RELAY_EVERY_TURN_KEY
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
from tests.session.test_dispatch_interrupt import (
    _RecordingDispatcher,
    _request_stop,
    _StopAwareExecutor,
    _build_returning,
)

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
    """The helper's own read says RUNNING; a writer the lifecycle lock does not cover (the reconciler, the pool's
    ``_end_session``, a force-delete on another process) ends the row before the helper's write lands. A guard on
    the helper's snapshot cannot see that; a conditional write can."""
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


# --- the other callers announce the ROW too: each of these had no test (reverting the line left every suite green) ---


def _spy_terminal(bus, session_id: str) -> list[dict]:
    """The payloads published on the session's terminal key (what the durable ``session.ended`` event is built from)."""
    terminal: list[dict] = []
    publish = bus.publish

    async def spy_publish(key: str, payload: dict) -> None:
        if key == f"session:{session_id}:terminal":
            terminal.append(dict(payload))
        await publish(key, payload)

    bus.publish = spy_publish
    return terminal


async def test_the_terminal_event_of_a_turn_that_failed_after_the_row_was_ended_carries_the_rows_reason(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    """The failure exit writes ENDED/failed through the same guarded helper. A force-delete that ends the row while
    the model call is failing makes that write skip, and the terminal event must say what the ROW says, not 'failed'."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    terminal = _spy_terminal(fake_event_bus, sid)

    async def the_delete_lands_and_the_model_fails() -> None:
        await ended._end_the_row(storage, sid, "force_deleted")
        raise RuntimeError("the provider went away")

    executor = _StopAwareExecutor([TextDelta(text="par", index=0), the_delete_lands_and_the_model_fails])
    executor.session = ended._Slot()
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor),
    )

    outcome = await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)

    row = await storage.get(sid)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "force_deleted"), "the failure overwrote the row"
    assert outcome.success is False and outcome.drop_lease, "the failed turn must still release its lease as a failure"
    assert terminal == [{"status": "ended", "ended_reason": "force_deleted"}], (
        f"the failure path announced its own outcome, not the row's: {terminal}"
    )


async def test_the_terminal_event_of_a_stop_that_lost_the_race_carries_the_rows_reason(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
) -> None:
    """The cancelled exit of a Stop decides WAITING from the row inside the lock; a writer the lock does not cover
    (the reconciler, the pool's ``_end_session``, a force-delete on another process) ends the row between that read
    and the write. The write is refused and the exit must announce the row's ENDED reason, not WAITING."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    terminal = _spy_terminal(fake_event_bus, sid)
    real_update_unless = storage.update_unless
    state = {"flipped": False}

    async def update_unless(entity, **kwargs):
        if kwargs.get("field") == "status" and not state["flipped"]:
            state["flipped"] = True
            stored = await storage.get(sid)
            stored.status, stored.ended_reason = SessionStatus.ENDED, "force_deleted"
        return await real_update_unless(entity, **kwargs)

    storage.update_unless = update_unless
    executor = _StopAwareExecutor(["BLOCK"])
    executor.session = ended._Slot()
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor),
    )

    turn = asyncio.create_task(run_one_session_turn(_make_lease(sid), deps))
    await asyncio.sleep(0.05)
    await _request_stop(fake_storage_provider, fake_event_bus, sid)
    await asyncio.wait_for(turn, 5.0)

    assert state["flipped"], "the race never happened: the Stop did not reach the terminal write"
    row = await storage.get(sid)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "force_deleted"), "the Stop overwrote the row"
    assert terminal == [{"status": "ended", "ended_reason": "force_deleted"}], (
        f"the cancelled exit announced the Stop's own outcome for a row that is ENDED: {terminal}"
    )


async def _relayed_texts(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, *, ended_under_the_turn: bool,
) -> list[str]:
    """Run one clean turn of a thread-mapped session (relay after every turn) and return what was posted to the channel."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    row = await storage.get(sid)
    row.metadata = {
        **(row.metadata or {}),
        SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-1", "anchor": "thr-1", "quiet": False},
        RELAY_EVERY_TURN_KEY: True,
    }
    await storage.update(row)
    if ended_under_the_turn:
        async def read_status_while_the_delete_lands(executor: Any):
            await ended._end_the_row(storage, sid, "force_deleted")
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_while_the_delete_lands)
    executor = _StopAwareExecutor([TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")])
    executor.session = ended._Slot()
    dispatcher = _RecordingDispatcher()
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor), channel_dispatcher=dispatcher,
    )

    await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)

    return dispatcher.texts


async def test_the_answer_of_a_clean_turn_is_relayed_to_the_channel(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
) -> None:
    """The control for the test below: this setup does relay, so an empty list there means the gate held, not that the
    relay path was never reachable."""
    texts = await _relayed_texts(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, ended_under_the_turn=False,
    )

    assert any("the full answer" in text for text in texts), f"the relay did not post the answer: {texts}"


async def test_a_completion_that_lost_the_race_relays_nothing_to_the_channel(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
) -> None:
    """The session was ended under the turn (a force-delete, the pool's preempt convergence, the reconciler): its
    answer must not be posted to the channel binding of a session the row says is over."""
    texts = await _relayed_texts(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, ended_under_the_turn=True,
    )

    assert texts == [], f"the answer of a session that was ended under the turn was relayed: {texts}"
