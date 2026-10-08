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
    ended = next(i for i, c in enumerate(calls) if c == ("update_unless", SessionStatus.ENDED))
    assert calls.index(writes[0]) < ended, "the field is written before the row stops being a live one"


@pytest.mark.asyncio
async def test_the_next_turn_clears_it(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider):
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    stale = LastTurnError(code="server_error", at=datetime.now(timezone.utc) - timedelta(minutes=5))
    await storage.update(seeded_session.model_copy(update={"last_turn_error": stale}))

    outcome = await _run(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
        [TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")],
    )

    assert outcome.success is True
    assert (await storage.get(seeded_session.id)).last_turn_error is None, "a turn that starts is past the earlier failure"


@pytest.mark.asyncio
async def test_a_successful_turn_leaves_no_error_on_the_row(seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider):
    await _run(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
        [TextDelta(text="hi", index=0), Done(stop_reason="stop", raw_reason="stop")],
    )

    assert (await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id)).last_turn_error is None
