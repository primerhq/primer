"""Current-row target merges, without changing expiry or lease-arm policy."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.memory import jobs
from primer.model.except_ import ConfigError, ConflictError, NotFoundError, ProviderError
from primer.model.memory import AgentMemory, MemoryJob
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage._locking import current_transaction
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.pg_gate import needs_postgres, postgres_marks
from tests.storage.test_storage_contract import _pg_config_for_test

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
JOB = 'reflect:agent:session'


@pytest_asyncio.fixture(params=['sqlite', 'fake', pytest.param('postgres', marks=postgres_marks())])
async def provider(request, tmp_path):
    if request.param == 'fake':
        yield _FakeStorageProvider()
        return
    sp = (SqliteStorageProvider(SqliteConfig(path=tmp_path / 'target.sqlite'))
          if request.param == 'sqlite' else PostgresStorageProvider(_pg_config_for_test().config))
    await sp.initialize()
    try:
        yield sp
    finally:
        if request.param == 'postgres':
            async with sp.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA "{sp.schema}" CASCADE')
        await sp.aclose()


def job(**fields):
    return MemoryJob(**{'id': JOB, 'kind': 'reflect', 'agent_id': 'agent', 'session_id': 'session',
                        'target_turn': 1, 'reflected_turn': 1, 'created_at': NOW,
                        'expires_at': NOW + jobs.JOB_LIFETIME, **fields})


async def enqueue(sp, target=2, now=NOW):
    await jobs._upsert_job(sp, JOB, kind='reflect', agent_id='agent', session_id='session', target_turn=target, now=now)


class Facade:
    def __init__(self, sp, rows):
        self.sp, self.rows = sp, rows
    def get_storage(self, model):
        return self.rows if model is MemoryJob else self.sp.get_storage(model)
    def __getattr__(self, name):
        return getattr(self.sp, name)


class ReadGate:
    """Pause after the authoritative locked read, not an unlocked snapshot."""
    def __init__(self, rows):
        self.rows = rows
        self.read = asyncio.Event()
        self.go = asyncio.Event()
        self.seen = None
    async def get_for_update(self, id, **kwargs):
        row = await self.rows.get_for_update(id, **kwargs)
        self.seen = row.target_turn if row else None
        self.read.set()
        await self.go.wait()
        return row
    def __getattr__(self, name):
        return getattr(self.rows, name)


@pytest.mark.parametrize('request_target', [2, 9, 12])
@pytest.mark.parametrize('first_high', [False, True])
async def test_real_concurrent_locked_read_preserves_max(provider, request_target, first_high):
    await race_locked(provider, provider, request_target, first_high)


@pytest.mark.parametrize('kind', ['reflect', 'curate', 'topics'])
async def test_lower_target_renews_from_supplied_time_and_keeps_identity(provider, kind):
    rows = provider.get_storage(MemoryJob)
    original = job(kind=kind, target_turn=9, expires_at=NOW + jobs.JOB_LIFETIME + timedelta(days=1))
    await rows.create(original)
    await enqueue(provider, 2)
    final = await rows.get(JOB)
    assert final.target_turn == 9 and final.expires_at == NOW + jobs.JOB_LIFETIME
    assert final.model_copy(update={'expires_at': original.expires_at}) == original


async def test_nested_unit_refuses_without_erasing_caller_work(provider):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    with pytest.raises(ConfigError):
        async with provider.locking_transaction(models=(MemoryJob,)) as token:
            await rows.patch_if(JOB, {'reflected_turn': 7}, where={'agent_id': ['agent']}, conn=token)
            await enqueue(provider, 9)
    assert await rows.get(JOB) == job()
    async with provider.locking_transaction(models=(MemoryJob,)) as token:
        await rows.patch_if(JOB, {'reflected_turn': 7}, where={'agent_id': ['agent']}, conn=token)
        with pytest.raises(ConfigError):
            await enqueue(provider, 9)
        assert not token.failed
    assert (await rows.get(JOB)).reflected_turn == 7
    async with provider.transaction():
        with pytest.raises(ConfigError):
            await enqueue(provider, 9)
    assert await rows.get(JOB) == job(reflected_turn=7)


@pytest.mark.parametrize('field', ['created_at', 'kind', 'session_id'])
async def test_replacement_before_authoritative_read_is_current(provider, field):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    await rows.delete(JOB)
    replacement = job(**{field: {'created_at': NOW + timedelta(seconds=1), 'kind': 'topics', 'session_id': 'new-session'}[field]})
    await rows.create(replacement)
    await enqueue(provider, 9)
    assert await rows.get(JOB) == replacement.model_copy(update={'target_turn': 9})


async def test_same_identity_recreation_is_not_a_generation_fence(provider):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    await rows.delete(JOB)
    await rows.create(job(reflected_turn=0))
    await enqueue(provider, 9)
    final = await rows.get(JOB)
    assert final.target_turn == 9 and final.reflected_turn == 0


@pytest.mark.parametrize('when', ['patch', 'refusal'])
async def test_unexpected_locked_row_loss_is_visible_and_not_retried(provider, monkeypatch, when):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    calls = 0
    async def race(id, changes, **kwargs):
        nonlocal calls
        calls += 1
        if when == 'patch':
            raise NotFoundError(id)
        return None
    monkeypatch.setattr(rows, 'patch_if', race)
    with pytest.raises(NotFoundError if when == 'patch' else ConflictError):
        await enqueue(provider, 9)
    assert calls == 1 and await rows.get(JOB) == job()


@pytest.mark.parametrize('change_during_patch', [False, True])
async def test_foreign_owner_stops_without_mutation_or_spin(provider, monkeypatch, change_during_patch):
    rows = provider.get_storage(MemoryJob)
    original = job(agent_id='agent' if change_during_patch else 'foreign')
    await rows.create(original)
    patch = rows.patch_if
    calls = 0
    async def race(id, changes, **kwargs):
        nonlocal calls
        calls += 1
        await patch(id, {'agent_id': 'foreign'}, where={'agent_id': ['agent']}, conn=kwargs['conn'])
        return await patch(id, changes, **kwargs)
    monkeypatch.setattr(rows, 'patch_if', race)
    if change_during_patch:
        with pytest.raises(ConflictError):
            await enqueue(provider, 9)
    else:
        await enqueue(provider, 9)
    assert calls == int(change_during_patch)
    assert await rows.get(JOB) == original


async def setup_hook(sp):
    await sp.get_storage(AgentMemory).create(AgentMemory(id='agent', enabled=True, idle_minutes=10))
    return WorkspaceSession(id='session', workspace_id='workspace', binding=AgentSessionBinding(agent_id='agent'),
                            status=SessionStatus.RUNNING, created_at=NOW, completed_turn_no=49)


async def test_locked_patch_refusal_is_visible_before_engine_arm(provider, monkeypatch):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    session = await setup_hook(provider)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    attempts = []
    async def competing(id, changes, **kwargs):
        attempts.append(changes)
        return None
    monkeypatch.setattr(rows, 'patch_if', competing)
    with pytest.raises(ConflictError, match='locked memory job'):
        await jobs.on_turn_completed(provider, engine, session, NOW)
    assert len(attempts) == 1 and (await rows.get(JOB)).target_turn == 1
    assert not await engine.has_lease(ClaimKind.MEMORY, JOB)


@pytest.mark.parametrize('error', [ProviderError, asyncio.CancelledError])
@pytest.mark.parametrize('after_write', [False, True])
async def test_errors_and_cancellation_are_not_retried_or_hidden(provider, monkeypatch, error, after_write):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    session = await setup_hook(provider)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    patch = rows.patch_if
    failure = error('unknown write acknowledgement' if after_write else 'write refused by driver')
    calls = 0
    async def fail(id, changes, **kwargs):
        nonlocal calls
        calls += 1
        if after_write:
            await patch(id, changes, **kwargs)
        raise failure
    monkeypatch.setattr(rows, 'patch_if', fail)
    with pytest.raises(error) as caught:
        await jobs.on_turn_completed(provider, engine, session, NOW)
    assert caught.value is failure and calls == 1
    assert (await rows.get(JOB)).target_turn == 1
    assert not await engine.has_lease(ClaimKind.MEMORY, JOB)


@pytest.mark.parametrize('disappear', [False, True])
async def test_create_conflict_keeps_existing_semantics(provider, monkeypatch, disappear):
    rows = provider.get_storage(MemoryJob)
    winner = job(target_turn=9, created_at=NOW + timedelta(seconds=1))
    calls = install_collision(provider, rows, monkeypatch, winner, disappear)
    await enqueue(provider, 2)
    assert calls == ['create', 'unwound', 'recovery']
    if disappear:
        assert await rows.get(JOB) is None
    else:
        final = await rows.get(JOB)
        assert final.target_turn == 9 and final.created_at == winner.created_at
        assert final.expires_at == NOW + jobs.JOB_LIFETIME


async def test_old_missing_target_default_merge(provider):
    from primer.model.common import dump_for_storage
    import json
    rows = provider.get_storage(MemoryJob)
    original = job(target_turn=0)
    await rows.create(original)
    raw = dump_for_storage(original)
    raw.pop('target_turn')
    if isinstance(provider, _FakeStorageProvider):
        rows.seed_raw(JOB, raw)
    else:
        async with provider.transaction() as conn:
            if isinstance(provider, SqliteStorageProvider):
                await conn.execute('UPDATE "memoryjob" SET data = ? WHERE id = ?', (json.dumps(raw), JOB))
            else:
                await conn.execute(f'UPDATE "{provider.schema}"."memoryjob" SET data = $1::jsonb WHERE id = $2', json.dumps(raw), JOB)
    await enqueue(provider, 9)
    assert (await rows.get(JOB)).target_turn == 9


async def test_hook_deadlines_and_claim_io_outside_model_unit(provider, monkeypatch):
    session = await setup_hook(provider)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    upsert = engine.upsert
    async def arm(*args, **kwargs):
        assert current_transaction() is None
        if isinstance(provider, SqliteStorageProvider):
            assert provider._txn_task is None and not provider.connection.in_transaction
        if isinstance(provider, _FakeStorageProvider):
            assert provider._guard_task is None
        return await upsert(*args, **kwargs)
    monkeypatch.setattr(engine, 'upsert', arm)
    await jobs.on_turn_completed(provider, engine, session, NOW)
    assert engine._leases[(ClaimKind.MEMORY, JOB)].next_attempt_at == NOW + timedelta(minutes=10)
    ended = NOW + timedelta(minutes=1)
    await jobs.on_session_ended(provider, engine, session, ended)
    assert engine._leases[(ClaimKind.MEMORY, JOB)].next_attempt_at == ended
    later = NOW + timedelta(minutes=2)
    await jobs.on_turn_completed(provider, engine, session, later)
    assert engine._leases[(ClaimKind.MEMORY, JOB)].next_attempt_at == later + timedelta(minutes=10)
    await jobs.schedule_job(provider, None, kind='topics', agent_id='agent', next_attempt_at=NOW)
    assert (await provider.get_storage(MemoryJob).get('topics:agent')).target_turn == 0


@pytest.mark.parametrize('no_write', ['foreign_owner', 'create_conflict_disappeared'])
async def test_existing_no_write_return_still_allows_outer_hook_arm(provider, monkeypatch, no_write):
    """The existing separate row/lease seam is not durable admission."""
    rows = provider.get_storage(MemoryJob)
    session = await setup_hook(provider)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    if no_write == 'foreign_owner':
        original = job(agent_id='foreign')
        await rows.create(original)
    else:
        install_collision(provider, rows, monkeypatch, job(), True)
    await jobs.on_turn_completed(provider, engine, session, NOW)
    assert await engine.has_lease(ClaimKind.MEMORY, JOB)
    assert await rows.get(JOB) == (original if no_write == 'foreign_owner' else None)


@pytest.mark.parametrize('after_write', [False, True])
async def test_actual_task_cancel_does_not_arm_or_retry(provider, monkeypatch, after_write):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    session = await setup_hook(provider)
    engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
    patch = rows.patch_if
    reached = asyncio.Event()
    calls = 0
    async def paused(id, changes, **kwargs):
        nonlocal calls
        calls += 1
        if after_write:
            await patch(id, changes, **kwargs)
        reached.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(rows, 'patch_if', paused)
    task = asyncio.create_task(jobs.on_turn_completed(provider, engine, session, NOW))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert calls == 1 and (await rows.get(JOB)).target_turn == 1
        assert not await engine.has_lease(ClaimKind.MEMORY, JOB)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def race_locked(first, second, request_target, first_high):
    """Real locks/current read, including separate database connections/pools."""
    rows, other = first.get_storage(MemoryJob), second.get_storage(MemoryJob)
    await rows.create(job())
    if hasattr(other, "_ensure_table"):
        await other._ensure_table()
    gate = ReadGate(rows)
    started, peer_read, native_wait = asyncio.Event(), asyncio.Event(), asyncio.Event()
    peer_pid = []
    if first is not second and isinstance(second, SqliteStorageProvider):
        loop = asyncio.get_running_loop()
        await second.connection.set_trace_callback(
            lambda sql: loop.call_soon_threadsafe(native_wait.set) if sql == "BEGIN IMMEDIATE" else None
        )
    native_get = other.get_for_update
    class Peer:
        async def get_for_update(self, *args, **kwargs):
            if first is not second and isinstance(second, PostgresStorageProvider):
                peer_pid.append(kwargs['conn'].connection.get_server_pid())
                native_wait.set()
            result = await native_get(*args, **kwargs)
            peer_read.set()
            assert result.target_turn == (9 if first_high else request_target)
            return result
        def __getattr__(self, name):
            return getattr(other, name)
    async def peer():
        started.set()
        await enqueue(Facade(second, Peer()), request_target if first_high else 9, NOW + timedelta(minutes=2))
    owner = asyncio.create_task(enqueue(Facade(first, gate), 9 if first_high else request_target))
    follower = None
    try:
        await asyncio.wait_for(gate.read.wait(), 3)
        assert gate.seen == 1
        follower = asyncio.create_task(peer())
        await asyncio.wait_for(started.wait(), 3)
        # Enter the native BEGIN/row-lock wait; no second stale read can pass.
        if first is not second:
            await asyncio.wait_for(native_wait.wait(), 3)
            if isinstance(second, PostgresStorageProvider):
                async with first.pool.acquire() as observer:
                    async with asyncio.timeout(3):
                        while not await observer.fetchval(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", peer_pid[0],
                        ):
                            await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0.04)
        assert not follower.done() and not peer_read.is_set()
        gate.go.set()
        await asyncio.wait_for(asyncio.gather(owner, follower), 5)
        final = await rows.get(JOB)
        assert peer_read.is_set() and final.target_turn == max(9, request_target)
        assert final.expires_at == NOW + timedelta(minutes=2) + jobs.JOB_LIFETIME
        assert final.created_at == NOW and final.reflected_turn == 1
    finally:
        gate.go.set()
        for task in (owner, follower):
            if task is not None and not task.done(): task.cancel()
        await asyncio.gather(*(t for t in (owner, follower) if t is not None), return_exceptions=True)
        if first is not second and isinstance(second, SqliteStorageProvider):
            await second.connection.set_trace_callback(None)


@pytest.mark.parametrize('first_high', [False, True])
async def test_independent_sqlite_connections_merge_current_target(tmp_path, first_high):
    config = SqliteConfig(path=tmp_path / 'independent-target.sqlite')
    first, second = SqliteStorageProvider(config), SqliteStorageProvider(config)
    try:
        await first.initialize()
        await second.initialize()
        await race_locked(first, second, 2, first_high)
    finally:
        await second.aclose()
        await first.aclose()


@needs_postgres()
@pytest.mark.parametrize('first_high', [False, True])
async def test_independent_postgres_pools_merge_current_target(first_high):
    config = _pg_config_for_test().config
    first, second = PostgresStorageProvider(config), PostgresStorageProvider(config)
    try:
        await first.initialize()
        await second.initialize()
        await race_locked(first, second, 2, first_high)
    finally:
        await second.aclose()
        if first._pool is not None:
            async with first.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA "{first.schema}" CASCADE')
        await first.aclose()


def install_collision(provider, rows, monkeypatch, winner, disappear=False):
    """Inject create collision; install a winner ONLY after actual unit unwind.

    This portable control is distinct from the native missing-row PG race.
    """
    create, unit = rows.create, provider.locking_transaction
    failure = ConflictError('definite original create collision')
    calls = []
    async def collide(*args, **kwargs):
        calls.append('create')
        raise failure
    @asynccontextmanager
    async def competing(**kwargs):
        if calls: calls.append('recovery')
        try:
            async with unit(**kwargs) as token:
                yield token
        except ConflictError as exc:
            assert exc is failure and token.rollback_complete and not token.active
            assert current_transaction() is None
            calls.append('unwound')
            await create(winner)
            if disappear: await rows.delete(winner.id)
            raise
    monkeypatch.setattr(rows, 'create', collide)
    monkeypatch.setattr(provider, 'locking_transaction', competing)
    return calls
