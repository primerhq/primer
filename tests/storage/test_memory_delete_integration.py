"""Mixed guarded deletion and MemoryJob transactions, including fake commit faults."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.events import registry
from primer.model.except_ import ConfigError, ConflictError, ProviderError
from primer.model.memory import MemoryJob
from primer.model.provider import SqliteConfig
from primer.storage._locking import drain_cleanup
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.memory.test_job_target import JOB, NOW, enqueue, job
from tests.pg_gate import postgres_marks
from tests.storage.test_delete_if import DeleteThing, deleted_events
from tests.storage.test_storage_contract import _pg_config_for_test


@pytest_asyncio.fixture(params=['sqlite', 'fake', pytest.param('postgres', marks=postgres_marks())])
async def provider(request, tmp_path, monkeypatch):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, 'delete_test')
    if request.param == 'fake':
        yield _FakeStorageProvider()
        return
    sp = (SqliteStorageProvider(SqliteConfig(path=tmp_path / 'mixed.sqlite'))
          if request.param == 'sqlite' else PostgresStorageProvider(_pg_config_for_test().config))
    await sp.initialize()
    try:
        yield sp
    finally:
        if request.param == 'postgres':
            async with sp.pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA "{sp.schema}" CASCADE')
        await sp.aclose()


async def setup(sp):
    deletes, memory = sp.get_storage(DeleteThing), sp.get_storage(MemoryJob)
    await deletes.create(DeleteThing(id='m'))
    await memory.create(job())
    return deletes, memory


async def mutate(deletes, memory, token):
    # Lock in canonical table order; a held deletion key may be revisited later.
    assert await deletes.get_for_update('m', conn=token) is not None
    assert (await memory.patch_if(JOB, {'target_turn': 9}, where={'agent_id': ['agent']}, conn=token)).target_turn == 9
    assert await deletes.delete_if('m', where={'guard': [1]}, conn=token) is True


@pytest.mark.parametrize('commit', [False, True])
async def test_mixed_commit_or_body_rollback(provider, commit):
    deletes, memory = await setup(provider)
    failure = ProviderError('abort body')
    try:
        with acting_as(Person('mixed-person', 'user')):
            async with provider.locking_transaction(models=(MemoryJob, DeleteThing)) as token:
                await mutate(deletes, memory, token)
                assert token.rollback_complete is False
                if not commit: raise failure
    except ProviderError as caught:
        assert not commit and caught is failure
    assert not token.active and token.rollback_complete is (not commit)
    assert (await deletes.get('m') is None) is commit
    assert (await memory.get(JOB)).target_turn == (9 if commit else 1)
    events = await deleted_events(provider)
    assert len(events) == int(commit)
    if commit: assert events[0].actor == 'user:mixed-person' and events[0].payload == {'id': 'm'}
    await enqueue(provider, 12)
    assert (await memory.get(JOB)).target_turn == 12


@pytest.mark.parametrize('after_stage', [False, True])
async def test_caught_mixed_cdc_error_poisoning_rolls_back_both_models(provider, monkeypatch, after_stage):
    deletes, memory = await setup(provider)
    error = ProviderError('delete CDC failed')
    if isinstance(provider, _FakeStorageProvider):
        target, method = provider.get_event_store(), 'append'
    else:
        import primer.storage.sqlite as sqlite
        import primer.storage.postgres as postgres
        target, method = (sqlite if isinstance(provider, SqliteStorageProvider) else postgres), '_append_crud_event'
    native = getattr(target, method)
    async def fail(*args, **kwargs):
        if after_stage: await native(*args, **kwargs)
        raise error
    monkeypatch.setattr(target, method, fail)
    with pytest.raises(ConfigError, match='caught storage failure'):
        async with provider.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
            assert await deletes.get_for_update('m', conn=token) is not None
            await memory.patch_if(JOB, {'target_turn': 9}, where={'agent_id': ['agent']}, conn=token)
            with pytest.raises(ProviderError) as caught:
                await deletes.delete_if('m', where={'guard': [1]}, conn=token)
            if isinstance(provider, SqliteStorageProvider):
                assert caught.value.cause is error and caught.value.__cause__ is error
            else:
                assert caught.value is error
            assert token.failed
    assert token.rollback_complete and not token.active
    assert await deletes.get('m') is not None and (await memory.get(JOB)).target_turn == 1
    assert await deleted_events(provider) == []


@pytest.mark.parametrize('after_publication', [False, True])
async def test_fake_publication_fault_is_not_a_body_rollback_receipt(monkeypatch, after_publication):
    sp = _FakeStorageProvider()
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, 'delete_test')
    deletes, memory = await setup(sp)
    event_store = sp.get_event_store()
    publish = event_store.publish
    failure = ConflictError('commit publication/ack failure')
    previous = list(event_store._events)
    def fail(pending):
        if after_publication: publish(pending)
        raise failure
    monkeypatch.setattr(event_store, 'publish', fail)
    with pytest.raises(ConflictError) as caught:
        async with sp.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
            await mutate(deletes, memory, token)
    assert caught.value is failure
    assert token.rollback_complete is False and not token.active
    # Synchronous fake publication restores both internal snapshots on error;
    # entering commit still forbids a reusable body-rollback confirmation.
    assert await deletes.get('m') is not None and (await memory.get(JOB)).target_turn == 1
    assert event_store._events == previous
    monkeypatch.setattr(event_store, 'publish', publish)
    await enqueue(sp, 12)
    assert (await memory.get(JOB)).target_turn == 12


@pytest.mark.parametrize('commit', [False, True])
async def test_fake_guard_exit_error_never_confirms_rollback(monkeypatch, commit):
    sp = _FakeStorageProvider()
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, 'delete_test')
    deletes, memory = await setup(sp)
    guard = sp._guard
    error = ConflictError('guard exit failed')
    @asynccontextmanager
    async def broken_guard():
        outer = sp._guard_task is not asyncio.current_task()
        try:
            async with guard(): yield
        finally:
            if outer: raise error
    monkeypatch.setattr(sp, '_guard', broken_guard)
    with pytest.raises(ConflictError) as caught:
        async with sp.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
            await mutate(deletes, memory, token)
            if not commit: raise ValueError('body abort')
    assert caught.value is error and not token.active and not token.rollback_complete
    monkeypatch.setattr(sp, '_guard', guard)
    assert (await deletes.get('m') is None) is commit
    assert (await memory.get(JOB)).target_turn == (9 if commit else 1)
    assert len(await deleted_events(sp)) == int(commit)


async def test_mixed_repeated_cancellation_drains_cleanup(provider, monkeypatch):
    deletes, memory = await setup(provider)
    cleanup, release = asyncio.Event(), asyncio.Event()
    if isinstance(provider, _FakeStorageProvider):
        native = provider._guard
        @asynccontextmanager
        async def guarded():
            outer = provider._guard_task is not asyncio.current_task()
            async with native():
                try: yield
                finally:
                    if outer:
                        cleanup.set()
                        await drain_cleanup(release.wait())
        monkeypatch.setattr(provider, '_guard', guarded)
    elif isinstance(provider, SqliteStorageProvider):
        rollback = provider.connection.rollback
        async def paused():
            cleanup.set()
            await release.wait()
            await rollback()
        monkeypatch.setattr(provider.connection, 'rollback', paused)
    else:
        pool = provider.pool
        class PausedPool:
            def __getattr__(self, name): return getattr(pool, name)
            async def release(self, conn):
                cleanup.set()
                await release.wait()
                await pool.release(conn)
        monkeypatch.setattr(provider, '_pool', PausedPool())
    ready, tokens = asyncio.Event(), []
    async def owner():
        async with provider.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
            tokens.append(token)
            await mutate(deletes, memory, token)
            ready.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and tokens[0].rollback_complete is False
        release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
        assert task.cancelling() >= 3 and tokens[0].rollback_complete and not tokens[0].active
        assert await deletes.get('m') is not None and (await memory.get(JOB)).target_turn == 1
        assert await deleted_events(provider) == []
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('commit', [False, True])
async def test_independent_memory_peer_waits_then_reads_current_mixed_outcome(provider, commit):
    deletes, memory = await setup(provider)
    ready, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    error = ProviderError('owner aborted')
    async def owner():
        try:
            async with provider.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
                await mutate(deletes, memory, token)
                ready.set()
                await release.wait()
                if not commit: raise error
        except ProviderError as caught: assert not commit and caught is error
    async def peer():
        started.set()
        await enqueue(provider, 12)
    task = asyncio.create_task(owner())
    follower = None
    try:
        await asyncio.wait_for(ready.wait(), 3)
        follower = asyncio.create_task(peer())
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.sleep(0.04)
        assert not follower.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(task, follower), 5)
        assert (await memory.get(JOB)).target_turn == 12
        assert (await deletes.get('m') is None) is commit
        assert len(await deleted_events(provider)) == int(commit)
    finally:
        release.set()
        for child in (task, follower):
            if child is not None: child.cancel()
        await asyncio.gather(*(c for c in (task, follower) if c is not None), return_exceptions=True)


async def test_fake_event_id_reservation_requires_current_owner_guard():
    from primer.storage._locking import LockingTransaction
    sp = _FakeStorageProvider()
    events = sp.get_event_store()
    forged = LockingTransaction(sp, None, (DeleteThing, MemoryJob))
    forged._fake_events = []
    before = events._next_id
    with pytest.raises(ConfigError):
        await events.append(event_type='forged', conn=forged)
    assert events._next_id == before and events._events == [] and forged._fake_events == []


@pytest.mark.parametrize('after_publication', [False, True])
async def test_clean_context_event_peer_survives_failed_publication(monkeypatch, after_publication):
    from contextvars import Context
    sp = _FakeStorageProvider()
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, 'delete_test')
    deletes, memory = await setup(sp)
    events = sp.get_event_store()
    first_id = await events.append(event_type='independent.before', payload={'preserve': True})
    original_events = list(events._events)
    publish = events.publish
    failure = ConflictError('publication acknowledgement')
    def fail(pending):
        if after_publication: publish(pending)
        raise failure
    monkeypatch.setattr(events, 'publish', fail)
    ready, proceed, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tokens = []
    async def owner():
        async with sp.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
            tokens.append(token)
            await mutate(deletes, memory, token)
            ready.set()
            await proceed.wait()
    async def peer():
        started.set()
        event_id = await events.append(event_type='independent.after', payload={'survives': True})
        assert await deletes.get('m') is not None and (await memory.get(JOB)).target_turn == 1
        return event_id
    task = asyncio.create_task(owner())
    follower = None
    try:
        await asyncio.wait_for(ready.wait(), 3)
        staged_id = tokens[0]._fake_events[0].id
        next_reserved = events._next_id
        assert first_id < staged_id < next_reserved
        follower = asyncio.create_task(peer(), context=Context())
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.sleep(0.04)
        assert not follower.done() and events._next_id == next_reserved
        proceed.set()
        with pytest.raises(ConflictError) as caught: await asyncio.wait_for(task, 3)
        assert caught.value is failure and not tokens[0].rollback_complete
        peer_id = await asyncio.wait_for(follower, 3)
        assert peer_id == next_reserved and peer_id > staged_id
        assert events._next_id == peer_id + 1
        assert events._events[:-1] == original_events
        assert [e.id for e in events._events] == [first_id, peer_id]
        assert events._events[-1].payload == {'survives': True}
    finally:
        proceed.set()
        for child in (task, follower):
            if child is not None: child.cancel()
        await asyncio.gather(*(c for c in (task, follower) if c is not None), return_exceptions=True)


@pytest.mark.parametrize('invalid', ['missing_token', 'expired', 'child_inherited', 'child_clean', 'wrong_guard'])
async def test_fake_event_admission_failures_do_not_reserve_ids(invalid):
    from contextvars import Context
    sp = _FakeStorageProvider()
    events = sp.get_event_store()
    async with sp.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
        before = events._next_id
        async def refused():
            with pytest.raises(ConfigError):
                await events.append(event_type='must_not_allocate', conn=token)
        if invalid == 'missing_token':
            with pytest.raises(ConfigError): await events.append(event_type='missing_token')
        elif invalid in {'child_inherited', 'child_clean'}:
            child = asyncio.create_task(refused(), **({'context': Context()} if invalid == 'child_clean' else {}))
            await asyncio.wait_for(child, 1)
        elif invalid == 'wrong_guard':
            owner = sp._guard_task
            sp._guard_task = None
            try: await refused()
            finally: sp._guard_task = owner
        assert events._next_id == before and token._fake_events == [] and events._events == []
    if invalid == 'expired': await refused()
    assert events._next_id == before
    assert await events.append(event_type='valid_clean_peer') == before


@pytest.mark.parametrize('mode', ['ack_error', 'cancel_after_commit'])
async def test_sqlite_mixed_commit_uncertainty_preserves_atomic_durable_outcome(tmp_path, monkeypatch, mode):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, 'delete_test')
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / 'mixed-commit.sqlite'))
    await sp.initialize()
    try:
        deletes, memory = await setup(sp)
        commit = sp.connection.commit
        committed, finish = asyncio.Event(), asyncio.Event()
        failure = ConflictError('actual mixed COMMIT acknowledgement lost')
        calls, tokens = [], []
        async def uncertain():
            calls.append('commit')
            await commit()
            committed.set()
            if mode == 'ack_error': raise failure
            await finish.wait()
        monkeypatch.setattr(sp.connection, 'commit', uncertain)
        async def owner():
            async with sp.locking_transaction(models=(DeleteThing, MemoryJob)) as token:
                tokens.append(token)
                await mutate(deletes, memory, token)
        task = asyncio.create_task(owner())
        try:
            await asyncio.wait_for(committed.wait(), 3)
            if mode == 'cancel_after_commit': task.cancel()
            with pytest.raises(ConflictError if mode == 'ack_error' else asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 3)
            if mode == 'ack_error': assert caught.value is failure
            assert calls == ['commit'] and len(tokens) == 1 and not tokens[0].rollback_complete
            assert await deletes.get('m') is None and (await memory.get(JOB)).target_turn == 9
            assert len(await deleted_events(sp)) == 1
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await sp.aclose()
