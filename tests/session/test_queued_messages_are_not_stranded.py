"""Queued messages are never stranded or lost (audit G7-14).

A message that waits in a session's pending queue is realized at a turn boundary. Two holes:

* a session can come to rest with messages queued and no boundary to come (the worker died between the turn's terminal write and its drain, a
  realize whose wake failed), and nothing re-drove the queue until a later message arrived, which then ran first;
* the realize deleted the queued row BEFORE its wake, so a crash or a failed write in between lost the message.

The session-messaging sweep (``SessionMessagingSweeper``, leader-elected, at boot and then periodically) realizes the queue head of every session at
rest that has one, through the same ``realize_next_pending`` a boundary uses, and the realize deletes a row only after its wake landed. These tests
drive the real sweep, realize, wake and drain checkpoint over the in-memory world of ``tests/session/_peer_fakes.py`` (the suite's storage, whose
``patch_if`` honours its predicate); every body is bounded by ``asyncio.timeout``.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.int.claim import ClaimKind
from primer.int.coordinator import ROLE_SESSION_MESSAGING_SWEEPER
from primer.model.storage import OffsetPage
from primer.model.workspace_session import PendingSessionMessage, SessionStatus
from primer.session.dispatch import realize_queued_steer
from primer.session.pending_messages import store_pending_steer
from tests.session._peer_fakes import FakeEngine, FakeWorkspace, World, make_world, records, seed_session

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
BOUND_S = 15.0


class _Engine(FakeEngine):
    """The world's claim engine, plus the one lookup the sweep makes: which sessions have a SESSION lease row."""

    def __init__(self, leased: set[str] | None = None) -> None:
        super().__init__()
        self.leased = leased or set()

    async def has_lease(self, kind: ClaimKind, entity_id: str) -> bool:
        assert kind == ClaimKind.SESSION
        return entity_id in self.leased


class _BrokenWorkspace(FakeWorkspace):
    """A workspace whose message log refuses writes until ``healed`` (a runtime connection that dropped, a disk that filled)."""

    def __init__(self) -> None:
        super().__init__()
        self.healed = False

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        if not self.healed:
            raise OSError("the workspace refused the write")
        await super().append_message_line(session_id, line)


def _world(leased: set[str] | None = None) -> World:
    world = make_world(with_bus=True)
    world.engine = _Engine(leased)
    return world


def _sweeper(world: World) -> SessionMessagingSweeper:
    return SessionMessagingSweeper(
        storage_provider=world.sp, scheduler=world.scheduler, claim_engine=world.engine, workspace_registry=world.registry, event_bus=world.bus,
    )


async def _queue(world: World, sid: str, *texts: str) -> list[str]:
    row = await world.sessions.get(sid)
    ids = []
    for text in texts:
        queued = await store_pending_steer(storage_provider=world.sp, session=row, text=text, workspace_registry=world.registry, event_bus=world.bus)
        ids.append(queued.id)
    return ids


async def _pending(world: World, sid: str = "ses-b") -> list[str]:
    page = await world.sp.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=200))
    rows = sorted((r for r in page.items if r.session_id == sid), key=lambda r: (r.enqueued_at, r.id))
    return [r.parts[0]["text"] for r in rows]


def _user_inputs(world: World, sid: str = "ses-b") -> list[str]:
    return [r["payload"]["text"] for r in records(world, sid) if r["kind"] == "user_input"]


# ---- the realize deletes a queued message only after its wake landed ---------------------------------------------------------------------


async def test_a_drain_whose_wake_does_not_land_keeps_the_message_queued_and_the_turn_idle():
    """The drain checkpoint realizes the queue head into a workspace that refuses the write. On main the row was deleted first and the message
    lost; now it stays queued, and the turn the wake armed for it is given back, so the session is not left armed with nothing to answer."""
    async with asyncio.timeout(BOUND_S):
        world = _world()
        broken = _BrokenWorkspace()
        world.registry.workspaces["ws-1"] = broken
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        await _queue(world, "ses-b", "do not lose me")

        await realize_queued_steer(
            storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler, claim_engine=world.engine,
            workspace_registry=world.registry, event_bus=world.bus,
        )

        assert await _pending(world) == ["do not lose me"]
        row = await world.sessions.get("ses-b")
        assert row.turn_status == "idle"
        assert world.scheduler.enqueued == [] and world.engine.upserts == []


async def test_the_sweep_delivers_a_message_whose_drain_failed_once_the_workspace_takes_writes_again():
    """Both halves together: the failed drain left the message queued, and the next sweep delivers it, once."""
    async with asyncio.timeout(BOUND_S):
        world = _world()
        broken = _BrokenWorkspace()
        world.registry.workspaces["ws-1"] = broken
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        await _queue(world, "ses-b", "do not lose me")
        await realize_queued_steer(
            storage_provider=world.sp, workspace_id="ws-1", session_id="ses-b", scheduler=world.scheduler, claim_engine=world.engine,
            workspace_registry=world.registry, event_bus=world.bus,
        )
        broken.healed = True

        assert await _sweeper(world)._tick() == 1

        assert _user_inputs(world) == ["do not lose me"]
        assert await _pending(world) == []
        assert (await world.sessions.get("ses-b")).turn_status == "claimable"


# ---- the sweep ------------------------------------------------------------------------------------------------------------------------------


async def test_the_sweep_realizes_the_queue_head_of_a_session_at_rest_that_nothing_else_will():
    """A session came to rest with two messages queued and no boundary to come: the sweep starts its next turn with the OLDER one (one per tick,
    as a checkpoint does); the turn it starts drains the rest. A second tick finds the turn armed and writes nothing."""
    async with asyncio.timeout(BOUND_S):
        world = _world()
        await seed_session(world, sid="ses-b", turn_no=3, created_at=T0)
        await _queue(world, "ses-b", "older", "newer")
        sweeper = _sweeper(world)

        assert await sweeper._tick() == 1

        assert _user_inputs(world) == ["older"]
        assert await _pending(world) == ["newer"]
        row = await world.sessions.get("ses-b")
        assert (row.status, row.turn_status) == (SessionStatus.RUNNING, "claimable")
        assert world.scheduler.enqueued == ["ses-b"]

        assert await sweeper._tick() == 0
        assert _user_inputs(world) == ["older"] and await _pending(world) == ["newer"]


async def test_the_sweep_leaves_a_session_a_turn_holds():
    """A SESSION lease row means a turn is running, queued or awaiting reclaim, and that turn's own boundary drains the queue: the sweep never
    races it, whatever the row's flags say."""
    async with asyncio.timeout(BOUND_S):
        world = _world(leased={"ses-b"})
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        await _queue(world, "ses-b", "the turn's own drain takes me")

        assert await _sweeper(world)._tick() == 0
        assert _user_inputs(world) == [] and await _pending(world) == ["the turn's own drain takes me"]


@pytest.mark.parametrize("fields", [
    pytest.param({"status": SessionStatus.PAUSED}, id="paused: it holds its queue on purpose"),
    pytest.param({"pause_requested": True}, id="a pause is requested"),
    pytest.param({"status": SessionStatus.RUNNING, "turn_status": "claimable"}, id="a turn is armed"),
    pytest.param({"status": SessionStatus.RUNNING, "turn_status": "running"}, id="a turn is running"),
    pytest.param({"parked_status": "parked", "parked_event_key": "ask_user:ses-b:c1"}, id="parked on a gate"),
    pytest.param({"status": SessionStatus.ENDED, "ended_reason": "completed"}, id="ended: the next message reopens it"),
])
async def test_the_sweep_leaves_a_session_that_is_not_at_rest(fields):
    async with asyncio.timeout(BOUND_S):
        world = _world()
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0, **fields)
        await _queue(world, "ses-b", "waits")

        assert await _sweeper(world)._tick() == 0
        assert records(world, "ses-b") == [] and await _pending(world) == ["waits"]
        assert world.scheduler.enqueued == []


async def test_the_sweep_drops_the_queue_of_a_session_that_no_longer_exists():
    async with asyncio.timeout(BOUND_S):
        world = _world()
        row = await seed_session(world, sid="ses-gone", turn_no=1, created_at=T0)
        await _queue(world, "ses-gone", "nobody will read me", "nor me")
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        await _queue(world, "ses-b", "for a live session")
        await world.sessions.delete(row.id)

        assert await _sweeper(world)._tick() == 1

        assert await _pending(world, "ses-gone") == []
        assert _user_inputs(world) == ["for a live session"]


async def test_the_sweep_without_a_claim_engine_does_nothing():
    """Without the engine it cannot tell a session a turn holds from one at rest, so it leaves every queue alone."""
    async with asyncio.timeout(BOUND_S):
        world = _world()
        await seed_session(world, sid="ses-b", turn_no=1, created_at=T0)
        await _queue(world, "ses-b", "waits")
        sweeper = SessionMessagingSweeper(
            storage_provider=world.sp, scheduler=world.scheduler, claim_engine=None, workspace_registry=world.registry,
        )

        assert await sweeper._tick() == 0
        assert await _pending(world) == ["waits"]


async def test_one_bad_session_does_not_stop_the_sweep(monkeypatch):
    async with asyncio.timeout(BOUND_S):
        world = _world()
        for sid in ("ses-a", "ses-b"):
            await seed_session(world, sid=sid, turn_no=1, created_at=T0)
            await _queue(world, sid, f"for {sid}")
        sweeper = _sweeper(world)
        real = sweeper._has_lease

        async def lease_lookup(session_id: str) -> Any:
            if session_id == "ses-a":
                raise RuntimeError("one row's lookup blew up")
            return await real(session_id)

        monkeypatch.setattr(sweeper, "_has_lease", lease_lookup)

        assert await sweeper._tick() == 1
        assert _user_inputs(world, "ses-b") == ["for ses-b"]
        assert await _pending(world, "ses-a") == ["for ses-a"]


# ---- one owner process ---------------------------------------------------------------------------------------------------------------------


def test_the_sweep_is_leader_elected_under_its_own_role_and_started_at_boot():
    """Never two sweepers racing: the task runs only while its process holds ``ROLE_SESSION_MESSAGING_SWEEPER`` (the coordinator's leader
    election, the same machinery as every other sweeper), and the lifespan starts it with the elector."""
    from primer.api import _app_lifespan

    assert SessionMessagingSweeper.role == ROLE_SESSION_MESSAGING_SWEEPER
    source = inspect.getsource(_app_lifespan)
    assert "session_messaging_sweeper.start(coordinator.leader_elector)" in source
    assert '(session_messaging_sweeper, "session_messaging_sweeper")' in source, "it is stopped with the other background tasks"
