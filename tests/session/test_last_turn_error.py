"""``WorkspaceSession.last_turn_error``: the row says its last turn failed (C-024 slice 1, ticket 01a11d23-705d).

A failed turn ends the session today, so the row says ``ended / failed`` and nothing more is needed. The next slice lets a retryable model error leave an
interactive session RESTING instead, and a resting row has no ``ended_reason``: ``last_turn_error {code, at}`` is how it still says that the turn it rests
after failed (the console's failed-turn indicator reads it, and so does the stuck-session sweeper, which must not take a first turn that failed and rests for
one that never started). This slice adds the field and its writers; the failure exit still ends the session.

Written by the failure exit with ONE field-scoped ``patch_if`` (before the status transition, so no reader sees a resting row without it), cleared by the
running flip of the next turn (the same ``patch_if`` that already clears ``workspace_refusal``) and by a reopen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.model.chat import Done, Error, TextDelta, TurnStreamFailure
from primer.model.workspace_session import LastTurnError, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeExecutor,
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)


async def _run(session, io, bus, storage, events):
    async def build(_session: WorkspaceSession):
        return FakeExecutor(events)

    deps = SessionDispatchDeps(storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build)
    return await run_one_session_turn(_make_lease(session.id), deps)


def _failure(code: str | None = "server_error") -> TurnStreamFailure:
    return TurnStreamFailure(Error(code=code, message="boom", fatal=True), partial_messages=[], rounds_completed=0)


def test_the_field_is_additive_and_round_trips():
    row = WorkspaceSession.model_validate({
        "id": "s1", "workspace_id": "w1", "binding": {"kind": "agent", "agent_id": "ag1"}, "status": "waiting",
        "created_at": "2026-10-09T00:00:00Z",
    })
    assert row.last_turn_error is None, "a row written before the field existed reads None"
    assert row.model_dump(mode="json")["last_turn_error"] is None

    at = datetime(2026, 10, 9, 1, 2, 3, tzinfo=timezone.utc)
    row = row.model_copy(update={"last_turn_error": LastTurnError(code="server_error", at=at)})
    again = WorkspaceSession.model_validate(row.model_dump(mode="json"))
    assert again.last_turn_error == LastTurnError(code="server_error", at=at)


@pytest.mark.asyncio
async def test_a_failed_turn_records_its_code_and_when(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider):
    before = datetime.now(timezone.utc)

    outcome = await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, [_failure("server_error")])

    assert outcome.success is False
    row = await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id)
    assert row.last_turn_error is not None and row.last_turn_error.code == "server_error"
    assert before - timedelta(seconds=1) <= row.last_turn_error.at <= datetime.now(timezone.utc) + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_a_stream_failure_without_a_code_and_a_crash_have_codes_too(fake_workspace_io, fake_event_bus, fake_storage_provider):
    from tests.session.test_dispatch import _seed_session

    codes = {}
    for sid, events in (("s-nocode", [_failure(None)]), ("s-crash", [RuntimeError("kaboom")])):
        session = await _seed_session(fake_storage_provider, sid)
        await _run(session, fake_workspace_io, fake_event_bus, fake_storage_provider, events)
        codes[sid] = (await fake_storage_provider.get_storage(WorkspaceSession).get(sid)).last_turn_error.code

    assert codes == {"s-nocode": "llm_stream_error", "s-crash": "turn_failed"}, (
        "the stream's own code, else the fallback ended_detail uses, else a name for 'the turn raised something that is not a model error'"
    )


@pytest.mark.asyncio
async def test_the_failure_exit_writes_it_with_one_field_scoped_patch_before_the_status_moves(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    calls: list[tuple[str, object]] = []
    real_patch, real_update_unless = storage.patch_if, storage.update_unless

    async def patch_if(id, patch=None, **kwargs):
        calls.append(("patch_if", frozenset((patch or {}).keys())))
        return await real_patch(id, patch, **kwargs)

    async def update_unless(entity, **kwargs):
        calls.append(("update_unless", entity.status))
        return await real_update_unless(entity, **kwargs)

    storage.patch_if, storage.update_unless = patch_if, update_unless

    await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, [_failure()])

    writes = [c for c in calls if c == ("patch_if", frozenset({"last_turn_error"}))]
    assert len(writes) == 1, f"expected ONE patch_if of last_turn_error alone, saw {calls}"
    moved = next(i for i, c in enumerate(calls) if c[0] == "update_unless")
    assert calls.index(writes[0]) < moved, "the field is written before the status moves (to ENDED, or to resting)"


@pytest.mark.asyncio
async def test_the_next_turn_clears_it_when_it_starts(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider):
    """Read the row from INSIDE the turn (as the executor is built, after the running flip): it is the flip that clears the field, not the end of
    the turn, so a console polling a turn in flight does not show the earlier failure."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    stale = LastTurnError(code="server_error", at=datetime.now(timezone.utc) - timedelta(minutes=5))
    await storage.update(seeded_session.model_copy(update={"last_turn_error": stale}))
    seen_while_building: list[LastTurnError | None] = []

    async def build(_session: WorkspaceSession):
        seen_while_building.append((await storage.get(seeded_session.id)).last_turn_error)
        return FakeExecutor([TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")])

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus, build_executor=build,
    )
    outcome = await run_one_session_turn(_make_lease(seeded_session.id), deps)

    assert outcome.success is True
    assert seen_while_building == [None], "the failure was still on the row while the next turn was running"
    assert (await storage.get(seeded_session.id)).last_turn_error is None


@pytest.mark.asyncio
async def test_a_row_that_is_already_ended_keeps_its_first_terminal_reason(fake_storage_provider):
    """The stamp is guarded on the row not being ENDED, like every terminal write: a Cancel that ended the session first is not overwritten."""
    import primer.session.dispatch as dispatch
    from tests.session.test_dispatch import _seed_session

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session = await _seed_session(fake_storage_provider, "s-ended-first")
    await storage.update(session.model_copy(update={"status": SessionStatus.ENDED, "ended_reason": "cancelled"}))

    landed = await dispatch._record_last_turn_error(storage, "s-ended-first", "server_error", session.binding_epoch)

    assert landed is False

    assert (await storage.get("s-ended-first")).last_turn_error is None


@pytest.mark.asyncio
async def test_a_binding_that_switched_during_the_turn_is_not_stamped(fake_storage_provider):
    """The status transition right after the stamp is voided when the binding epoch moved (the failure describes work done for a binding the session
    has left); the stamp is fenced on the same epoch."""
    import primer.session.dispatch as dispatch
    from tests.session.test_dispatch import _seed_session

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    session = await _seed_session(fake_storage_provider, "s-switched")
    await storage.update(session.model_copy(update={"binding_epoch": session.binding_epoch + 1}))

    landed = await dispatch._record_last_turn_error(storage, "s-switched", "server_error", session.binding_epoch)

    assert landed is False

    assert (await storage.get("s-switched")).last_turn_error is None


@pytest.mark.asyncio
async def test_a_turn_whose_binding_switched_while_it_ran_and_then_failed_is_not_stamped(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """The epoch fence at the CALL site, end to end (the helper test above passes the epoch by hand): the binding switches while the executor is
    being built, the turn then fails, and the failure it describes belongs to a binding the session has left."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)

    async def build(_session: WorkspaceSession):
        row = await storage.get(seeded_session.id)
        await storage.update(row.model_copy(update={"binding_epoch": row.binding_epoch + 1}))
        return FakeExecutor([_failure("server_error")])

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus, build_executor=build,
    )
    await run_one_session_turn(_make_lease(seeded_session.id), deps)

    assert (await storage.get(seeded_session.id)).last_turn_error is None


@pytest.mark.asyncio
async def test_a_stamp_that_cannot_be_written_does_not_keep_the_session_from_ending(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    """The stamp is advisory: the failure exit's job is to release the lease and move the status. A storage error on the stamp is logged and the
    exit goes on."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    real_patch = storage.patch_if

    async def patch_if(id, patch=None, **kwargs):
        if "last_turn_error" in (patch or {}) and patch["last_turn_error"] is not None:
            raise RuntimeError("storage hiccup")
        return await real_patch(id, patch, **kwargs)

    storage.patch_if = patch_if

    outcome = await _run(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, [_failure("auth_error")])

    assert outcome.success is False and outcome.drop_lease is True
    row = await storage.get(seeded_session.id)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed") and row.last_turn_error is None


@pytest.mark.asyncio
async def test_a_successful_turn_leaves_no_error_on_the_row(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider):
    await _run(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
        [TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")],
    )

    assert (await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id)).last_turn_error is None
