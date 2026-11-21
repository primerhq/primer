"""Durable memory schedules, tested against real claims in both runtime modes."""

from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.adapters.memory import MemoryClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.memory import jobs
from primer.model.memory import MemoryJob
from primer.storage.postgres import PostgresStorageProvider
from tests.memory.test_jobs import _deps
from tests.pg_gate import needs_postgres
from tests.storage.test_storage_contract import _pg_config_for_test
from tests.storage.test_delete_if import provider  # noqa: F401


def queue(sp):
    adapters = {ClaimKind.MEMORY: MemoryClaimAdapter(storage=sp.get_storage(MemoryJob))}
    if isinstance(sp, PostgresStorageProvider):
        return PostgresClaimEngine(storage_provider=sp, adapters=adapters)
    return InMemoryClaimEngine(adapters=adapters)


async def schedule(sp, engine, due):
    await jobs.schedule_job(sp, engine, kind="topics", agent_id="a", next_attempt_at=due)
    return await sp.get_storage(MemoryJob).get("topics:a")


async def test_committed_unarmed_intent_recovers_original_deadline(provider):
    due = datetime.now(UTC) + timedelta(hours=4)
    job = await schedule(provider, None, due)
    assert job.requested_due_at == due
    engine = queue(provider)
    assert not await engine.has_lease(ClaimKind.MEMORY, job.id)
    from primer.memory.scheduling import reconcile_page
    await reconcile_page(provider, engine)
    assert await engine.has_lease(ClaimKind.MEMORY, job.id)
    assert await engine.claim_due("worker", max_count=1) == []


@pytest.mark.parametrize("drop", [True, False])
async def test_release_cannot_erase_or_delay_new_occurrence(provider, drop):
    engine = queue(provider)
    due = datetime.now(UTC) - timedelta(minutes=1)
    await schedule(provider, engine, due)
    old, = await engine.claim_due("same-worker", max_count=1)
    renewed = await schedule(provider, engine, due)
    assert await engine.claim_due("other", max_count=1) == []
    late = ReleaseOutcome(success=False, drop_lease=drop, requeue_after=timedelta(days=1))
    await engine.release(old, outcome=late)
    fresh, = await engine.claim_due("same-worker", max_count=1)
    assert old.memory_occurrence != fresh.memory_occurrence == renewed.occurrence_id
    await engine.release(old, outcome=late)
    assert await engine.has_live_lease(ClaimKind.MEMORY, renewed.id)
    assert fresh.attempt_count == 0


async def projected(sp, engine, job_id):
    if isinstance(sp, PostgresStorageProvider):
        async with sp.pool.acquire() as db:
            row = await db.fetchrow(
                f"SELECT next_attempt_at, memory_occurrence, attempt_count, last_error FROM {sp.leases_table} "
                "WHERE kind='memory' AND entity_id=$1", job_id,
            )
            return tuple(row) if row else None
    row = engine._leases.get((ClaimKind.MEMORY, job_id))
    return (row.next_attempt_at, row.memory_occurrence, row.attempt_count, row.last_error) if row else None


async def test_delayed_producer_projects_current_deadline(provider, monkeypatch):
    import asyncio
    engine = queue(provider)
    arrived, resume = asyncio.Event(), asyncio.Event()
    project = jobs.arm_current

    async def pause_first(*args):
        if not arrived.is_set():
            arrived.set()
            await resume.wait()
        await project(*args)

    monkeypatch.setattr(jobs, "arm_current", pause_first)
    due = datetime.now(UTC)
    first = asyncio.create_task(schedule(provider, engine, due))
    try:
        async with asyncio.timeout(5):
            await arrived.wait()
            newer = await schedule(provider, engine, due + timedelta(hours=2))
            resume.set()
            await first
    finally:
        resume.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert await projected(provider, engine, newer.id) == (newer.requested_due_at, newer.occurrence_id, 0, None)
    assert await engine.claim_due("w", max_count=1) == []


async def test_recreated_job_survives_old_execution_cleanup_and_release(provider):
    engine = queue(provider)
    now = datetime.now(UTC)
    old = await schedule(provider, engine, now)
    claim, = await engine.claim_due("worker", max_count=1)
    await provider.get_storage(MemoryJob).delete(old.id)
    replacement = await schedule(provider, engine, now)
    # Simulate a recreation with all the same pre-protocol identity and timestamps.
    replacement = await provider.get_storage(MemoryJob).patch_if(
        old.id, {"created_at": old.created_at.isoformat(), "expires_at": old.expires_at.isoformat()},
        where=replacement.occurrence_guard(),
    )
    assert replacement.incarnation_id != old.incarnation_id and replacement.occurrence_id != old.occurrence_id
    await jobs._forget(provider, old)
    outcome = await jobs.run_memory_job(_deps(provider, engine, now=now), claim)
    assert outcome.drop_lease
    await engine.release(claim, outcome=outcome)
    await engine.discard_memory_lease(old.id, old.occurrence_id)
    assert await provider.get_storage(MemoryJob).get(old.id) == replacement
    fresh, = await engine.claim_due("worker", max_count=1)
    assert fresh.memory_occurrence == replacement.occurrence_id


async def test_terminal_occurrence_not_resurrected_after_lease_loss(provider):
    from primer.model.agent import Agent, AgentModel

    engine = queue(provider)
    now = datetime.now(UTC)
    job = await schedule(provider, engine, now)
    await provider.get_storage(Agent).create(Agent(id="a", description="d", model=AgentModel(profile_id="p")))
    # Disabled settings: terminal disposition retains the job and retires its deadline.
    claim, = await engine.claim_due("worker", max_count=1)
    out = await jobs.run_memory_job(_deps(provider, engine, now=now), claim)
    assert (await provider.get_storage(MemoryJob).get(job.id)).requested_due_at is None
    await engine.release(claim, outcome=out)
    from primer.memory.scheduling import reconcile_page
    await reconcile_page(provider, engine)
    assert not await engine.has_lease(ClaimKind.MEMORY, job.id)


async def test_recovery_keeps_attempt_and_deadline_after_lost_projection(provider, monkeypatch):
    from primer.memory.scheduling import reconcile_page

    engine = queue(provider)
    now = datetime.now(UTC)
    job = await schedule(provider, engine, now)
    claim, = await engine.claim_due("worker", max_count=1)
    out = ReleaseOutcome(success=False, last_error="transient", requeue_after=timedelta(hours=3))
    # Persist the retry, then lose the old lease before its successor has been projected.
    async def unavailable(*_):
        raise ConnectionError("queue unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "arm_current", unavailable)
        with pytest.raises(ConnectionError):
            await jobs._settle(_deps(provider, engine, now=now), job, claim, out, now)
    await engine.delete_lease(ClaimKind.MEMORY, job.id)
    await reconcile_page(provider, engine)
    retry = await provider.get_storage(MemoryJob).get(job.id)
    assert retry.incarnation_id == job.incarnation_id and retry.occurrence_id != job.occurrence_id
    assert await projected(provider, engine, job.id) == (now + timedelta(hours=3), retry.occurrence_id, 1, "transient")
    assert not await engine.claim_due("another", max_count=1)


async def test_reconcile_pages_retry_failed_row_and_leave_legacy_due_untouched(provider, monkeypatch):
    from primer.memory import scheduling
    now = datetime.now(UTC)
    engine = queue(provider)
    for agent in ("a", "b", "c", "d"):
        await jobs.schedule_job(provider, None, kind="topics", agent_id=agent, next_attempt_at=now + timedelta(hours=2))
    legacy = MemoryJob(id="topics:legacy", kind="topics", agent_id="legacy", created_at=now, expires_at=now)
    await provider.get_storage(MemoryJob).create(legacy)
    failure = []
    project = scheduling.arm_current

    async def once(sp, engine, job_id):
        if not failure:
            failure.append(job_id)
            raise RuntimeError("offline")
        await project(sp, engine, job_id)

    monkeypatch.setattr(scheduling, "arm_current", once)
    cursor = await scheduling.reconcile_page(provider, engine, length=2)
    assert cursor is not None
    assert await scheduling.reconcile_page(provider, engine, cursor, length=2) is None
    assert not await engine.has_lease(ClaimKind.MEMORY, failure[0])
    for agent in ("a", "b", "c", "d"):
        if "topics:" + agent != failure[0]:
            assert await engine.has_lease(ClaimKind.MEMORY, "topics:" + agent)
    await scheduling.reconcile_page(provider, engine)
    assert await engine.has_lease(ClaimKind.MEMORY, failure[0])
    assert not await engine.has_lease(ClaimKind.MEMORY, legacy.id)


def _crashing_producer(config, due):
    """Child process exits after a real PG job commit, before any lease I/O."""
    import asyncio
    import os

    async def crash(*_):
        os._exit(86)

    async def produce():
        sp = PostgresStorageProvider(config)
        await sp.initialize()
        jobs.arm_current = crash
        await schedule(sp, queue(sp), due)

    asyncio.run(produce())


@needs_postgres()
async def test_postgres_process_death_after_job_commit():
    import multiprocessing
    from primer.memory.scheduling import reconcile_page

    config = _pg_config_for_test().config
    sp = PostgresStorageProvider(config)
    await sp.initialize()
    due = datetime.now(UTC) + timedelta(days=2)
    proc = multiprocessing.get_context("spawn").Process(target=_crashing_producer, args=(config, due))
    try:
        proc.start()
        proc.join(timeout=20)
        assert proc.exitcode == 86
        engine = queue(sp)
        pending = await sp.get_storage(MemoryJob).get("topics:a")
        assert pending.requested_due_at == due and pending.occurrence_id
        assert not await engine.has_lease(ClaimKind.MEMORY, pending.id)
        await reconcile_page(sp, engine)
        assert await projected(sp, engine, pending.id) == (due, pending.occurrence_id, 0, None)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=5)
        async with sp.pool.acquire() as db:
            await db.execute(f'DROP SCHEMA "{sp.schema}" CASCADE')
        await sp.aclose()


@needs_postgres()
@pytest.mark.parametrize("arm_first", [False, True])
async def test_postgres_release_and_projection_contend_on_same_lease(arm_first):
    import asyncio
    from primer.memory.scheduling import arm_current

    config = _pg_config_for_test().config
    sp = PostgresStorageProvider(config)
    await sp.initialize()
    engine = queue(sp)
    tasks = []
    try:
        first = await schedule(sp, engine, datetime.now(UTC))
        lease, = await engine.claim_due("worker", max_count=1)
        future = await schedule(sp, None, datetime.now(UTC) + timedelta(days=1))
        async with sp.pool.acquire() as blocker, sp.pool.acquire() as observer:
            async with blocker.transaction():
                await blocker.fetchrow(
                    f"SELECT 1 FROM {sp.leases_table} WHERE kind='memory' AND entity_id=$1 FOR UPDATE", first.id,
                )
                actions = [
                    lambda: arm_current(sp, engine, first.id),
                    lambda: engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True)),
                ]
                if not arm_first:
                    actions.reverse()
                waiting = set()
                async with asyncio.timeout(6):
                    for action in actions:
                        tasks.append(asyncio.create_task(action()))
                        # Observe actual backend lock waits, including the
                        # second waiter's queue behind the first one.
                        while True:
                            rows = await observer.fetch(
                                "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                                "WHERE datname=current_database() AND wait_event_type='Lock'"
                            )
                            found = {row["pid"] for row in rows if set(row["blockers"]) & (waiting | {blocker.get_server_pid()})}
                            if found - waiting:
                                waiting |= found
                                break
                            await asyncio.sleep(.01)
                    assert all(not task.done() for task in tasks)
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert await projected(sp, engine, first.id) == (future.requested_due_at, future.occurrence_id, 0, None)
        assert not await engine.has_live_lease(ClaimKind.MEMORY, first.id)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with sp.pool.acquire() as db:
            await db.execute(f'DROP SCHEMA "{sp.schema}" CASCADE')
        await sp.aclose()


async def test_same_worker_reacquisition_rejects_the_old_release(provider):
    engine = queue(provider)
    job = await schedule(provider, engine, datetime.now(UTC))
    first, = await engine.claim_due("worker", max_count=1)
    if isinstance(provider, PostgresStorageProvider):
        async with provider.pool.acquire() as db:
            await db.execute(f"UPDATE {provider.leases_table} SET expires_at=now() - interval '1 second' "
                             "WHERE kind='memory' AND entity_id=$1", job.id)
    else:
        engine._leases[(ClaimKind.MEMORY, job.id)].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    second, = await engine.claim_due("worker", max_count=1)
    assert first.memory_occurrence == second.memory_occurrence == job.occurrence_id
    deps = _deps(provider, engine, now=datetime.now(UTC))
    await jobs._settle(deps, job, first, ReleaseOutcome(success=True, drop_lease=True), deps.now())
    await jobs._forget(provider, job, engine=engine, lease=first)
    assert await provider.get_storage(MemoryJob).get(job.id) == job
    await engine.release(first, outcome=ReleaseOutcome(success=False, drop_lease=True))
    assert await engine.has_live_lease(ClaimKind.MEMORY, job.id)
    key = [(ClaimKind.MEMORY, job.id)]
    assert first.memory_claim_id != second.memory_claim_id
    assert await engine.heartbeat("worker", key) == []
    assert await engine.heartbeat("worker", key, memory_claims={job.id: first.memory_claim_id}) == []
    assert await engine.heartbeat("worker", key, memory_claims={job.id: second.memory_claim_id}) == key
    await engine.release(second, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert not await engine.has_lease(ClaimKind.MEMORY, job.id)


@pytest.mark.parametrize("attempt,seconds", [(0, 7), (1, 10)])
async def test_failure_retry_backoff_survives_failed_projection(provider, monkeypatch, attempt, seconds):
    from dataclasses import replace

    from primer.memory.scheduling import reconcile_page
    from primer.model.scheduler import WorkerConfig

    engine = queue(provider)
    started = datetime.now(UTC)
    finished = started + timedelta(minutes=4)
    job = await schedule(provider, engine, started)
    lease, = await engine.claim_due("worker", max_count=1)
    lease = replace(lease, attempt_count=attempt)
    deps = _deps(provider, engine, now=finished)
    deps.worker_config = WorkerConfig(base_backoff_seconds=7, max_backoff_seconds=10)
    failure = ReleaseOutcome(success=False, last_error="connection refused")

    async def offline(*_):
        raise ConnectionError("projection interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "arm_current", offline)
        with pytest.raises(ConnectionError):
            await jobs._settle(deps, job, lease, failure, started)
    # The handler's finally can drop the old lease after a settle error.
    await engine.release(lease, outcome=ReleaseOutcome(success=False, drop_lease=True))
    await reconcile_page(provider, engine)
    retry = await provider.get_storage(MemoryJob).get(job.id)
    assert retry.occurrence_id != job.occurrence_id
    assert await projected(provider, engine, job.id) == (
        finished + timedelta(seconds=seconds), retry.occurrence_id, attempt + 1, "connection refused",
    )


async def test_failed_settlement_before_commit_retains_original_pending_work(provider, monkeypatch):
    from primer.memory.scheduling import reconcile_page

    engine = queue(provider)
    now = datetime.now(UTC)
    job = await schedule(provider, engine, now)
    lease, = await engine.claim_due("worker", max_count=1)
    store = provider.get_storage(MemoryJob)
    apply = type(store).patch_if

    async def fail_after_write(self, *args, **kwargs):
        await apply(self, *args, **kwargs)
        raise RuntimeError("rollback")

    with monkeypatch.context() as patch:
        patch.setattr(type(store), "patch_if", fail_after_write)
        with pytest.raises(RuntimeError, match="rollback"):
            await jobs._settle(_deps(provider, engine, now=now), job, lease, ReleaseOutcome(success=False), now)
    assert await store.get(job.id) == job
    await engine.release(lease, outcome=ReleaseOutcome(success=False, drop_lease=True))
    await reconcile_page(provider, engine)
    recovered, = await engine.claim_due("worker", max_count=1)
    assert recovered.memory_occurrence == lease.memory_occurrence and recovered.attempt_count == 0


@pytest.mark.parametrize("ended", [False, True])
async def test_session_public_hooks_recover_after_projection_failure(provider, monkeypatch, ended):
    from types import SimpleNamespace

    from primer.memory.scheduling import reconcile_page
    from primer.model.workspace_session import SessionStatus, WorkspaceSession
    from primer.session import dispatch
    from tests.memory.test_jobs import _agent, _session

    await _agent(provider)
    session = _session(turns=3)
    sessions = provider.get_storage(WorkspaceSession)
    await sessions.create(session)
    engine = queue(provider)
    deps = SimpleNamespace(claim_engine=engine, storage_provider=provider, event_bus=None)
    calls = []

    async def offline(*args, **kwargs):
        calls.append(args)
        raise ConnectionError("lease unavailable")

    async def emit(*args, **kwargs):
        pass

    start = datetime.now(UTC)
    with monkeypatch.context() as patch:
        patch.setattr(engine, "arm_memory_job", offline)
        patch.setattr(dispatch, "_event_recorder", lambda _: SimpleNamespace(emit=emit))
        if ended:
            await dispatch._publish_terminal(deps, session, SessionStatus.ENDED, "done")
        else:
            await dispatch._mark_turn_completed(sessions, session.id, session.turn_no, deps)
    end = datetime.now(UTC)
    job = await provider.get_storage(MemoryJob).get("reflect:a1:s1")
    delay = timedelta(minutes=0 if ended else 10)
    assert len(calls) == 1 and start + delay <= job.requested_due_at <= end + delay
    assert job.target_turn == (session.completed_turn_no if ended else session.turn_no) + 1
    assert not await engine.has_lease(ClaimKind.MEMORY, job.id)
    await reconcile_page(provider, engine)
    assert await projected(provider, engine, job.id) == (job.requested_due_at, job.occurrence_id, 0, None)
    assert bool(await engine.claim_due("worker", max_count=1)) == ended


async def test_running_reconciler_rebuilds_an_empty_queue(fake_storage_provider):
    import asyncio
    from primer.bus.scheduler_tasks import MemoryScheduleReconciler

    due = datetime.now(UTC) + timedelta(days=1)
    job = await schedule(fake_storage_provider, None, due)
    engine = queue(fake_storage_provider)
    loop = MemoryScheduleReconciler(fake_storage_provider, engine, poll_seconds=.01)
    loop.start()
    try:
        async with asyncio.timeout(3):
            while not await engine.has_lease(ClaimKind.MEMORY, job.id):
                await asyncio.sleep(.01)
        assert await projected(fake_storage_provider, engine, job.id) == (due, job.occurrence_id, 0, None)
    finally:
        await loop.stop()


@needs_postgres()
async def test_postgres_upgrade_preserves_existing_legacy_deadline():
    from primer.memory.scheduling import reconcile_page

    config = _pg_config_for_test().config
    before = PostgresStorageProvider(config)
    after = PostgresStorageProvider(config)
    await before.initialize()
    due = datetime.now(UTC) + timedelta(hours=7)
    job = MemoryJob(id="topics:legacy", agent_id="legacy", kind="topics", created_at=due, expires_at=due + timedelta(days=7))
    try:
        await before.get_storage(MemoryJob).create(job)
        async with before.pool.acquire() as db:
            await db.execute(f"ALTER TABLE {before.leases_table} DROP COLUMN memory_occurrence, DROP COLUMN memory_claim_id")
            await db.execute(
                f"INSERT INTO {before.leases_table} (kind,entity_id,next_attempt_at,attempt_count,last_error)"
                " VALUES ('memory',$1,$2,1,'legacy error')", job.id, due,
            )
        await before.aclose()
        await after.initialize()
        engine = queue(after)
        await reconcile_page(after, engine)
        assert await projected(after, engine, job.id) == (due, None, 1, "legacy error")
        assert await engine.claim_due("worker", max_count=1) == []
        assert await after.get_storage(MemoryJob).get(job.id) == job
    finally:
        await after.initialize()
        async with after.pool.acquire() as db:
            await db.execute(f'DROP SCHEMA "{after.schema}" CASCADE')
        await after.aclose()
        await before.aclose()


async def test_rearmed_memory_claim_stops_held_reflection_summary(provider):
    """Actual claim/row fences, including the gated native PG parametrization."""
    import asyncio
    from types import SimpleNamespace

    from primer.memory.reflect import reflect
    from primer.model.chat import Done, Message, TextDelta, TextPart
    from primer.model.model_profile import ModelProfile
    from tests.memory.test_reflect import AGENT, FakeLLM, _deps as reflect_deps, _setup, _transcript

    await provider.get_storage(ModelProfile).create(ModelProfile(
        id="p", description="offline", provider_id="prov", model_name="m", context_length=32768))
    agent, settings, old = await _setup(provider)
    engine = queue(provider)
    now = datetime.now(UTC)
    await jobs._upsert_job(provider, old.id, kind="reflect", agent_id=AGENT, session_id="s1", target_turn=2, now=now)
    await jobs.arm_current(provider, engine, old.id)
    job = await provider.get_storage(MemoryJob).get(old.id)
    lease, = await engine.claim_due("reflection-owner", max_count=1)
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []
    class Held(FakeLLM):
        async def stream(self, **kwargs):
            calls.append(kwargs)
            entered.set()
            await resume.wait()
            yield TextDelta(text='{"facts":[]}', index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
    async def empty(*args):
        return []
    source = _transcript()
    source[0] = Message(role="user", parts=[TextPart(text="original correction " + "x" * 140000)])
    deps = reflect_deps(provider, Held(), source, store=SimpleNamespace(list_entries=empty))
    deps.engine, deps.lease = engine, lease
    running = asyncio.create_task(reflect(deps, job, agent, settings))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            await jobs._upsert_job(provider, job.id, kind="reflect", agent_id=AGENT, session_id="s1", target_turn=3, now=now)
            await jobs.arm_current(provider, engine, job.id)
            resume.set()
            report = await running
    finally:
        resume.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert report.skipped_reason == "input_superseded" and len(calls) == 1
    latest = await provider.get_storage(MemoryJob).get(job.id)
    assert latest.target_turn == 3 and latest.reflected_turn == 0
    assert latest.occurrence_id != lease.memory_occurrence and latest.requested_due_at is not None


async def test_failed_stream_cleanup_does_not_settle_cancelled_memory_job(provider):
    import asyncio
    from types import SimpleNamespace

    from primer.model.chat import TextDelta
    from primer.model.model_profile import ModelProfile
    from tests.memory.test_reflect import AGENT, FakeLLM, _deps as reflect_deps, _setup

    await provider.get_storage(ModelProfile).create(ModelProfile(
        id="p", description="offline", provider_id="prov", model_name="m", context_length=32768))
    _, _, old = await _setup(provider)
    engine = queue(provider)
    now = datetime.now(UTC)
    await jobs._upsert_job(provider, old.id, kind="reflect", agent_id=AGENT, session_id="s1", target_turn=2, now=now)
    await jobs.arm_current(provider, engine, old.id)
    lease, = await engine.claim_due("reflection-owner", max_count=1)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Failing(FakeLLM):
        async def stream(self, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield TextDelta(text="", index=0)
            finally:
                cleaning.set()
                await release.wait()
                raise RuntimeError("isolated cleanup failure")
    async def empty(*args):
        return []
    deps = reflect_deps(provider, Failing(), store=SimpleNamespace(list_entries=empty))
    deps.engine, deps.now = engine, lambda: now
    before = await provider.get_storage(MemoryJob).get(old.id)
    running = asyncio.create_task(jobs.run_memory_job(deps, lease))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            running.cancel()
            await cleaning.wait()
            running.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    latest = await provider.get_storage(MemoryJob).get(old.id)
    assert latest.reflected_turn == 0 and latest.retry_count == before.retry_count
    assert latest.requested_due_at == before.requested_due_at and latest.occurrence_id == before.occurrence_id
