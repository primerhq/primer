"""Dormant priority mechanics: real adapters, explicit internal engine capability."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from primer.claim.adapters.memory import MemoryClaimAdapter
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.work_priority import validate_work_priority


class Rows:
    def __init__(self, **rows):
        self.rows = rows

    async def get(self, key):
        return self.rows.get(key)


class Sessions(SessionClaimAdapter):
    async def on_claim(self, conn, lease):
        pass  # This suite isolates queue semantics from the unrelated worker-id stamp.


def queue(rows, *, enabled=True):
    return InMemoryClaimEngine(adapters={ClaimKind.SESSION: Sessions(session_storage=rows)},
                               work_priority_enabled=enabled)


def row(priority=0):
    return SimpleNamespace(priority=priority, parked_status=None)


def test_priority_validation_is_strict_and_bounded():
    assert [validate_work_priority(v) for v in (0, 1, 1_000_000)] == [0, 1, 1_000_000]
    for bad in (-1, 1_000_001, True, 1.0, "1", None):
        with pytest.raises(ValidationError):
            validate_work_priority(bad)


async def test_order_ceiling_and_requeue_preserve_queued_snapshot():
    rows = Rows(background=row(100), urgent=row(0), resumed=row(100))
    engine = queue(rows)
    await engine.upsert(ClaimKind.SESSION, "background", priority=100)
    await engine.upsert(ClaimKind.SESSION, "urgent", priority=100)
    await engine.mark_resumable(ClaimKind.SESSION, "resumed")
    urgent, = await engine.claim_due("one", max_count=4, max_work_priority=0)
    assert urgent.entity_id == "urgent" and urgent.work_priority == 0
    resumed, background = await engine.claim_due("two", max_count=2)
    assert [resumed.entity_id, background.entity_id] == ["resumed", "background"]
    rows.rows["background"] = row(2)
    await engine.release(background, outcome=ReleaseOutcome(success=True, entity_noop=True))
    again, = await engine.claim_due("three", max_count=1)
    assert again.work_priority == background.work_priority == 100
    await engine.release(again, outcome=ReleaseOutcome(success=True, entity_noop=True))
    await engine.mark_resumable(ClaimKind.SESSION, "background")
    fresh, = await engine.claim_due("four", max_count=1)
    assert fresh.work_priority == 2 and background.work_priority == 100


async def test_old_awaited_arm_cannot_overwrite_newer_source_snapshot():
    rows = Rows(s=row(80))
    engine = queue(rows)
    entered, finish = asyncio.Event(), asyncio.Event()
    get = rows.get
    first = True

    async def delayed(key):
        nonlocal first
        result = await get(key)
        if first:
            first = False
            entered.set()
            await finish.wait()
        return result

    rows.get = delayed
    old = asyncio.create_task(engine.upsert(ClaimKind.SESSION, "s"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            rows.rows["s"] = row(5)
            await engine.mark_resumable(ClaimKind.SESSION, "s")
            finish.set()
            await old
        lease, = await engine.claim_due("worker", max_count=1)
        assert lease.work_priority == 5
    finally:
        finish.set()
        await asyncio.gather(old, return_exceptions=True)


async def test_missing_and_invalid_sources_do_not_create_foreground_leases():
    rows = Rows(bad=row(True), legacy=SimpleNamespace(parked_status=None))
    engine = queue(rows)
    for name in ("missing", "bad"):
        with pytest.raises((ValueError, ValidationError)):
            await engine.upsert(ClaimKind.SESSION, name)
        assert not await engine.has_lease(ClaimKind.SESSION, name)
    await engine.upsert(ClaimKind.SESSION, "legacy")
    lease, = await engine.claim_due("worker", max_count=1)
    assert lease.work_priority == 0


async def test_task_arm_reads_parent_not_task_or_producer_priority():
    tasks = Rows(t=SimpleNamespace(session_id="s", priority=0))
    sessions = Rows(s=row(77))
    adapter = ToolCallClaimAdapter(task_storage=tasks, session_storage=sessions)
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: adapter}, work_priority_enabled=True)
    await engine.upsert(ClaimKind.TOOL_CALL, "t", priority=10)
    assert engine._leases[ClaimKind.TOOL_CALL, "t"].work_priority == 77
    sessions.rows.clear()
    with pytest.raises(ValueError):
        await engine.mark_resumable(ClaimKind.TOOL_CALL, "t")
    assert engine._leases[ClaimKind.TOOL_CALL, "t"].work_priority == 77


async def test_replacement_memory_occurrence_keeps_its_priority_through_stale_release():
    due = datetime.now(UTC) - timedelta(seconds=1)
    job = SimpleNamespace(id="m", occurrence_id="first", requested_due_at=due,
                          priority=90, retry_count=0, retry_error=None)
    rows = Rows(m=job)
    engine = InMemoryClaimEngine(adapters={ClaimKind.MEMORY: MemoryClaimAdapter(storage=rows)},
                                work_priority_enabled=True)
    await engine.arm_memory_job(job, conn=None)
    old, = await engine.claim_due("same-worker", max_count=1)
    job = SimpleNamespace(**{**vars(job), "occurrence_id": "second", "priority": 50})
    rows.rows["m"] = job
    await engine.arm_memory_job(job, conn=None)
    await engine.release(old, outcome=ReleaseOutcome(success=False, drop_lease=True))
    fresh, = await engine.claim_due("same-worker", max_count=1)
    assert fresh.memory_occurrence == "second" and fresh.work_priority == 50
    assert old.work_priority == 90
    await engine.release(old, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert await engine.has_live_lease(ClaimKind.MEMORY, "m")


async def test_default_runtime_remains_legacy_order_and_refuses_priority_ceiling():
    rows = Rows(background=row(100), urgent=row(0))
    engine = queue(rows, enabled=False)
    await engine.upsert(ClaimKind.SESSION, "background", priority=10)
    await engine.upsert(ClaimKind.SESSION, "urgent", priority=100)
    with pytest.raises(ValueError, match="not enabled"):
        await engine.claim_due("worker", max_count=1, max_work_priority=0)
    lease, = await engine.claim_due("worker", max_count=1)
    assert lease.entity_id == "background" and lease.work_priority == 0


def test_model_fields_validate_old_rows_and_refuse_noninteger_priorities():
    from primer.model.memory import MemoryJob
    from primer.model.workspace_session import WorkspaceSession
    now = datetime.now(UTC)
    cases = [(WorkspaceSession, {"id": "s", "workspace_id": "w", "created_at": now, "status": "running", "binding": {"kind": "agent", "agent_id": "a"}}),
             (MemoryJob, {"id": "m", "kind": "topics", "agent_id": "a", "created_at": now, "expires_at": now})]
    for model, data in cases:
        assert model.model_validate(data).priority == 0
        assert model.model_validate({**data, "priority": 33}).priority == 33
        for bad in (True, "33", 33.0, -1, 1_000_001):
            with pytest.raises(ValidationError):
                model.model_validate({**data, "priority": bad})


@pytest.mark.parametrize("removal", ["delete", "release", "prune", "already_absent"])
async def test_absent_lease_aba_forces_a_fresh_source_read(removal):
    rows = Rows(s=row(0))
    kind = ClaimKind.MEMORY if removal == "prune" else ClaimKind.SESSION
    engine = (InMemoryClaimEngine(adapters={kind: MemoryClaimAdapter(storage=rows)}, work_priority_enabled=True)
              if removal == "prune" else queue(rows))
    entered, finish = asyncio.Event(), asyncio.Event()
    get = rows.get
    reads = 0

    async def delayed(key):
        nonlocal reads
        reads += 1
        value = await get(key)
        if reads == 1:
            entered.set()
            await finish.wait()
        return value

    rows.get = delayed
    old = asyncio.create_task(engine.upsert(kind, "s"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            rows.rows["s"] = row(5)
            if removal != "already_absent":
                await engine.mark_resumable(kind, "s")
            if removal == "release":
                lease, = await engine.claim_due("worker", max_count=1)
                assert lease.work_priority == 5
                await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True, entity_noop=True))
            elif removal == "prune":
                rows.rows.pop("s")
                assert await engine.prune_dead_leases(kind) == 1
                rows.rows["s"] = row(5)
            else:
                await engine.delete_lease(kind, "s")
            assert not await engine.has_lease(kind, "s")
            reads_before_finish = reads
            finish.set()
            await old
        assert reads == reads_before_finish + 1
        assert await engine.claim_due("foreground", max_count=1, max_work_priority=0) == []
        fresh, = await engine.claim_due("background", max_count=1)
        assert fresh.work_priority == 5
        assert engine._priority_reads == {}
    finally:
        finish.set()
        await asyncio.gather(old, return_exceptions=True)


async def test_cancelled_priority_reader_releases_its_epoch():
    rows = Rows(s=row(0))
    engine = queue(rows)
    entered = asyncio.Event()

    async def suspended(key):
        entered.set()
        await asyncio.Event().wait()

    rows.get = suspended
    task = asyncio.create_task(engine.upsert(ClaimKind.SESSION, "s"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            assert len(engine._priority_reads) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert engine._priority_reads == {}
        assert not await engine.has_lease(ClaimKind.SESSION, "s")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
