"""Real queued SQLite cancellation, quarantine, COMMIT ambiguity and PG waits."""

import asyncio
from contextvars import Context
import functools
import threading

import pytest
import pytest_asyncio

from primer.events import registry
from primer.model.except_ import ConfigError, ProviderError
from primer.model.provider import SqliteConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import needs_postgres, postgres_marks
from tests.storage.test_delete_if import DeleteThing, deleted_events
from tests.storage.test_storage_contract import _pg_config_for_test


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=postgres_marks())])
async def sql_provider(request, tmp_path, monkeypatch):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    p = (SqliteStorageProvider(SqliteConfig(path=tmp_path / "delete-boundary.sqlite"))
         if request.param == "sqlite" else PostgresStorageProvider(_pg_config_for_test().config))
    await p.initialize()
    try:
        yield p
    finally:
        if request.param == "postgres":
            async with p.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA "{p.schema}" CASCADE')
        await p.aclose()


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path, monkeypatch):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "queued-delete.sqlite"))
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


def queued_sql(connection, monkeypatch, prefix):
    """Observe the actual aiosqlite worker queue without replacing execute."""
    original = connection._tx
    queued = asyncio.Event()
    loop = asyncio.get_running_loop()
    class Queue:
        def put_nowait(self, item):
            original.put_nowait(item)
            fn = item[1] if isinstance(item, tuple) else None
            args = fn.args if isinstance(fn, functools.partial) else ()
            if args and isinstance(args[0], str) and args[0].startswith(prefix):
                loop.call_soon_threadsafe(queued.set)
        def __getattr__(self, name):
            return getattr(original, name)
    monkeypatch.setattr(connection, "_tx", Queue())
    return queued


async def block_worker(connection):
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        if not release.wait(5):
            raise RuntimeError("bounded SQLite worker gate expired")
    task = asyncio.create_task(connection._execute(block))
    assert await asyncio.to_thread(entered.wait, 1)
    return release, task


@pytest.mark.parametrize("stage", ["begin", "delete"])
async def test_actual_queued_cancellation_drains_before_peer(sqlite_provider, monkeypatch, stage):
    p = sqlite_provider
    rows = p.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    ready, go, caught, peer_started = (asyncio.Event() for _ in range(4))
    async def owner():
        if stage == "begin":
            await rows.delete_if("m", where={"guard": [1]})
            raise AssertionError("cancelled standalone call returned success")
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            await rows.create(DeleteThing(id="a"), conn=token)
            ready.set()
            await go.wait()
            try:
                await rows.delete_if("m", where={"guard": [1]}, conn=token)
            except asyncio.CancelledError:
                caught.set()
                assert token.failed
            else:
                raise AssertionError("queued DELETE was not cancelled")
    async def peer():
        peer_started.set()
        await rows.update(DeleteThing(id="m", guard=9))
    task = worker = other = None
    release = None
    try:
        queued = queued_sql(p.connection, monkeypatch, "BEGIN IMMEDIATE" if stage == "begin" else "DELETE FROM")
        if stage == "delete":
            task = asyncio.create_task(owner())
            await asyncio.wait_for(ready.wait(), 1)
        release, worker = await block_worker(p.connection)
        if stage == "begin":
            task = asyncio.create_task(owner())
        else:
            go.set()
        await asyncio.wait_for(queued.wait(), 1)
        task.cancel()
        if stage == "delete":
            await asyncio.wait_for(caught.wait(), 1)
        other = asyncio.create_task(peer(), context=Context())
        await asyncio.wait_for(peer_started.wait(), 1)
        await asyncio.sleep(.02)
        task.cancel()
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and not other.done()
        release.set()
        await asyncio.wait_for(worker, 5)
        with pytest.raises(asyncio.CancelledError if stage == "begin" else ConfigError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(other, 5)
        assert (await rows.get("m")).guard == 9
        assert await rows.get("a") is None and await deleted_events(p) == []
        await rows.create(DeleteThing(id="z"))
        assert not p.connection.in_transaction
    finally:
        if release is not None:
            release.set()
        for pending in [task, worker, other]:
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(x for x in [task, worker, other] if x is not None), return_exceptions=True)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("failure", ["cancel", "exception"])
async def test_commit_ambiguity_propagates_atomic_outcome(sql_provider, monkeypatch, durable, failure):
    p = sql_provider
    rows = p.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    reached = asyncio.Event()
    task = None
    if isinstance(p, SqliteStorageProvider):
        original = p.connection.commit
        async def committing():
            if durable:
                await original()
            reached.set()
            if failure == "exception":
                raise RuntimeError("commit acknowledgement lost")
            await asyncio.Event().wait()
        monkeypatch.setattr(p.connection, "commit", committing)
    else:
        from asyncpg.transaction import Transaction
        original = Transaction.commit
        async def committing(transaction):
            if asyncio.current_task() is not task:
                return await original(transaction)
            if durable:
                await original(transaction)
            reached.set()
            if failure == "exception":
                raise RuntimeError("commit acknowledgement lost")
            await asyncio.Event().wait()
        monkeypatch.setattr(Transaction, "commit", committing)
    try:
        task = asyncio.create_task(rows.delete_if("m", where={"guard": [1]}))
        await asyncio.wait_for(reached.wait(), 2)
        if failure == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await asyncio.wait_for(task, 5)
        if isinstance(p, SqliteStorageProvider):
            monkeypatch.setattr(p.connection, "commit", original)
        else:
            monkeypatch.setattr(Transaction, "commit", original)
        assert (await rows.get("m") is None) is durable
        assert len(await deleted_events(p)) == int(durable)
        await rows.create(DeleteThing(id="z"))
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("close_error", [False, True])
async def test_failed_rollback_quarantines_and_does_not_commit(sqlite_provider, monkeypatch, close_error):
    p = sqlite_provider
    rows = p.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    connection = p.connection
    original_close = connection.close
    async def broken_rollback():
        raise OSError("rollback failed")
    async def closed_but_unacknowledged():
        await original_close()
        raise OSError("close acknowledgement failed")
    monkeypatch.setattr(connection, "rollback", broken_rollback)
    if close_error:
        monkeypatch.setattr(connection, "close", closed_but_unacknowledged)
    from primer.storage import sqlite
    original_append = sqlite._append_crud_event
    async def failed_cdc(*args, **kwargs):
        await original_append(*args, **kwargs)
        if kwargs["event_type"].endswith(".deleted"):
            raise ProviderError("CDC failed")
    monkeypatch.setattr(sqlite, "_append_crud_event", failed_cdc)
    with pytest.raises(ConfigError, match="caught storage"):
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            await rows.create(DeleteThing(id="a"), conn=token)
            with pytest.raises(ProviderError):
                await rows.delete_if("m", where={"guard": [1]}, conn=token)
    with pytest.raises(ConfigError):
        _ = p.connection
    second = SqliteStorageProvider(p._config)
    await second.initialize()
    try:
        other = second.get_storage(DeleteThing)
        assert await other.get("m") is not None and await other.get("a") is None
        assert await deleted_events(second) == []
        await other.create(DeleteThing(id="z"))
    finally:
        await second.aclose()


async def test_sqlite_untracked_raw_begin_is_refused_without_rollback(sqlite_provider):
    p = sqlite_provider
    rows = p.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    await p.connection.execute("BEGIN")
    try:
        await p.connection.execute('UPDATE "deletething" SET data = json_set(data, \'$.count\', 8) WHERE id = \'m\'')
        with pytest.raises(ConfigError, match="legacy/raw"):
            await rows.delete_if("m", where={"guard": [1]})
        assert p.connection.in_transaction
        await p.connection.commit()
    finally:
        await p.connection.rollback()
    assert (await rows.get("m")).count == 8


@needs_postgres()
async def test_postgres_cancel_genuine_row_lock_wait(monkeypatch):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    p = PostgresStorageProvider(_pg_config_for_test().config)
    await p.initialize()
    task = None
    try:
        rows = p.get_storage(DeleteThing)
        await rows.create(DeleteThing(id="m"))
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            await rows.get_for_update("m", conn=token)
            task = asyncio.create_task(rows.delete_if("m", where={"guard": [1]}), context=Context())
            async with asyncio.timeout(3):
                while True:
                    async with p.pool.acquire() as c:
                        waiting = await c.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE $1)", f'DELETE FROM "{p.schema}".%')
                    if waiting:
                        break
                    await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        assert await rows.get("m") is not None and await deleted_events(p) == []
        await rows.update(DeleteThing(id="m", guard=9))
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with p.pool.acquire() as c:
            await c.execute(f'DROP SCHEMA "{p.schema}" CASCADE')
        await p.aclose()


def blocked_writer_observer(provider, monkeypatch):
    queued = (queued_sql(provider.connection, monkeypatch, "BEGIN IMMEDIATE")
              if isinstance(provider, SqliteStorageProvider) else None)
    async def observe(task):
        if queued is not None:
            await asyncio.wait_for(queued.wait(), 1)
            await asyncio.sleep(.02)
        else:
            async with asyncio.timeout(3):
                while True:
                    async with provider.pool.acquire() as c:
                        waiting = await c.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE wait_event_type = 'Lock' AND query LIKE $1)",
                            f'DELETE FROM "{provider.schema}".%',
                        )
                    if waiting:
                        break
                    await asyncio.sleep(.01)
        assert not task.done()
    return observe


@pytest.mark.parametrize("new_guard", [2, 3])
async def test_independent_writer_rechecks_multivalue_guard(sql_provider, monkeypatch, new_guard):
    p = sql_provider
    other = (SqliteStorageProvider(p._config) if isinstance(p, SqliteStorageProvider)
             else PostgresStorageProvider(p._config))
    await other.initialize()
    rows, peer_rows = p.get_storage(DeleteThing), other.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    await peer_rows._ensure_table()
    await other._ensure_events_schema()
    started = asyncio.Event()
    observe = blocked_writer_observer(other, monkeypatch)
    task = None
    async def peer():
        started.set()
        return await peer_rows.delete_if("m", where={"guard": [1, 2]})
    try:
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            await rows.patch_if("m", {"guard": new_guard}, where={"guard": [1]}, conn=token)
            task = asyncio.create_task(peer(), context=Context())
            await asyncio.wait_for(started.wait(), 1)
            await observe(task)
        assert await asyncio.wait_for(task, 5) is (new_guard == 2)
        assert (await rows.get("m") is None) is (new_guard == 2)
        assert len(await deleted_events(p)) == int(new_guard == 2)
        if new_guard == 3:
            assert (await rows.get("m")).guard == 3
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await other.aclose()


async def test_two_independent_deleters_have_one_cdc_winner(sql_provider, monkeypatch):
    p = sql_provider
    other = (SqliteStorageProvider(p._config) if isinstance(p, SqliteStorageProvider)
             else PostgresStorageProvider(p._config))
    await other.initialize()
    rows, peer_rows = p.get_storage(DeleteThing), other.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    await peer_rows._ensure_table()
    await other._ensure_events_schema()
    started = asyncio.Event()
    observe = blocked_writer_observer(other, monkeypatch)
    task = None
    async def peer():
        started.set()
        return await peer_rows.delete_if("m", where={"guard": [1]})
    try:
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            assert await rows.delete_if("m", where={"guard": [1]}, conn=token) is True
            task = asyncio.create_task(peer(), context=Context())
            await asyncio.wait_for(started.wait(), 1)
            await observe(task)
        assert await asyncio.wait_for(task, 5) is False
        assert await rows.get("m") is None and len(await deleted_events(p)) == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await other.aclose()


async def test_actual_queued_commit_cancellation_is_atomic_unknown(sqlite_provider, monkeypatch):
    p = sqlite_provider
    rows = p.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    cdc_done, continue_commit, queued = (asyncio.Event() for _ in range(3))
    from primer.storage import sqlite
    original_append = sqlite._append_crud_event
    async def append(*args, **kwargs):
        await original_append(*args, **kwargs)
        if kwargs["event_type"].endswith(".deleted"):
            cdc_done.set()
            await continue_commit.wait()
    monkeypatch.setattr(sqlite, "_append_crud_event", append)
    actual_queue, loop = p.connection._tx, asyncio.get_running_loop()
    class Queue:
        def put_nowait(self, item):
            actual_queue.put_nowait(item)
            fn = item[1] if isinstance(item, tuple) else None
            if isinstance(fn, functools.partial) and getattr(fn.func, "__name__", None) == "commit":
                loop.call_soon_threadsafe(queued.set)
        def __getattr__(self, name):
            return getattr(actual_queue, name)
    monkeypatch.setattr(p.connection, "_tx", Queue())
    task = asyncio.create_task(rows.delete_if("m", where={"guard": [1]}))
    release = worker = None
    try:
        await asyncio.wait_for(cdc_done.wait(), 1)
        release, worker = await block_worker(p.connection)
        continue_commit.set()
        await asyncio.wait_for(queued.wait(), 1)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        assert not task.done()
        release.set()
        await asyncio.wait_for(worker, 3)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        # The queued COMMIT cannot be recalled. Here the real worker executes it
        # before the draining rollback, so both effects are actually durable.
        assert await rows.get("m") is None and len(await deleted_events(p)) == 1
        assert not p.connection.in_transaction
        await rows.create(DeleteThing(id="z"))
    finally:
        continue_commit.set()
        if release is not None:
            release.set()
        for pending in [task, worker]:
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(x for x in [task, worker] if x is not None), return_exceptions=True)


@needs_postgres()
async def test_postgres_missing_delete_grants_no_gap_lock(monkeypatch):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    p = PostgresStorageProvider(_pg_config_for_test().config)
    await p.initialize()
    peer = None
    try:
        rows = p.get_storage(DeleteThing)
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            assert await rows.delete_if("m", where={"guard": [1]}, conn=token) is False
            peer = asyncio.create_task(rows.create(DeleteThing(id="m")), context=Context())
            await asyncio.wait_for(peer, 2)
        assert await rows.get("m") is not None and await deleted_events(p) == []
    finally:
        if peer is not None and not peer.done():
            peer.cancel()
            await asyncio.gather(peer, return_exceptions=True)
        async with p.pool.acquire() as c:
            await c.execute(f'DROP SCHEMA "{p.schema}" CASCADE')
        await p.aclose()
