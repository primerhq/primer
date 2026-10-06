"""A turn's terminal write that races a DELETED row raises ``NotFoundError`` out of the helper (ticket 01a1134b).

Reproduction only: the fix is held for the independent verifiers (the terminal write is on the session core path).

``_transition_session_status`` reads the row, decides, and then writes with ``update_unless`` (a conditional write that
refuses an ENDED row). A row that is DELETED between that read and the write is neither "ended" nor "absent" as far as
the helper's own snapshot knows, and the backends answer the write the way they answer any write to a missing row:
``update_unless`` raises ``NotFoundError`` (Postgres and SQLite alike, see their ``if not exists: raise NotFoundError``).
Nothing in the helper catches it, although the very same situation one step earlier (the read finds no row) is handled
quietly: ``if fresh is None: return _TerminalWrite(False, new_status, ended_reason)``. A force-delete on another API
process does not serialize with this worker (the lifecycle lock is process-local), so the window is real.

Two layers, so the tests do not know the helper's internals more than they must: the helper itself, and a whole turn
through ``run_one_session_turn`` (its completion exit and its failure exit), where the control is the SAME turn with the
row already gone before the helper reads it. Whatever a fix does with a vanished row, the race must end the way its
control ends: no ``NotFoundError`` escapes, nothing is written onto the slot of a deleted session, the row is not
resurrected.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import primer.session.dispatch as dispatch
from primer.model.chat import Done, TextDelta
from primer.model.except_ import NotFoundError
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
from tests.session.test_dispatch_interrupt import _build_returning, _StopAwareExecutor

xfail_the_race = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="01a1134b: update_unless raises NotFoundError for a row deleted between the helper's read and its write, "
    "and nothing in _transition_session_status catches it (the read-time absence is handled)",
)

WRITES = [
    pytest.param(SessionStatus.ENDED, "completed", id="completion"),
    pytest.param(SessionStatus.ENDED, "failed", id="failure"),
    pytest.param(SessionStatus.WAITING, None, id="rest-waiting"),
]


def _delete_the_row_between_the_helpers_read_and_write(storage, session_id: str) -> dict[str, Any]:
    """The row is deleted right before the helper's conditional write reaches the store: after its read, before its write.

    Returns what the store did, so a test can say the race really happened: ``deleted`` (the delete ran) and ``raised``
    (the store itself raised ``NotFoundError`` for the write, as the real backends do)."""
    real_update_unless = storage.update_unless
    seen: dict[str, Any] = {"deleted": False, "raised": None}

    async def update_unless(entity, **kwargs):
        if kwargs.get("field") == "status" and not seen["deleted"]:
            seen["deleted"] = True
            await storage.delete(session_id)
        try:
            return await real_update_unless(entity, **kwargs)
        except NotFoundError as exc:
            seen["raised"] = exc
            raise

    storage.update_unless = update_unless
    return seen


async def _running_row(storage, session_id: str) -> None:
    row = await storage.get(session_id)
    row.status = SessionStatus.RUNNING
    await storage.update(row)


# --- the helper -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("new_status, reason", WRITES)
async def test_scenario_the_row_is_deleted_between_the_helpers_read_and_its_write(
    seeded_session, fake_storage_provider, new_status, reason,
) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await _running_row(storage, sid)
    seen = _delete_the_row_between_the_helpers_read_and_write(storage, sid)

    try:
        await dispatch._transition_session_status(
            storage, SimpleNamespace(id=sid, workspace_id="w1"), new_status=new_status, ended_reason=reason,
        )
    except NotFoundError:
        pass  # today's behaviour; the behaviour test below is the one that says it must not escape

    assert seen["deleted"], "the race never happened: the helper did not reach its conditional write"
    assert isinstance(seen["raised"], NotFoundError), "the store did not refuse the write to a missing row"
    assert await storage.get(sid) is None, "the row must still be gone"


@xfail_the_race
@pytest.mark.parametrize("new_status, reason", WRITES)
async def test_a_row_deleted_between_the_read_and_the_write_is_reported_like_a_row_that_was_already_gone(
    seeded_session, fake_storage_provider, new_status, reason,
) -> None:
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await _running_row(storage, sid)
    _delete_the_row_between_the_helpers_read_and_write(storage, sid)
    slot = ended._Slot()
    escaped: NotFoundError | None = None
    result = None

    try:
        result = await dispatch._transition_session_status(
            storage, SimpleNamespace(id=sid, workspace_id="w1"), new_status=new_status, ended_reason=reason,
            executor=SimpleNamespace(session=slot),
        )
    except NotFoundError as exc:
        escaped = exc

    problems = []
    if escaped is not None:
        problems.append(f"NotFoundError escaped the helper: {escaped}")
    elif tuple(result) != (False, new_status, reason):
        problems.append(f"the write reported {tuple(result)}, expected {(False, new_status, reason)} (nothing landed)")
    if await storage.get(sid) is not None:
        problems.append("the deleted row was resurrected")
    if slot.mirrored:
        problems.append(f"an outcome was mirrored onto the slot of a deleted session: {slot.mirrored}")
    assert not problems, "\n".join(problems)


# --- a whole turn -------------------------------------------------------------------------------------------------


class _Turn:
    """What one ``run_one_session_turn`` did: the exception that escaped it (if any), its release outcome and the
    terminal events it published."""

    def __init__(self) -> None:
        self.escaped: BaseException | None = None
        self.outcome: Any = None
        self.terminal: list[dict] = []
        self.slot = ended._Slot()

    def summary(self) -> dict[str, Any]:
        return {
            "escaped": type(self.escaped).__name__ if self.escaped is not None else None,
            "success": getattr(self.outcome, "success", None),
            "drop_lease": getattr(self.outcome, "drop_lease", None),
            "terminal": list(self.terminal),
            "slot": list(self.slot.mirrored),
        }


async def _run_turn(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, *, exit_: str, row_gone: str,
) -> _Turn:
    """Run one turn that ends through ``exit_`` ("completion": a clean answer, "failure": the model call fails) while
    the row is deleted either BEFORE the helper reads it (``row_gone="before-read"``, the control: handled quietly) or
    between its read and its write (``row_gone="between"``, the race)."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    turn = _Turn()
    publish = fake_event_bus.publish

    async def spy_publish(key: str, payload: dict) -> None:
        if key == f"session:{sid}:terminal":
            turn.terminal.append(dict(payload))
        await publish(key, payload)

    fake_event_bus.publish = spy_publish

    async def delete_now() -> None:
        if await storage.get(sid) is not None:
            await storage.delete(sid)

    if row_gone == "between":
        _delete_the_row_between_the_helpers_read_and_write(storage, sid)
    if exit_ == "completion":
        if row_gone == "before-read":
            async def read_status_while_the_delete_lands(executor: Any):
                await delete_now()
                return None

            monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_while_the_delete_lands)
        script = [TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")]
    else:
        async def the_model_fails() -> None:
            if row_gone == "before-read":
                await delete_now()
            raise RuntimeError("the provider went away")

        script = [TextDelta(text="par", index=0), the_model_fails]
    executor = _StopAwareExecutor(script)
    executor.session = turn.slot
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor),
    )

    try:
        turn.outcome = await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)
    except Exception as exc:  # noqa: BLE001 - what escaped is the observation
        turn.escaped = exc
    return turn


# What a turn does when its row is already gone BEFORE the terminal write reads it (observed on main): the write is a
# no-op, the turn announces its own outcome and releases its lease. The race must end the same way.
CONTROL = {
    "completion": {
        "escaped": None, "success": True, "drop_lease": True,
        "terminal": [{"status": "ended", "ended_reason": "completed"}], "slot": [],
    },
    "failure": {
        "escaped": None, "success": False, "drop_lease": True,
        "terminal": [{"status": "ended", "ended_reason": "failed"}], "slot": [],
    },
}


@pytest.mark.parametrize("exit_", ["completion", "failure"])
async def test_scenario_a_turn_whose_row_vanished_before_the_read_ends_quietly(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, exit_,
) -> None:
    turn = await _run_turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
        exit_=exit_, row_gone="before-read",
    )

    assert await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id) is None, "the row must be gone"
    assert turn.summary() == CONTROL[exit_]


@pytest.mark.parametrize("exit_", ["completion", "failure"])
async def test_scenario_the_row_is_deleted_under_the_turns_terminal_write(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, exit_,
) -> None:
    """The harness really created the race: the store refused the turn's conditional write because the row was gone.
    What the turn then did is the behaviour test's business, so this one passes before and after a fix."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    real_update_unless = storage.update_unless
    refused: list[NotFoundError] = []

    async def watching(entity, **kwargs):
        try:
            return await real_update_unless(entity, **kwargs)
        except NotFoundError as exc:
            refused.append(exc)
            raise

    storage.update_unless = watching
    await _run_turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
        exit_=exit_, row_gone="between",
    )

    assert len(refused) == 1, "the turn's terminal write never reached a missing row"
    assert await storage.get(seeded_session.id) is None, "the row must still be gone"


@xfail_the_race
@pytest.mark.parametrize("exit_", ["completion", "failure"])
async def test_a_turn_whose_row_is_deleted_under_its_terminal_write_ends_like_one_whose_row_was_already_gone(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, exit_,
) -> None:
    """Today the NotFoundError escapes ``run_one_session_turn`` itself: no release outcome is returned, so the pool logs
    "raised unexpectedly" and its ``finally`` releases with the outcome it pre-set (``success=False``,
    ``primer/worker/pool.py`` ~992-1071), and no terminal event is published."""
    race = await _run_turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
        exit_=exit_, row_gone="between",
    )

    assert race.summary() == CONTROL[exit_], f"race: {race.summary()}\ncontrol: {CONTROL[exit_]}"
