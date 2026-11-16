"""The opt-in locking transaction must fence peer writers and roll back."""

import asyncio
import sys

import pytest
import pytest_asyncio

from primer.model.common import Identifiable
from primer.model.except_ import ConfigError
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.pg_gate import needs_postgres
from tests.storage.test_storage_contract import _pg_config_for_test


class LockingThing(Identifiable):
    value: int = 0


class OtherThing(Identifiable):
    value: int = 0


@pytest_asyncio.fixture(params=["sqlite", "fake"])
async def provider(request, tmp_path):
    if request.param == "fake":
        yield _FakeStorageProvider()
        return
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "locking.sqlite"))
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


async def check_lock(provider):
    store = provider.get_storage(LockingThing)
    await store.create(LockingThing(id="a", value=1))
    started = asyncio.Event()

    async def writer():
        started.set()
        await store.patch_if("a", {"value": 3}, where={"value": [1, 2]})

    async with provider.locking_transaction(models=(LockingThing,)) as conn:
        assert (await store.get_for_update("a", conn=conn)).value == 1
        assert await store.get_for_update("missing", conn=conn) is None
        await store.patch_if("a", {"value": 2}, where={"value": [1]}, conn=conn)
        task = asyncio.create_task(writer())
        await started.wait()
        await asyncio.sleep(0.1)
        assert not task.done()
    await asyncio.wait_for(task, 5)
    assert (await store.get("a")).value == 3


@pytest.mark.asyncio
async def test_lock_and_commit(provider):
    await check_lock(provider)


@pytest.mark.asyncio
async def test_rollback_cancellation_and_capability_checks(provider):
    store = provider.get_storage(LockingThing)
    await store.create(LockingThing(id="a", value=1))
    async with provider.locking_transaction(models=(LockingThing,)) as conn:
        with pytest.raises(ConfigError):
            await store.get_for_update("a", conn=object())
        with pytest.raises(ConfigError):
            await provider.get_storage(OtherThing).get_for_update("a", conn=conn)
        with pytest.raises(ConfigError):
            async with provider.locking_transaction(models=(LockingThing,)):
                pass
        async def wrong_task():
            with pytest.raises(ConfigError):
                await store.get_for_update("a", conn=conn)
        await asyncio.create_task(wrong_task())
    with pytest.raises(ConfigError):
        await store.get_for_update("a", conn=conn)

    entered = asyncio.Event()
    async def cancelled():
        async with provider.locking_transaction(models=(LockingThing,)) as txn:
            await store.get_for_update("a", conn=txn)
            await store.patch_if("a", {"value": 9}, where={"value": [1]}, conn=txn)
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(cancelled())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.get("a")).value == 1
    async with provider.locking_transaction(models=(LockingThing,)) as txn:
        assert (await store.get_for_update("a", conn=txn)).value == 1


@pytest.mark.asyncio
async def test_multiple_models_rollback_as_one(provider):
    a = provider.get_storage(LockingThing)
    b = provider.get_storage(OtherThing)
    await a.create(LockingThing(id="a", value=1))
    with pytest.raises(RuntimeError):
        async with provider.locking_transaction(models=(LockingThing, OtherThing)) as conn:
            await a.get_for_update("a", conn=conn)
            await a.patch_if("a", {"value": 2}, where={"value": [1]}, conn=conn)
            await b.create(OtherThing(id="b", value=3), conn=conn)
            raise RuntimeError("abort")
    assert (await a.get("a")).value == 1
    assert await b.get("b") is None


@pytest.mark.asyncio
async def test_fake_old_snapshot_cannot_mutate_locked_row():
    provider = _FakeStorageProvider()
    store = provider.get_storage(LockingThing)
    old = await store.create(LockingThing(id="a", value=1))
    async with provider.locking_transaction(models=(LockingThing,)) as conn:
        old.value = 99
        assert (await store.get_for_update("a", conn=conn)).value == 1
    assert (await store.get("a")).value == 1


@pytest.mark.asyncio
async def test_sqlite_peer_process_blocked_until_commit(tmp_path):
    path = tmp_path / "peer.sqlite"
    p = SqliteStorageProvider(SqliteConfig(path=path))
    await p.initialize()
    try:
        store = p.get_storage(LockingThing)
        await store.create(LockingThing(id="a", value=1))
        script = """
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=5)
print('ready', flush=True)
c.execute('UPDATE lockingthing SET data=json_set(data, ?, 7) WHERE id=?', ('$.value', 'a'))
c.commit()
print('committed', flush=True)
"""
        async with p.locking_transaction(models=(LockingThing,)) as conn:
            assert (await store.get_for_update("a", conn=conn)).value == 1
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 0.15)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        assert b"committed" in stdout
        assert (await store.get("a")).value == 7
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_sqlite_cancel_while_waiting_to_begin(tmp_path, monkeypatch):
    path = tmp_path / "two-providers.sqlite"
    first = SqliteStorageProvider(SqliteConfig(path=path))
    second = SqliteStorageProvider(SqliteConfig(path=path))
    await first.initialize()
    await second.initialize()
    try:
        store = first.get_storage(LockingThing)
        await store.create(LockingThing(id="a", value=1))
        # Prepare before the first provider holds the database write lock.
        await second.get_storage(LockingThing).get("a")
        cleanup = asyncio.Event()
        rollback = second.connection.rollback
        async def observed_rollback():
            cleanup.set()
            await rollback()
        monkeypatch.setattr(second.connection, "rollback", observed_rollback)
        async def contender():
            async with second.locking_transaction(models=(LockingThing,)):
                pytest.fail("cancelled contender entered")
        async with first.locking_transaction(models=(LockingThing,)):
            task = asyncio.create_task(contender())
            await asyncio.sleep(0.1)
            assert not task.done()
            task.cancel()
            await asyncio.wait_for(cleanup.wait(), 3)
            task.cancel()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        async with second.locking_transaction(models=(LockingThing,)) as conn:
            assert (await second.get_storage(LockingThing).get_for_update("a", conn=conn)).value == 1
    finally:
        await second.aclose()
        await first.aclose()


@pytest.mark.asyncio
async def test_sqlite_cancel_after_commit_has_unknown_outcome(tmp_path, monkeypatch):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "commit.sqlite"))
    await p.initialize()
    try:
        store = p.get_storage(LockingThing)
        await store.create(LockingThing(id="a", value=1))
        original_commit = p.connection.commit
        committed = asyncio.Event()
        async def commit_then_pause():
            await original_commit()
            committed.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(p.connection, "commit", commit_then_pause)
        async def transaction():
            async with p.locking_transaction(models=(LockingThing,)) as conn:
                await store.get_for_update("a", conn=conn)
                await store.patch_if("a", {"value": 9}, where={"value": [1]}, conn=conn)
        task = asyncio.create_task(transaction())
        await committed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(p.connection, "commit", original_commit)
        assert (await store.get("a")).value == 9
        async with p.locking_transaction(models=(LockingThing,)) as conn:
            assert (await store.get_for_update("a", conn=conn)).value == 9
    finally:
        await p.aclose()


async def check_event_atomicity(provider, monkeypatch):
    from primer.events import registry
    monkeypatch.setitem(registry._KIND_BY_MODEL, OtherThing, "locking_test")
    store = provider.get_storage(OtherThing)
    async def count():
        if isinstance(provider, SqliteStorageProvider):
            cur = await provider.connection.execute("SELECT COUNT(*) FROM events WHERE entity_kind = 'locking_test'")
            return (await cur.fetchone())[0]
        async with provider.pool.acquire() as conn:
            return await conn.fetchval(f'SELECT COUNT(*) FROM "{provider.schema}".events WHERE entity_kind = $1', "locking_test")
    with pytest.raises(RuntimeError):
        async with provider.locking_transaction(models=(OtherThing,)) as conn:
            await store.create(OtherThing(id="event-rollback"), conn=conn)
            raise RuntimeError("abort")
    assert await store.get("event-rollback") is None
    assert await count() == 0
    async with provider.locking_transaction(models=(OtherThing,)) as conn:
        await store.create(OtherThing(id="event-commit"), conn=conn)
    assert await count() == 1


@pytest.mark.asyncio
async def test_sqlite_cdc_atomicity(tmp_path, monkeypatch):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "cdc.sqlite"))
    await provider.initialize()
    try:
        await check_event_atomicity(provider, monkeypatch)
    finally:
        await provider.aclose()


@needs_postgres()
@pytest.mark.asyncio
async def test_postgres_lock_and_rollback(monkeypatch):
    from primer.storage.postgres import PostgresStorageProvider
    cfg = _pg_config_for_test().config
    p = PostgresStorageProvider(cfg)
    await p.initialize()
    try:
        await check_lock(p)
        await p.get_storage(LockingThing).delete("a")
        await test_rollback_cancellation_and_capability_checks(p)
        await p.get_storage(LockingThing).delete("a")
        await test_multiple_models_rollback_as_one(p)

        # Cancel a distinct connection while SELECT FOR UPDATE is blocked.
        store = p.get_storage(LockingThing)
        entered = asyncio.Event()
        async def blocked():
            async with p.locking_transaction(models=(LockingThing,)) as conn:
                entered.set()
                await store.get_for_update("a", conn=conn)
        async with p.locking_transaction(models=(LockingThing,)) as conn:
            await store.get_for_update("a", conn=conn)
            task = asyncio.create_task(blocked())
            await entered.wait()
            await asyncio.sleep(0.1)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with p.locking_transaction(models=(LockingThing,)) as conn:
            assert (await store.get_for_update("a", conn=conn)).value == 1
        await check_event_atomicity(p, monkeypatch)
    finally:
        async with p.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{cfg.db_schema}" CASCADE')
        await p.aclose()


@pytest.mark.asyncio
async def test_ordering_ownership_and_keyword_writes(provider):
    store = provider.get_storage(LockingThing)
    await store.create(LockingThing(id="a", value=1))
    await store.create(LockingThing(id="b", value=1))
    async with provider.locking_transaction(models=(LockingThing,)) as conn:
        await store.get_for_update("a", conn=conn)
        await store.get_for_update("b", conn=conn)
        # Revisit a held row and use the keyword spelling of a write.
        await store.patch_if(id="a", patch={"value": 2}, where={"value": [1]}, conn=conn)
        with pytest.raises(ConfigError, match="thread"):
            await store.get("a")
        with pytest.raises(ConfigError, match="prepared"):
            await provider.get_storage(OtherThing).create(OtherThing(id="x"), conn=conn)
        async def steal():
            with pytest.raises(ConfigError, match="another task"):
                await store.patch_if("a", {"value": 99}, where={"value": [2]}, conn=conn)
        await asyncio.create_task(steal())
    with pytest.raises(ConfigError, match="ended"):
        await store.get("a", conn=conn)
    assert (await store.get("a")).value == 2

    with pytest.raises(ConfigError, match="ascending"):
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            await store.get_for_update("b", conn=conn)
            await store.get_for_update("a", conn=conn)
    assert (await store.get("b")).value == 1


@pytest.mark.asyncio
async def test_writes_cannot_bypass_order_and_caught_error_rolls_back(provider):
    store = provider.get_storage(LockingThing)
    with pytest.raises(ConfigError, match="caught storage failure"):
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            await store.create(LockingThing(id="b"), conn=conn)
            # The failed write is caught in user code, but cannot commit b.
            with pytest.raises(Exception):
                await store.create(LockingThing(id="b"), conn=conn)
    assert await store.get("b") is None
    with pytest.raises(ConfigError, match="ascending"):
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            await store.create(LockingThing(id="b"), conn=conn)
            await store.create(LockingThing(id="a"), conn=conn)
    assert await store.get("b") is None


@pytest.mark.asyncio
async def test_multi_model_order_and_wrong_provider(provider, tmp_path):
    other = SqliteStorageProvider(SqliteConfig(path=tmp_path / "foreign.sqlite"))
    await other.initialize()
    try:
        async with provider.locking_transaction(models=(OtherThing, LockingThing)) as conn:
            await provider.get_storage(LockingThing).get_for_update("a", conn=conn)
            await provider.get_storage(OtherThing).get_for_update("a", conn=conn)
            with pytest.raises(ConfigError, match="another task/provider"):
                await other.get_storage(LockingThing).get("a", conn=conn)
            with pytest.raises(ConfigError, match="re-entrant"):
                async with other.locking_transaction(models=(LockingThing,)):
                    pass
    finally:
        await other.aclose()
    for models in [(), (LockingThing, LockingThing), (str,), [LockingThing]]:
        with pytest.raises(ConfigError):
            async with provider.locking_transaction(models=models):
                pytest.fail("invalid model declaration entered")


def test_unsupported_backends_fail_closed():
    from primer.int.storage_provider import StorageProvider
    with pytest.raises(NotImplementedError):
        StorageProvider.locking_transaction(object(), models=(LockingThing,))


@pytest.mark.asyncio
async def test_unsupported_row_lock_refuses():
    from primer.int.storage import Storage
    with pytest.raises(NotImplementedError):
        await Storage.get_for_update(object(), "a", conn=object())


@pytest.mark.asyncio
async def test_sqlite_repeated_cancel_drains_real_queued_rollback(tmp_path, monkeypatch):
    import threading
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "queued-rollback.sqlite"))
    await provider.initialize()
    release = threading.Event()
    busy = threading.Event()
    rollback_queued = asyncio.Event()
    store = provider.get_storage(LockingThing)
    owner = None
    blocker = None
    try:
        await store.create(LockingThing(id="a", value=1))
        def block():
            busy.set()
            if not release.wait(5):
                raise RuntimeError("test queue blocker timed out")
            return 1
        await provider.connection.create_function("locking_test_block", 0, block)
        entered = asyncio.Event()
        async def run():
            async with provider.locking_transaction(models=(LockingThing,)) as conn:
                await store.patch_if("a", {"value": 9}, where={"value": [1]}, conn=conn)
                entered.set()
                await asyncio.Event().wait()
        owner = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(), 3)
        blocker = asyncio.create_task(provider.connection.execute("SELECT locking_test_block()"))
        assert await asyncio.to_thread(busy.wait, 3)
        rollback = provider.connection.rollback
        async def observed_rollback():
            rollback_queued.set()
            await rollback()
        monkeypatch.setattr(provider.connection, "rollback", observed_rollback)
        owner.cancel()
        await asyncio.wait_for(rollback_queued.wait(), 3)
        owner.cancel()
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        release.set()
        await blocker
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 3)
        assert (await store.get("a")).value == 1
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            assert (await store.get_for_update("a", conn=conn)).value == 1
    finally:
        release.set()
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)
        if owner is not None:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        await provider.aclose()


@pytest.mark.asyncio
async def test_sqlite_queued_commit_cancel_is_unknown_but_connection_is_reusable(tmp_path, monkeypatch):
    import threading
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "queued-commit.sqlite"))
    await provider.initialize()
    busy, release = threading.Event(), threading.Event()
    queued = asyncio.Event()
    owner = blocker = None
    try:
        store = provider.get_storage(LockingThing)
        await store.create(LockingThing(id="a", value=1))
        def block():
            busy.set()
            if not release.wait(5):
                raise RuntimeError("test queue blocker timed out")
            return 1
        await provider.connection.create_function("locking_test_block", 0, block)
        commit = provider.connection.commit
        async def observed_commit():
            queued.set()
            await commit()
        monkeypatch.setattr(provider.connection, "commit", observed_commit)
        entered, leave = asyncio.Event(), asyncio.Event()
        async def run():
            async with provider.locking_transaction(models=(LockingThing,)) as conn:
                await store.patch_if("a", {"value": 9}, where={"value": [1]}, conn=conn)
                entered.set()
                await leave.wait()
        owner = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(), 3)
        blocker = asyncio.create_task(provider.connection.execute("SELECT locking_test_block()"))
        assert await asyncio.to_thread(busy.wait, 3)
        leave.set()
        await asyncio.wait_for(queued.wait(), 3)
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        assert not owner.done()
        release.set()
        await blocker
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 3)
        monkeypatch.setattr(provider.connection, "commit", commit)
        # The actual queued COMMIT wins. Cancellation must not be reported as rollback.
        assert (await store.get("a")).value == 9
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            assert (await store.get_for_update("a", conn=conn)).value == 9
    finally:
        release.set()
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)
        if owner is not None:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        await provider.aclose()


@pytest.mark.asyncio
async def test_missing_row_is_not_a_held_lock_for_later_ordering(provider):
    store = provider.get_storage(LockingThing)
    await store.create(LockingThing(id="b"))
    with pytest.raises(ConfigError, match="ascending"):
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            assert await store.get_for_update("a", conn=conn) is None
            await store.get_for_update("b", conn=conn)
            await store.create(LockingThing(id="a"), conn=conn)
    assert await store.get("a") is None


@needs_postgres()
@pytest.mark.asyncio
async def test_postgres_order_ownership_and_missing_row_contract(tmp_path):
    from primer.storage.postgres import PostgresStorageProvider
    cfg = _pg_config_for_test().config
    provider = PostgresStorageProvider(cfg)
    await provider.initialize()
    try:
        await test_ordering_ownership_and_keyword_writes(provider)
        for entity_id in ("a", "b"):
            await provider.get_storage(LockingThing).delete(entity_id)
        await test_writes_cannot_bypass_order_and_caught_error_rolls_back(provider)
        await test_multi_model_order_and_wrong_provider(provider, tmp_path)
        await test_missing_row_is_not_a_held_lock_for_later_ordering(provider)
        store = provider.get_storage(LockingThing)
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            assert await store.get_for_update("z", conn=conn) is None
            # SELECT FOR UPDATE on absence does not exclude a peer insertion.
            await asyncio.wait_for(asyncio.create_task(store.create(LockingThing(id="z"))), 3)
        assert await store.get("z") is not None
    finally:
        async with provider.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{cfg.db_schema}" CASCADE')
        await provider.aclose()


@pytest.mark.asyncio
async def test_sqlite_failed_rollback_invalidates_connection_and_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "failed-cleanup.sqlite"
    provider = SqliteStorageProvider(SqliteConfig(path=path))
    await provider.initialize()
    try:
        store = provider.get_storage(LockingThing)
        await store.create(LockingThing(id="a", value=1))
        async def failed_rollback():
            raise RuntimeError("injected rollback failure")
        monkeypatch.setattr(provider.connection, "rollback", failed_rollback)
        with pytest.raises(ValueError, match="original body failure"):
            async with provider.locking_transaction(models=(LockingThing,)) as conn:
                await store.patch_if("a", {"value": 9}, where={"value": [1]}, conn=conn)
                raise ValueError("original body failure")
        with pytest.raises(Exception, match=r"used before initialize\(\)"):
            await store.get("a")
    finally:
        await provider.aclose()
    reopened = SqliteStorageProvider(SqliteConfig(path=path))
    await reopened.initialize()
    try:
        assert (await reopened.get_storage(LockingThing).get("a")).value == 1
    finally:
        await reopened.aclose()


async def check_caught_cdc_failure(provider, monkeypatch):
    from primer.events import registry
    import primer.storage.postgres as postgres_backend
    import primer.storage.sqlite as sqlite_backend
    monkeypatch.setitem(registry._KIND_BY_MODEL, OtherThing, "locking_test")
    rows = provider.get_storage(OtherThing)
    await rows.create(OtherThing(id="cdc", value=1))
    async def fail_event(*args, **kwargs):
        raise RuntimeError("injected event failure")
    backend = sqlite_backend if isinstance(provider, SqliteStorageProvider) else postgres_backend
    monkeypatch.setattr(backend, "_append_crud_event", fail_event)
    with pytest.raises(ConfigError, match="caught storage failure"):
        async with provider.locking_transaction(models=(OtherThing,)) as conn:
            with pytest.raises(Exception, match="injected event failure"):
                await rows.patch_if("cdc", {"value": 9}, where={"value": [1]}, conn=conn)
    assert (await rows.get("cdc")).value == 1


@pytest.mark.asyncio
async def test_sqlite_caught_cdc_failure_is_rollback_only(tmp_path, monkeypatch):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "cdc-failure.sqlite"))
    await provider.initialize()
    try:
        await check_caught_cdc_failure(provider, monkeypatch)
    finally:
        await provider.aclose()


@needs_postgres()
@pytest.mark.asyncio
async def test_postgres_caught_cdc_failure_is_rollback_only(monkeypatch):
    from primer.storage.postgres import PostgresStorageProvider
    cfg = _pg_config_for_test().config
    provider = PostgresStorageProvider(cfg)
    await provider.initialize()
    try:
        await check_caught_cdc_failure(provider, monkeypatch)
    finally:
        async with provider.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{cfg.db_schema}" CASCADE')
        await provider.aclose()


@pytest.mark.asyncio
async def test_regular_and_locking_transactions_do_not_mix(provider):
    async with provider.transaction():
        with pytest.raises(ConfigError, match="re-entrant"):
            async with provider.locking_transaction(models=(LockingThing,)):
                pytest.fail("nested locking transaction entered")
    async with provider.locking_transaction(models=(LockingThing,)):
        with pytest.raises(ConfigError):
            async with provider.transaction():
                pytest.fail("nested ordinary transaction entered")


@pytest.mark.asyncio
async def test_postgres_repeated_cancellation_drains_rollback_and_pool_reset(monkeypatch):
    """Control the driver boundary without connecting to any database."""
    from primer.model.provider import PostgresConfig
    from primer.storage.postgres import PostgresStorageProvider

    started, rollback_started, reset_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rollback_done, reset_done = asyncio.Event(), asyncio.Event()
    class Transaction:
        async def start(self):
            started.set()
        async def rollback(self):
            rollback_started.set()
            await rollback_done.wait()
        async def commit(self):
            pytest.fail("cancelled unit must not commit")
    class Connection:
        def transaction(self):
            return Transaction()
    connection = Connection()
    released = []
    class Pool:
        async def acquire(self):
            return connection
        async def release(self, conn):
            assert conn is connection
            reset_started.set()
            await reset_done.wait()
            released.append(conn)
    class Handle:
        async def _ensure_table(self):
            pass
    provider = PostgresStorageProvider(PostgresConfig.model_construct(db_schema="public"))
    monkeypatch.setattr(provider, "_pool", Pool())
    monkeypatch.setattr(provider, "get_storage", lambda model: Handle())
    observed = []
    async def run():
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            observed.append(conn)
            await asyncio.Event().wait()
    owner = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.wait_for(rollback_started.wait(), 3)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        rollback_done.set()
        await asyncio.wait_for(reset_started.wait(), 3)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        reset_done.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 3)
        assert released == [connection]
        with pytest.raises(ConfigError, match="ended"):
            observed[0].check(provider, LockingThing)
    finally:
        rollback_done.set()
        reset_done.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_rejected_patch_current_is_not_a_held_lock(provider):
    store = provider.get_storage(LockingThing)
    await store.create(LockingThing(id="a", value=1))
    await store.create(LockingThing(id="b", value=1))
    with pytest.raises(ConfigError, match="ascending"):
        async with provider.locking_transaction(models=(LockingThing,)) as conn:
            written, current = await store.patch_if_or_current("a", {"value": 2}, where={"value": [99]}, conn=conn)
            assert written is None and current.value == 1
            await store.get_for_update("b", conn=conn)
            await store.patch_if("a", {"value": 2}, where={"value": [1]}, conn=conn)
    assert (await store.get("a")).value == 1


@needs_postgres()
@pytest.mark.asyncio
async def test_postgres_two_ordered_units_serialize_and_rejected_cas_is_not_a_lock():
    from primer.storage.postgres import PostgresStorageProvider
    cfg = _pg_config_for_test().config
    provider = PostgresStorageProvider(cfg)
    await provider.initialize()
    first_locked, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks = []
    try:
        await test_rejected_patch_current_is_not_a_held_lock(provider)
        store = provider.get_storage(LockingThing)
        async def unit(first):
            async with provider.locking_transaction(models=(LockingThing,)) as conn:
                if not first:
                    second_started.set()
                a = await store.get_for_update("a", conn=conn)
                if first:
                    first_locked.set()
                    await release.wait()
                b = await store.get_for_update("b", conn=conn)
                await store.patch_if("a", {"value": a.value + 1}, where={"value": [a.value]}, conn=conn)
                await store.patch_if("b", {"value": b.value + 1}, where={"value": [b.value]}, conn=conn)
        tasks.append(asyncio.create_task(unit(True)))
        await asyncio.wait_for(first_locked.wait(), 3)
        tasks.append(asyncio.create_task(unit(False)))
        await asyncio.wait_for(second_started.wait(), 3)
        await asyncio.sleep(0.1)
        assert not tasks[1].done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert (await store.get("a")).value == 3
        assert (await store.get("b")).value == 3
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with provider.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{cfg.db_schema}" CASCADE')
        await provider.aclose()
