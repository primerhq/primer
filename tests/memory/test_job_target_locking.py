"""Representation and failure boundaries of the owned memory target unit."""

import json

import pytest
import pytest_asyncio

from primer.model.common import dump_for_storage
from primer.model.memory import MemoryJob
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.memory.test_job_target import JOB, NOW, enqueue, job
from tests.pg_gate import postgres_marks


@pytest_asyncio.fixture(params=['sqlite', 'fake', pytest.param('postgres', marks=postgres_marks())])
async def provider(request, tmp_path):
    from primer.model.provider import SqliteConfig
    from primer.storage.postgres import PostgresStorageProvider
    from tests.storage.test_storage_contract import _pg_config_for_test
    if request.param == 'fake':
        yield _FakeStorageProvider()
        return
    sp = (SqliteStorageProvider(SqliteConfig(path=tmp_path / 'representations.sqlite'))
          if request.param == 'sqlite' else PostgresStorageProvider(_pg_config_for_test().config))
    await sp.initialize()
    try:
        yield sp
    finally:
        if request.param == 'postgres':
            async with sp.pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA "{sp.schema}" CASCADE')
        await sp.aclose()


@pytest.mark.parametrize('representation', [
    'canonical', 'date_offset', 'date_fraction', 'date_epoch',
    'target_string', 'target_boolean', 'target_float', 'missing_target', 'missing_session',
])
async def test_readable_stored_representations_merge_without_rewriting_identity(provider, representation):
    rows = provider.get_storage(MemoryJob)
    original = job()
    await rows.create(original)
    raw = dump_for_storage(original)
    raw.pop('id', None)
    if representation == 'date_offset':
        raw['created_at'] = NOW.isoformat()
    elif representation == 'date_fraction':
        raw['created_at'] = '2026-11-16T12:00:00.000000Z'
    elif representation == 'date_epoch':
        raw['created_at'] = int(NOW.timestamp())
    elif representation == 'target_string':
        raw['target_turn'] = '1'
    elif representation == 'target_boolean':
        raw['target_turn'] = True
    elif representation == 'target_float':
        raw['target_turn'] = 1.0
    elif representation == 'missing_target':
        raw.pop('target_turn')
    elif representation == 'missing_session':
        raw.pop('session_id')
    if isinstance(provider, _FakeStorageProvider):
        rows.seed_raw(JOB, raw)
    else:
        async with provider.transaction() as connection:
            if isinstance(provider, SqliteStorageProvider):
                await connection.execute('UPDATE memoryjob SET data = ? WHERE id = ?', (json.dumps(raw), JOB))
            else:
                await connection.execute(
                    f'UPDATE "{provider.schema}".memoryjob SET data = $1::jsonb WHERE id = $2', json.dumps(raw), JOB,
                )
    readable = await rows.get(JOB)
    assert readable.created_at == NOW
    await enqueue(provider, 9)
    final = await rows.get(JOB)
    assert final.target_turn == 9 and final.reflected_turn == 1
    assert final.created_at == readable.created_at and final.session_id == readable.session_id
    if isinstance(provider, _FakeStorageProvider):
        stored = rows._raw_doc(JOB, final)
    elif isinstance(provider, SqliteStorageProvider):
        async with provider.connection.execute('SELECT data FROM memoryjob WHERE id = ?', (JOB,)) as cursor:
            stored = json.loads((await cursor.fetchone())[0])
    else:
        async with provider.pool.acquire() as connection:
            stored = json.loads(await connection.fetchval(
                f'SELECT data::text FROM "{provider.schema}".memoryjob WHERE id = $1', JOB,
            ))
    assert stored['created_at'] == raw['created_at']
    assert stored.get('session_id') == raw.get('session_id')


async def test_unreadable_target_is_not_cast_or_overwritten(provider):
    from pydantic import ValidationError
    from primer.model.except_ import RowDecodeError
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    raw = dump_for_storage(job())
    raw['target_turn'] = 'not-an-integer'
    if isinstance(provider, _FakeStorageProvider):
        with pytest.raises(ValidationError):
            rows.seed_raw(JOB, raw)
        assert await rows.get(JOB) == job()
        return  # the fake refuses invalid raw seed; real backends test stored corruption below
    else:
        async with provider.transaction() as conn:
            if isinstance(provider, SqliteStorageProvider):
                await conn.execute('UPDATE memoryjob SET data = ? WHERE id = ?', (json.dumps(raw), JOB))
            else:
                await conn.execute(f'UPDATE "{provider.schema}".memoryjob SET data=$1::jsonb WHERE id=$2', json.dumps(raw), JOB)
    with pytest.raises(RowDecodeError):
        await enqueue(provider, 9)
    with pytest.raises(RowDecodeError):
        await rows.get(JOB)


async def test_unknown_or_unsupported_capabilities_never_fall_back():
    from contextlib import asynccontextmanager
    from primer.int.storage_provider import StorageProvider
    class Unsupported:
        locking_transaction = StorageProvider.locking_transaction
        def get_storage(self, model):
            pytest.fail('unsupported provider must not fall back to an unlocked model read')
    with pytest.raises(NotImplementedError):
        await enqueue(Unsupported())
    class MissingModelLock:
        @asynccontextmanager
        async def locking_transaction(self, **kwargs):
            yield object()
        def get_storage(self, model):
            return object()
    with pytest.raises(AttributeError, match='get_for_update'):
        await enqueue(MissingModelLock())


@pytest.mark.parametrize('receipt', [None, False, 'true', 1, True])
async def test_unknown_receipt_cannot_retry_create_collision(receipt):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from primer.model.except_ import ConflictError
    failure = ConflictError('original create conflict')
    calls = []
    class Unknown:
        @asynccontextmanager
        async def locking_transaction(self, **kwargs):
            calls.append('unit')
            yield SimpleNamespace(rollback_complete=receipt)
        def get_storage(self, model):
            return self
        async def get_for_update(self, *args, **kwargs):
            return None
        async def create(self, *args, **kwargs):
            calls.append('create')
            raise failure
    with pytest.raises(ConflictError) as caught:
        await enqueue(Unknown())
    assert caught.value is failure and calls == ['unit', 'create']


@pytest.mark.parametrize('phase', ['read', 'patch', 'exit'])
async def test_noncreate_conflict_never_recovers(provider, monkeypatch, phase):
    from contextlib import asynccontextmanager
    from primer.model.except_ import ConflictError
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    unit = provider.locking_transaction
    failure, entries = ConflictError('not a create collision'), []
    @asynccontextmanager
    async def observed(**kwargs):
        entries.append('unit')
        async with unit(**kwargs) as token:
            yield token
        if phase == 'exit': raise failure
    async def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(provider, 'locking_transaction', observed)
    if phase != 'exit':
        monkeypatch.setattr(rows, 'get_for_update' if phase == 'read' else 'patch_if', fail)
    with pytest.raises(ConflictError) as caught:
        await enqueue(provider, 9)
    assert caught.value is failure and entries == ['unit']
    assert (await rows.get(JOB)).target_turn == (9 if phase == 'exit' else 1)


@pytest.mark.parametrize('mode', ['error', 'cancel'])
async def test_actual_sqlite_commit_unknown_never_arms_or_retries(tmp_path, monkeypatch, mode):
    import asyncio
    from primer.claim.in_memory import InMemoryClaimEngine
    from primer.int.claim import ClaimKind
    from primer.memory import jobs
    from primer.model.except_ import ConflictError
    from primer.model.provider import SqliteConfig
    from primer.storage._locking import current_transaction
    from tests.memory.test_job_target import setup_hook
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / 'unknown.sqlite'))
    await sp.initialize()
    try:
        rows = sp.get_storage(MemoryJob)
        await rows.create(job())
        session = await setup_hook(sp)
        engine = InMemoryClaimEngine(adapters={}, clock=lambda: NOW)
        commit = sp.connection.commit
        reached, release = asyncio.Event(), asyncio.Event()
        failure = ConflictError('actual committed, acknowledgement lost')
        calls, tokens = [], []
        async def uncertain():
            calls.append('commit')
            tokens.append(current_transaction())
            await commit()
            if mode == 'error': raise failure
            reached.set()
            await release.wait()
        monkeypatch.setattr(sp.connection, 'commit', uncertain)
        task = asyncio.create_task(jobs.on_turn_completed(sp, engine, session, NOW))
        try:
            if mode == 'cancel':
                await asyncio.wait_for(reached.wait(), 3)
                task.cancel()
            with pytest.raises(ConflictError if mode == 'error' else asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 3)
            if mode == 'error': assert caught.value is failure
            assert len(tokens) == 1 and not tokens[0].active and not tokens[0].rollback_complete
            assert calls == ['commit'] and (await rows.get(JOB)).target_turn == 50
            assert not await engine.has_lease(ClaimKind.MEMORY, JOB)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await sp.aclose()


@pytest.mark.parametrize('mode', ['repeat', 'timeout', 'handled_before'])
async def test_collision_cleanup_new_cancellation_prevents_recovery(tmp_path, monkeypatch, mode):
    import asyncio
    from contextlib import asynccontextmanager
    from primer.model.except_ import ConflictError
    from primer.model.provider import SqliteConfig
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / 'cancel.sqlite'))
    await sp.initialize()
    try:
        rows = sp.get_storage(MemoryJob)
        await rows._ensure_table()
        failure = ConflictError('original create collision')
        rollback = sp.connection.rollback
        started, release = asyncio.Event(), asyncio.Event()
        calls, entries = [], []
        unit = sp.locking_transaction
        @asynccontextmanager
        async def tracked(**kwargs):
            async with unit(**kwargs) as token:
                entries.append(token)
                yield token
        monkeypatch.setattr(sp, 'locking_transaction', tracked)
        async def collide(*args, **kwargs):
            calls.append('create')
            raise failure
        async def slow_rollback():
            started.set()
            await release.wait()
            await rollback()
        monkeypatch.setattr(rows, 'create', collide)
        monkeypatch.setattr(sp.connection, 'rollback', slow_rollback)
        async def owner():
            if mode == 'handled_before':
                asyncio.current_task().cancel()
                try: await asyncio.sleep(0)
                except asyncio.CancelledError: pass
            if mode == 'timeout':
                async with asyncio.timeout(0.05): await enqueue(sp, 9)
            else: await enqueue(sp, 9)
        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(started.wait(), 3)
            if mode == 'repeat':
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            elif mode == 'timeout':
                await asyncio.sleep(0.08)
            assert not task.done()
            release.set()
            if mode == 'handled_before':
                await asyncio.wait_for(task, 3)  # confirmed rollback; missing winner is a no-write return
            else:
                with pytest.raises(ConflictError) as caught:
                    await asyncio.wait_for(task, 3)
                assert caught.value is failure
            assert calls == ['create'] and await rows.get(JOB) is None
            assert len(entries) == (2 if mode == 'handled_before' else 1)
            assert entries[0].rollback_complete and not entries[0].active
            assert sp._txn_task is None and not sp.connection.in_transaction
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await sp.aclose()


async def test_rollback_receipt_is_post_exit_and_not_a_commit_receipt(provider):
    from primer.model.except_ import ConflictError
    from primer.storage._locking import current_transaction
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    async with provider.locking_transaction(models=(MemoryJob,)) as committed:
        assert committed.rollback_complete is False
        await rows.patch_if(JOB, {'target_turn': 3}, where={'agent_id': ['agent']}, conn=committed)
    assert not committed.active and committed.rollback_complete is False
    failure = ConflictError('body failed')
    with pytest.raises(ConflictError) as caught:
        async with provider.locking_transaction(models=(MemoryJob,)) as rolled:
            await rows.patch_if(JOB, {'target_turn': 9}, where={'agent_id': ['agent']}, conn=rolled)
            assert rolled.rollback_complete is False
            raise failure
    assert caught.value is failure and not rolled.active and rolled.rollback_complete is True
    assert current_transaction() is None and (await rows.get(JOB)).target_turn == 3
    # A clean peer unit can reuse storage after complete cleanup.
    await enqueue(provider, 4)
    assert (await rows.get(JOB)).target_turn == 4


@pytest.mark.parametrize('close_failure', [False, True])
async def test_sqlite_failed_rollback_quarantines_and_does_not_recover(tmp_path, monkeypatch, close_failure):
    from primer.model.except_ import ConflictError
    from primer.model.provider import SqliteConfig
    config = SqliteConfig(path=tmp_path / 'failed.sqlite')
    sp = SqliteStorageProvider(config)
    await sp.initialize()
    try:
        rows = sp.get_storage(MemoryJob)
        await rows._ensure_table()
        failure, calls, tokens = ConflictError('original create collision'), [], []
        create = rows.create
        async def collide(entity, **kwargs):
            calls.append('create')
            tokens.append(kwargs['conn'])
            await create(entity, **kwargs)  # real uncommitted write must be discarded by close
            raise failure
        async def fail_rollback():
            calls.append('rollback')
            raise RuntimeError('cleanup failed')
        if close_failure:
            close = sp.connection.close
            async def failed_close():
                calls.append('close')
                await close()
                raise RuntimeError('close acknowledgement failed')
            monkeypatch.setattr(sp.connection, 'close', failed_close)
        monkeypatch.setattr(rows, 'create', collide)
        monkeypatch.setattr(sp.connection, 'rollback', fail_rollback)
        with pytest.raises(ConflictError) as caught:
            await enqueue(sp, 9)
        assert caught.value is failure and calls == ['create', 'rollback'] + (['close'] if close_failure else [])
        assert len(tokens) == 1 and not tokens[0].active and not tokens[0].rollback_complete
        assert sp._conn is None and sp._txn_task is None and not sp._write_lock.locked()
    finally:
        await sp.aclose()
    reopened = SqliteStorageProvider(config)
    await reopened.initialize()
    try:
        assert await reopened.get_storage(MemoryJob).get(JOB) is None
    finally:
        await reopened.aclose()


async def test_sqlite_owner_exit_failure_cannot_confirm_rollback(tmp_path, monkeypatch):
    import asyncio
    from primer.model.except_ import ConflictError
    from primer.model.provider import SqliteConfig
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / 'owner.sqlite'))
    await sp.initialize()
    try:
        await sp.get_storage(MemoryJob)._ensure_table()
        native = sp._write_lock
        failure = ConflictError('owner cleanup failed')
        class FailingExit:
            async def __aenter__(self): return await native.__aenter__()
            async def __aexit__(self, *args):
                await native.__aexit__(*args)
                raise failure
        monkeypatch.setattr(sp, '_write_lock', FailingExit())
        with pytest.raises(ConflictError) as caught:
            async with sp.locking_transaction(models=(MemoryJob,)) as token:
                raise ValueError('body error has lower precedence than lock cleanup failure')
        assert caught.value is failure and token.rollback_complete is False and not token.active
        assert sp._txn_task is None and not native.locked()
        assert asyncio.current_task() is not None
    finally:
        await sp.aclose()


@pytest.mark.parametrize('phase', ['begin', 'commit', 'rollback', 'release'])
async def test_postgres_driver_receipt_rejects_uncertain_paths(pg_driver, monkeypatch, phase):
    from primer.model.except_ import ConflictError
    from tests.storage.test_locking_transaction_boundaries import BoundaryThing
    sp, trace, _, _ = pg_driver
    connection = await sp.pool.acquire()
    transaction = connection.transaction()
    failure = ConflictError('injected ' + phase)
    async def fail(): raise failure
    if phase == 'release':
        async def release(conn): raise failure
        monkeypatch.setattr(sp.pool, 'release', release)
    else:
        monkeypatch.setattr(transaction, {'begin': 'start', 'commit': 'commit', 'rollback': 'rollback'}[phase], fail)
    monkeypatch.setattr(connection, 'transaction', lambda: transaction)
    if phase == 'begin':
        with pytest.raises(ConflictError) as caught:
            async with sp.locking_transaction(models=(BoundaryThing,)):
                pytest.fail('failed begin cannot enter body')
        assert caught.value is failure
        return
    with pytest.raises(ConflictError if phase in {'commit', 'release'} else ValueError):
        async with sp.locking_transaction(models=(BoundaryThing,)) as token:
            if phase in {'rollback', 'release'}: raise ValueError('original body')
    assert token.rollback_complete is False and not token.active
    assert ('commit' in trace) is False  # injected commit raises before recording it


@pytest.mark.parametrize('mode', ['repeat', 'timeout'])
async def test_postgres_reset_cancellation_preserves_collision_without_recovery(pg_driver, monkeypatch, mode):
    import asyncio
    from primer.model.except_ import ConflictError
    sp, trace, reset_started, finish_reset = pg_driver
    rows = sp.get_storage(MemoryJob)
    monkeypatch.setattr(rows, '_table_ensured', True)
    failure = ConflictError('original create collision')
    calls = []
    async def missing(*args, **kwargs): return None
    async def collide(*args, **kwargs):
        calls.append('create')
        raise failure
    monkeypatch.setattr(rows, 'get_for_update', missing)
    monkeypatch.setattr(rows, 'create', collide)
    finish_reset.clear()
    async def owner():
        if mode == 'timeout':
            async with asyncio.timeout(0.05): await enqueue(sp, 9)
        else: await enqueue(sp, 9)
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(reset_started.wait(), 3)
        if mode == 'repeat':
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        else: await asyncio.sleep(0.08)
        assert not task.done()
        finish_reset.set()
        with pytest.raises(ConflictError) as caught:
            await asyncio.wait_for(task, 3)
        assert caught.value is failure and calls == ['create']
        assert trace.count('begin') == 1 and trace.count('reset-done') == 1
        assert 'commit' not in trace
    finally:
        finish_reset.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# Controlled driver tests above do not assert native PostgreSQL acceptance.
from tests.storage.test_locking_transaction_boundaries import pg_driver as pg_driver
from tests.pg_gate import needs_postgres


@needs_postgres()
@pytest.mark.parametrize('foreign_winner', [False, True])
async def test_native_postgres_missing_row_create_collision_unwinds_before_merge(monkeypatch, foreign_winner):
    import asyncio
    from primer.memory import jobs
    from primer.storage.postgres import PostgresStorageProvider
    from tests.storage.test_storage_contract import _pg_config_for_test
    config = _pg_config_for_test().config
    first, second = PostgresStorageProvider(config), PostgresStorageProvider(config)
    await first.initialize()
    await second.initialize()
    tasks = []
    try:
        stores = [sp.get_storage(MemoryJob) for sp in (first, second)]
        for rows in stores: await rows._ensure_table()
        read, proceed = [asyncio.Event(), asyncio.Event()], [asyncio.Event(), asyncio.Event()]
        counts = [0, 0]
        for index, rows in enumerate(stores):
            native = rows.get_for_update
            async def observed(*args, _index=index, _native=native, **kwargs):
                result = await _native(*args, **kwargs)
                counts[_index] += 1
                if counts[_index] == 1:
                    assert result is None  # genuine PG absence provides no gap lock
                    read[_index].set()
                    await proceed[_index].wait()
                return result
            monkeypatch.setattr(rows, 'get_for_update', observed)
        async def produce(sp, agent, target):
            await jobs._upsert_job(sp, JOB, kind='reflect', agent_id=agent, session_id='session', target_turn=target, now=NOW)
        winner = asyncio.create_task(produce(first, 'foreign' if foreign_winner else 'agent', 2))
        loser = asyncio.create_task(produce(second, 'agent', 9))
        tasks = [winner, loser]
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in read)), 5)
        proceed[0].set()
        await asyncio.wait_for(winner, 5)
        proceed[1].set()
        await asyncio.wait_for(loser, 5)
        final = await stores[0].get(JOB)
        assert counts == [1, 2]  # actual unique insert failure, full exit, fresh locked read
        assert final.target_turn == (2 if foreign_winner else 9)
        assert final.agent_id == ('foreign' if foreign_winner else 'agent')
        assert final.reflected_turn == 0 and final.created_at == NOW
    finally:
        for e in locals().get('proceed', []): e.set()
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await second.aclose()
        if first._pool is not None:
            async with first.pool.acquire() as c: await c.execute(f'DROP SCHEMA "{first.schema}" CASCADE')
        await first.aclose()


@pytest.mark.parametrize('cleanup', ['release_error', 'repeat_cancel'])
@needs_postgres()
async def test_native_postgres_cleanup_cannot_turn_collision_into_success(monkeypatch, cleanup):
    import asyncio
    from primer.model.except_ import ConflictError
    from primer.storage.postgres import PostgresStorageProvider
    from tests.storage.test_storage_contract import _pg_config_for_test
    sp = PostgresStorageProvider(_pg_config_for_test().config)
    await sp.initialize()
    original_pool = sp.pool
    rows = sp.get_storage(MemoryJob)
    try:
        await rows._ensure_table()
        failure, cleanup_error = ConflictError('original create collision'), ConflictError('pool release error')
        calls, tokens = [], []
        reached, proceed = asyncio.Event(), asyncio.Event()
        async def collide(entity, **kwargs):
            tokens.append(kwargs['conn'])
            calls.append('create')
            raise failure
        class Pool:
            def __getattr__(self, name): return getattr(original_pool, name)
            async def release(self, conn):
                await original_pool.release(conn)  # actual native reset has completed
                reached.set()
                await proceed.wait()
                if cleanup == 'release_error': raise cleanup_error
        monkeypatch.setattr(sp, '_pool', Pool())
        monkeypatch.setattr(rows, 'create', collide)
        task = asyncio.create_task(enqueue(sp, 9))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            assert len(tokens) == 1 and tokens[0].rollback_complete is False
            if cleanup == 'repeat_cancel':
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            proceed.set()
            with pytest.raises(ConflictError) as caught:
                await asyncio.wait_for(task, 5)
            assert caught.value is (cleanup_error if cleanup == 'release_error' else failure)
            assert calls == ['create'] and tokens[0].rollback_complete is (cleanup != 'release_error')
            assert not tokens[0].active and await rows.get(JOB) is None
        finally:
            proceed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        monkeypatch.setattr(sp, '_pool', original_pool)
        async with original_pool.acquire() as c: await c.execute(f'DROP SCHEMA "{sp.schema}" CASCADE')
        await sp.aclose()


async def test_create_defaults_and_token_forwarding(provider, monkeypatch):
    from primer.memory import jobs
    from primer.storage._locking import current_transaction
    rows = provider.get_storage(MemoryJob)
    create, read = rows.create, rows.get_for_update
    seen = []
    async def locked(*args, **kwargs):
        token = kwargs['conn']
        assert token is current_transaction() and token.active
        seen.append(token)
        return await read(*args, **kwargs)
    async def insert(entity, **kwargs):
        assert kwargs['conn'] is seen[-1]
        return await create(entity, **kwargs)
    monkeypatch.setattr(rows, 'get_for_update', locked)
    monkeypatch.setattr(rows, 'create', insert)
    await enqueue(provider, 2)
    final = await rows.get(JOB)
    assert len(seen) == 1 and not seen[0].active and not seen[0].rollback_complete
    assert final == MemoryJob(id=JOB, kind='reflect', agent_id='agent', session_id='session', target_turn=2,
                              created_at=NOW, expires_at=NOW + jobs.JOB_LIFETIME)
    assert final.reflected_turn == 0


@pytest.mark.parametrize('error_name', ['conflict', 'provider', 'cancel'])
async def test_recovery_failure_never_opens_a_third_unit(provider, monkeypatch, error_name):
    import asyncio
    from contextlib import asynccontextmanager
    from primer.model.except_ import ConflictError, ProviderError
    rows = provider.get_storage(MemoryJob)
    unit = provider.locking_transaction
    original_read = rows.get_for_update
    original = ConflictError('original create collision')
    failure = {'conflict': ConflictError, 'provider': ProviderError, 'cancel': asyncio.CancelledError}[error_name]('recovery failed')
    tokens, reads = [], []
    @asynccontextmanager
    async def tracked(**kwargs):
        async with unit(**kwargs) as token:
            tokens.append(token)
            yield token
    async def read(*args, **kwargs):
        reads.append('read')
        if len(reads) == 2: raise failure
        return await original_read(*args, **kwargs)
    async def collide(*args, **kwargs): raise original
    monkeypatch.setattr(provider, 'locking_transaction', tracked)
    monkeypatch.setattr(rows, 'get_for_update', read)
    monkeypatch.setattr(rows, 'create', collide)
    with pytest.raises(type(failure)) as caught:
        await enqueue(provider, 9)
    assert caught.value is failure and len(tokens) == 2 and reads == ['read', 'read']
    assert all(not token.active and token.rollback_complete for token in tokens)
    assert await rows.get(JOB) is None


@pytest.mark.parametrize('invalid_lifecycle', ['active', 'foreign_owner'])
async def test_claimed_receipt_requires_exited_current_owner_token(invalid_lifecycle):
    from contextlib import asynccontextmanager
    from primer.model.except_ import ConflictError
    from primer.storage._locking import LockingTransaction
    failure, entries = ConflictError('original create conflict'), []
    class Invalid:
        @asynccontextmanager
        async def locking_transaction(self, **kwargs):
            token = LockingTransaction(self, None, (MemoryJob,))
            token.rollback_complete = True  # deliberately invalid lifecycle claim
            if invalid_lifecycle == 'foreign_owner':
                token.active = False
                token.owner = object()
            entries.append(token)
            yield token
        def get_storage(self, model): return self
        async def get_for_update(self, *args, **kwargs): return None
        async def create(self, *args, **kwargs): raise failure
    with pytest.raises(ConflictError) as caught:
        await enqueue(Invalid(), 9)
    assert caught.value is failure and len(entries) == 1
