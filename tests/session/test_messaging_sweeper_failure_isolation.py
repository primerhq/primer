"""Session recovery isolates row failures, not scan failures or cancellation.

Real storage predicates, pagination, park redelivery and turn CAS run on both
fake storage and private SQLite. The claim engine is the production in-memory
engine; faults are injected at the particular row operation that failed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from primer.bus import scheduler_tasks
from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.workspace_session import SessionStatus
from tests.session._peer_fakes import seed_session
from tests.session._wake_worlds import new_world  # noqa: F401

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FAULTS = [
    ("resumable", "arm"), ("arming", "patch"), ("claimable", "arm"),
    ("resumable", "lease"), ("claimable", "lease"),
]


async def _world(new_world):
    world = await new_world()
    world.engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=world.sessions)})

    async def read_file(path):
        return b'{"seq": 1, "kind": "user_input", "payload": {"text": "unanswered"}}\n'

    world.registry.workspace("ws-1").read_file = read_file
    return world


def _sweeper(world):
    return SessionMessagingSweeper(
        storage_provider=world.sp, scheduler=world.scheduler,
        claim_engine=world.engine, workspace_registry=world.registry,
        stale_arm_grace_seconds=1,
    )


async def _seed(world, mode, sid, **overrides):
    fields = dict(turn_status=mode, armed_at=T0 if mode == "arming" else None)
    if mode == "resumable":
        key = f"evwait:{sid}:call"
        fields = dict(
            turn_status="idle", parked_status="resumable", parked_at=T0,
            parked_event_key=key,
            parked_state={"resume_event_key": key, "resume_event_payload": {"answer": "accepted"}},
        )
    fields.update(overrides)
    return await seed_session(world, sid=sid, created_at=T0, **fields)


async def _pass(sweeper, mode):
    if mode == "resumable":
        return await sweeper._repair_resumable_parks()
    return await sweeper._heal_unclaimed_turns()


async def _leased(world, ids):
    return {sid for sid in ids if await world.engine.has_lease(ClaimKind.SESSION, sid)}


@pytest.mark.parametrize("mode,point", FAULTS)
@pytest.mark.parametrize("position", [0, 2, 4], ids=["first", "middle", "last"])
async def test_failed_row_does_not_starve_other_pages_and_is_retried(
    new_world, monkeypatch, caplog, mode, point, position,
):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        sweeper = _sweeper(world)
        monkeypatch.setattr(scheduler_tasks, "_MAX_PAGE", 2)
        ids = [f"s-{i}" for i in range(5)]
        for sid in ids:
            await _seed(world, mode, sid)
        store = world.sessions
        original_find = store.find
        offsets = []

        async def find(predicate, page, **kwargs):
            offsets.append(page.offset)
            return await original_find(predicate, page, **kwargs)

        monkeypatch.setattr(store, "find", find)
        owner, method = {
            "arm": (world.engine, "upsert"),
            "patch": (store, "patch_if"),
            # Include lease lookup in the row boundary even if an implementation
            # raises outside _has_lease's existing fail-closed engine wrapper.
            "lease": (sweeper, "_has_lease"),
        }[point]
        original = getattr(owner, method)
        failing = True
        attempted = []

        async def operation(*args, **kwargs):
            sid = args[1] if point == "arm" else args[0]
            attempted.append(sid)
            if failing and sid == ids[position]:
                raise ConnectionError("one session operation unavailable")
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, method, operation)
        with caplog.at_level(logging.ERROR, logger=scheduler_tasks.__name__):
            assert await _pass(sweeper, mode) == 4
        assert offsets == [0, 2, 4]
        assert set(attempted) == set(ids)
        assert await _leased(world, ids) == set(ids) - {ids[position]}
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and errors[0].exc_info is not None
        assert ids[position] in errors[0].getMessage()
        failed = await store.get(ids[position])
        assert failed is not None
        assert (failed.parked_status if mode == "resumable" else failed.turn_status) == mode
        failing = False
        assert await _pass(sweeper, mode) == 1
        assert await _leased(world, ids) == set(ids)


@pytest.mark.parametrize("mode", ["resumable", "claimable"])
async def test_engine_lease_lookup_failure_remains_fail_closed_and_retries(new_world, monkeypatch, mode):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        for sid in ["s-0", "s-1"]:
            await _seed(world, mode, sid)
        original = world.engine.has_lease
        failing = True

        async def has_lease(kind, sid):
            if failing and sid == "s-0":
                raise ConnectionError("lease lookup unavailable")
            return await original(kind, sid)

        monkeypatch.setattr(world.engine, "has_lease", has_lease)
        sweeper = _sweeper(world)
        assert await _pass(sweeper, mode) == 1
        assert not await original(ClaimKind.SESSION, "s-0")
        assert await original(ClaimKind.SESSION, "s-1")
        failing = False
        assert await _pass(sweeper, mode) == 1
        assert await original(ClaimKind.SESSION, "s-0")


@pytest.mark.parametrize("mode,point", FAULTS)
async def test_cancellation_propagates_without_attempting_the_next_row(new_world, monkeypatch, caplog, mode, point):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        for sid in ["s-0", "s-1"]:
            await _seed(world, mode, sid)
        sweeper = _sweeper(world)
        owner, method = {
            "arm": (world.engine, "upsert"), "patch": (world.sessions, "patch_if"),
            "lease": (world.engine, "has_lease"),
        }[point]
        original_operation = getattr(owner, method)
        entered = asyncio.Event()
        attempted = []

        async def blocked(*args, **kwargs):
            attempted.append(args[1] if point in ("arm", "lease") else args[0])
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(owner, method, blocked)
        task = asyncio.create_task(_pass(sweeper, mode))
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(owner, method, original_operation)
        assert attempted == ["s-0"]
        assert await _leased(world, ["s-0", "s-1"]) == set()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("mode", ["resumable", "arming", "claimable"])
@pytest.mark.parametrize("offset", [0, 2], ids=["first-page", "later-page"])
async def test_scan_failures_propagate_and_a_future_scan_retries(new_world, monkeypatch, mode, offset):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        for i in range(3):
            await _seed(world, mode, f"s-{i}")
        monkeypatch.setattr(scheduler_tasks, "_MAX_PAGE", 2)
        store = world.sessions
        original = store.find
        failing = True

        async def find(predicate, page, **kwargs):
            if failing and page.offset == offset:
                raise ConnectionError("scan unavailable")
            return await original(predicate, page, **kwargs)

        monkeypatch.setattr(store, "find", find)
        sweeper = _sweeper(world)
        with pytest.raises(ConnectionError, match="scan unavailable"):
            await _pass(sweeper, mode)
        # Park recovery processes each page; turn healing snapshots all pages
        # before writing. Neither reports a failed scan as completed recovery.
        already = 2 if mode == "resumable" and offset == 2 else 0
        assert len(await _leased(world, [f"s-{i}" for i in range(3)])) == already
        failing = False
        assert await _pass(sweeper, mode) == 3 - already
        assert len(await _leased(world, [f"s-{i}" for i in range(3)])) == 3


@pytest.mark.parametrize("mode", ["resumable", "claimable"])
async def test_ineligible_and_leased_rows_remain_untouched(new_world, mode):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        await _seed(world, mode, "paused", status=SessionStatus.PAUSED)
        await _seed(world, mode, "ended", status=SessionStatus.ENDED)
        await _seed(world, mode, "leased")
        await world.engine.upsert(ClaimKind.SESSION, "leased")
        if mode == "resumable":
            await _seed(world, mode, "reparked", parked_status="parked")
        else:
            await _seed(world, mode, "reparked", parked_status="parked", parked_at=T0)
        before = {sid: (await world.sessions.get(sid)).model_dump() for sid in ["paused", "ended", "leased", "reparked"]}
        assert await _pass(_sweeper(world), mode) == 0
        assert await _leased(world, before) == {"leased"}
        assert {sid: (await world.sessions.get(sid)).model_dump() for sid in before} == before


@pytest.mark.parametrize("change", ["ended", "reparked", "payload"])
async def test_park_redelivery_rechecks_occurrence_after_another_row_fails(new_world, monkeypatch, change):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        await _seed(world, "resumable", "s-0")
        await _seed(world, "resumable", "s-1")
        store = world.sessions
        original_upsert, original_get = world.engine.upsert, store.get
        moved = False

        async def upsert(kind, sid, **kwargs):
            if sid == "s-0":
                raise ConnectionError("first session unavailable")
            return await original_upsert(kind, sid, **kwargs)

        async def get(sid, *args, **kwargs):
            nonlocal moved
            row = await original_get(sid, *args, **kwargs)
            if sid == "s-1" and not moved:
                moved = True
                changes = {
                    "ended": {"status": SessionStatus.ENDED},
                    "reparked": {"parked_at": T0 + timedelta(seconds=1), "parked_status": "parked"},
                    "payload": {"parked_state": {"resume_event_key": row.parked_event_key, "resume_event_payload": {"answer": "new"}}},
                }[change]
                await store.update(row.model_copy(update=changes))
                return await original_get(sid, *args, **kwargs)
            return row

        monkeypatch.setattr(world.engine, "upsert", upsert)
        monkeypatch.setattr(store, "get", get)
        assert await _pass(_sweeper(world), "resumable") == 0
        assert moved
        assert await _leased(world, ["s-0", "s-1"]) == set()


async def test_arm_generation_fence_remains_after_another_row_fails(new_world, monkeypatch):
    async with asyncio.timeout(10):
        world = await _world(new_world)
        for sid in ["s-0", "s-1"]:
            await _seed(world, "arming", sid)
        store = world.sessions
        original = store.patch_if
        moved = False
        newer = T0 + timedelta(seconds=1)

        async def patch(sid, *args, **kwargs):
            nonlocal moved
            if sid == "s-0":
                raise ConnectionError("first session unavailable")
            if not moved:
                moved = True
                await original(sid, {"armed_at": newer.isoformat()}, where={"turn_status": ["arming"]})
            return await original(sid, *args, **kwargs)

        monkeypatch.setattr(store, "patch_if", patch)
        assert await _pass(_sweeper(world), "arming") == 0
        assert moved
        row = await store.get("s-1")
        assert row.turn_status == "arming" and row.armed_at == newer
        assert await _leased(world, ["s-0", "s-1"]) == set()


@pytest.mark.parametrize("mode", ["resumable", "arming", "claimable"])
async def test_tick_retries_failed_claim_write_after_peer_recovery(new_world, monkeypatch, mode):
    """A partially healed arm is claimable on the next tick, not lost or counted as delivered."""
    async with asyncio.timeout(10):
        world = await _world(new_world)
        for sid in ["s-0", "s-1", "s-2"]:
            await _seed(world, mode, sid)
        original = world.engine.upsert
        failing = True
        attempts = []

        async def upsert(kind, sid, **kwargs):
            attempts.append(sid)
            if failing and sid == "s-0":
                raise ConnectionError("claim write unavailable")
            return await original(kind, sid, **kwargs)

        monkeypatch.setattr(world.engine, "upsert", upsert)
        sweeper = _sweeper(world)
        assert await sweeper._tick() == 0  # queue deliveries, not repair count
        assert attempts == ["s-0", "s-1", "s-2"]
        assert await _leased(world, attempts) == {"s-1", "s-2"}
        if mode == "arming":
            assert (await world.sessions.get("s-0")).turn_status == "claimable"
        failing = False
        assert await sweeper._tick() == 0
        assert attempts == ["s-0", "s-1", "s-2", "s-0"]
        assert await _leased(world, attempts) == {"s-0", "s-1", "s-2"}
