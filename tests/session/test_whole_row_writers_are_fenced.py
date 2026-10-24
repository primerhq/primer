"""Writers that used to put a whole session row back now write only their own fields (review of the G7-03 fix, M5).

The pause route, the drain cursor, and the pool's ``_end_session`` and ``_pause_session`` each read the row and then wrote the WHOLE row they read.
The lifecycle lock is this process's alone, so a wake on another process that reserved a seq or armed a turn in between was put back: its
``last_seq`` regressed (the next writer repeated its seq) and its armed turn was erased. Each is now ONE ``patch_if`` of its own fields, fenced on
what it decided from. (The steer route's ``response_format`` stamp is pinned the same way in ``tests/api/test_session_idle_writers.py``.)

Every test lands the competing write in the exact gap, after the writer's read and before its write, through the real storage (the suite's
in-memory storage, whose ``patch_if`` honours its predicate), with the real writer. Every body is bounded by ``asyncio.timeout``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from primer.api.routers.sessions import pause_session
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import _advance_drain_cursor
from primer.session.seq_reservation import reserve_next_seq
from primer.session.steer_delivery import deliver_steer
from primer.worker.pool import WorkerPool
from tests.session._peer_fakes import World, make_world, seed_session
from tests.session.test_one_turn_per_wake import _each_wake_in_its_own_process

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
BOUND_S = 15.0


class _AfterTheFirstRead:
    """The real session storage; the FIRST read of the row runs ``action`` right after it (the competing writer lands after the writer's read,
    before its write). Everything else goes straight through."""

    def __init__(self, inner: Any, action: Any) -> None:
        self._inner = inner
        self._action = action
        self.fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get(self, id: str, **kw: Any) -> Any:  # noqa: A002
        row = await self._inner.get(id, **kw)
        if not self.fired:
            self.fired = True
            await self._action()
        return row


def _install(world: World, action: Any) -> _AfterTheFirstRead:
    wrapper = _AfterTheFirstRead(world.sp.get_storage(WorkspaceSession), action)
    world.sp._stores[WorkspaceSession] = wrapper
    return wrapper


async def _reserve(world: World) -> None:
    """An advisory record (a PAUSE_SUPERSEDED note, a release's failure marker) takes the next seq on another process."""
    inner = world.sp.get_storage(WorkspaceSession)
    await reserve_next_seq(getattr(inner, "_inner", inner), "ses-b")


async def test_a_pause_that_a_wake_overtakes_between_its_read_and_its_write_does_not_put_the_row_back(monkeypatch):
    """The pause read the session WAITING and idle; a person's message on another process then woke it (RUNNING, its turn armed, its USER_INPUT at
    the next seq). The pause used to write PAUSED over the copy it read: the armed turn and the seq were erased. Now its write is fenced on the
    status it read, so it decides again on the woken row and requests the pause from the running session."""
    async with asyncio.timeout(BOUND_S):
        world = make_world(with_bus=True)
        _each_wake_in_its_own_process(monkeypatch)
        await seed_session(world, sid="ses-b", turn_no=1, last_seq=4, created_at=T0)

        async def a_wake() -> None:
            await deliver_steer(session_id="ses-b", text="go", parallelism="queue", human_intent=True, **world.deliver_kwargs())

        sessions = _install(world, a_wake)
        await pause_session(workspace_id="ws-1", session_id="ses-b", sessions=sessions)

        row = await world.sessions.get("ses-b")
        assert (row.status, row.turn_status, row.last_seq, row.pause_requested) == (SessionStatus.RUNNING, "claimable", 5, True)


async def test_the_drain_cursor_never_puts_back_a_seq_reserved_after_its_read():
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_status="running", turn_no=1, last_seq=6,
                           next_unprocessed_seq=3, created_at=T0)
        sessions = _install(world, lambda: _reserve(world))

        await _advance_drain_cursor(sessions, "ses-b")

        row = await world.sessions.get("ses-b")
        assert (row.last_seq, row.next_unprocessed_seq) == (7, 8), "the reservation survives and the cursor passes it"


def _pool(world: World) -> SimpleNamespace:
    """The fields ``WorkerPool._end_session`` and ``_pause_session`` read off the pool."""
    return SimpleNamespace(
        _storage=world.sp, _workspace_registry=world.registry, _scheduler=world.scheduler, _engine=world.engine, _event_bus=None,
    )


async def test_the_pools_end_never_puts_back_a_seq_reserved_after_its_read():
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        row = await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=1, last_seq=2, created_at=T0)
        _install(world, lambda: _reserve(world))

        await WorkerPool._end_session(_pool(world), row, reason="failed")

        after = await world.sessions.get("ses-b")
        assert (after.status, after.ended_reason, after.last_seq) == (SessionStatus.ENDED, "failed", 3)


async def test_the_pools_end_keeps_the_reason_of_a_row_another_path_ended_first():
    """The first terminal reason wins: a force-delete that ended the row between the end's read and its write is not overwritten."""
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        row = await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=1, created_at=T0)

        async def force_delete() -> None:
            inner = world.sp.get_storage(WorkspaceSession)._inner
            await inner.patch_if("ses-b", {"status": "ended", "ended_reason": "force_deleted"}, where={"status": ["running"]})

        _install(world, force_delete)

        await WorkerPool._end_session(_pool(world), row, reason="failed")

        after = await world.sessions.get("ses-b")
        assert (after.status, after.ended_reason) == (SessionStatus.ENDED, "force_deleted")


async def test_the_pools_pause_never_puts_back_a_seq_reserved_after_its_read():
    async with asyncio.timeout(BOUND_S):
        world = make_world()
        row = await seed_session(world, sid="ses-b", status=SessionStatus.RUNNING, turn_no=1, last_seq=2, created_at=T0)
        _install(world, lambda: _reserve(world))

        await WorkerPool._pause_session(_pool(world), row)

        after = await world.sessions.get("ses-b")
        assert (after.status, after.last_seq) == (SessionStatus.PAUSED, 3)
