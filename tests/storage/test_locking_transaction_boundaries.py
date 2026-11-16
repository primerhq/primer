"""Regression boundaries from the independent locking-transaction review."""

import asyncio
from contextvars import Context

import pytest
import pytest_asyncio

from primer.model.common import Identifiable
from primer.model.except_ import ConfigError
from primer.model.provider import PostgresConfig, SqliteConfig
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.pg_gate import needs_postgres
from tests.storage.test_storage_contract import _pg_config_for_test


class BoundaryThing(Identifiable):
    value: int = 0


class UndeclaredThing(Identifiable):
    value: int = 0


@pytest_asyncio.fixture(params=["sqlite", "fake"])
async def provider(request, tmp_path):
    if request.param == "fake":
        yield _FakeStorageProvider()
        return
    storage = SqliteStorageProvider(SqliteConfig(path=tmp_path / "boundary.sqlite"))
    await storage.initialize()
    try:
        yield storage
    finally:
        await storage.aclose()


async def inherited_operation(storage, operation):
    rows = storage.get_storage(BoundaryThing)
    if operation == "read":
        return await rows.get("a")
    if operation == "write":
        return await rows.create(BoundaryThing(id="child", value=4))
    if operation == "undeclared":
        return await storage.get_storage(UndeclaredThing).get("missing")
    if operation == "ordinary":
        async with storage.transaction():
            return "ordinary-entered"
    if operation == "locking":
        async with storage.locking_transaction(models=(BoundaryThing,)):
            return "locking-entered"
    raise AssertionError(operation)


async def check_inherited_refusal(storage, operation):
    rows = storage.get_storage(BoundaryThing)
    await rows.create(BoundaryThing(id="a", value=1))
    async with storage.locking_transaction(models=(BoundaryThing,)) as conn:
        await rows.patch_if("a", {"value": 7}, where={"value": [1]}, conn=conn)
        async def child():
            with pytest.raises(ConfigError, match="inherited"):
                await inherited_operation(storage, operation)
        # An inherited child is not a peer. Refuse rather than exposing the
        # parent's uncommitted value, waiting on its lock, or routing elsewhere.
        await asyncio.wait_for(asyncio.create_task(child()), 0.3)
        assert not conn.failed
    assert (await rows.get("a")).value == 7
    assert await rows.get("child") is None


@pytest.mark.parametrize("operation", ["read", "write", "undeclared", "ordinary", "locking"])
async def test_inherited_tokenless_child_refuses(provider, operation):
    await check_inherited_refusal(provider, operation)




@pytest.fixture
def pg_driver(monkeypatch):
    """Run real storage routing against a controlled driver, never a server."""
    trace = []
    reset_started, finish_reset = asyncio.Event(), asyncio.Event()
    finish_reset.set()
    class Transaction:
        async def start(self):
            trace.append("begin")
        async def commit(self):
            trace.append("commit")
            if getattr(storage.pool, "commit_error", None) is not None:
                raise storage.pool.commit_error
        async def rollback(self):
            trace.append("rollback")
        async def __aenter__(self):
            await self.start()
            return self
        async def __aexit__(self, kind, value, tb):
            await (self.rollback() if kind else self.commit())
    class Connection:
        def __init__(self, name):
            self.name = name
        def transaction(self):
            return Transaction()
        async def fetchrow(self, sql, *args):
            trace.append((self.name, sql))
            return {"id": args[0], "data": args[1] if len(args) > 1 else '{"value":1}'}
    owner_connection, peer_connection = Connection("owner"), Connection("independent")
    class Acquisition:
        def __await__(self):
            async def acquire():
                trace.append("owner-acquire")
                return owner_connection
            return acquire().__await__()
        async def __aenter__(self):
            trace.append("independent-acquire")
            return peer_connection
        async def __aexit__(self, *exc):
            trace.append("independent-release")
    class Pool:
        def acquire(self):
            return Acquisition()
        async def release(self, conn):
            assert conn is owner_connection
            trace.append("reset-start")
            reset_started.set()
            await finish_reset.wait()
            trace.append("reset-done")
    storage = PostgresStorageProvider(PostgresConfig.model_construct(db_schema="public"))
    monkeypatch.setattr(storage, "_pool", Pool())
    # Preparation is not under test here. Every native query still goes through
    # the production model-operation and acquisition code.
    for model in (BoundaryThing, UndeclaredThing):
        monkeypatch.setattr(storage.get_storage(model), "_table_ensured", True)
    return storage, trace, reset_started, finish_reset


@pytest.mark.parametrize("operation", ["read", "write", "undeclared", "ordinary", "locking"])
async def test_inherited_postgres_routing_refuses_before_driver(pg_driver, operation):
    storage, trace, _, _ = pg_driver
    with pytest.raises(ValueError, match="parent rollback"):
        async with storage.locking_transaction(models=(BoundaryThing,)):
            before = list(trace)
            async def child():
                with pytest.raises(ConfigError, match="inherited"):
                    await inherited_operation(storage, operation)
            await asyncio.wait_for(asyncio.create_task(child()), 0.3)
            assert trace == before
            raise ValueError("parent rollback")
    assert trace == ["owner-acquire", "begin", "rollback", "reset-start", "reset-done"]


async def test_clean_release_preserves_first_and_repeated_cancellation(pg_driver):
    storage, trace, reset_started, finish_reset = pg_driver
    finish_reset.clear()
    async def run():
        async with storage.locking_transaction(models=(BoundaryThing,)):
            trace.append("body")
        trace.append("caller-success")
    owner = asyncio.create_task(run())
    try:
        await asyncio.wait_for(reset_started.wait(), 3)
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        finish_reset.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 3)
        assert trace == ["owner-acquire", "begin", "body", "commit", "reset-start", "reset-done"]
    finally:
        finish_reset.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_clean_release_preserves_asyncio_timeout(pg_driver):
    storage, trace, reset_started, finish_reset = pg_driver
    finish_reset.clear()
    async def deadline():
        async with asyncio.timeout(0.02):
            async with storage.locking_transaction(models=(BoundaryThing,)):
                pass
    owner = asyncio.create_task(deadline())
    try:
        await asyncio.wait_for(reset_started.wait(), 3)
        await asyncio.sleep(0.05)
        assert not owner.done()
        finish_reset.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(owner, 3)
        assert trace[-2:] == ["reset-start", "reset-done"]
    finally:
        finish_reset.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_release_cancellation_preserves_prior_body_error(pg_driver):
    storage, trace, reset_started, finish_reset = pg_driver
    finish_reset.clear()
    async def run():
        async with storage.locking_transaction(models=(BoundaryThing,)):
            raise ValueError("original body error")
    owner = asyncio.create_task(run())
    try:
        await asyncio.wait_for(reset_started.wait(), 3)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        finish_reset.set()
        with pytest.raises(ValueError, match="original body error"):
            await asyncio.wait_for(owner, 3)
        assert "commit" not in trace
    finally:
        finish_reset.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def check_pre_sql_refusal(storage):
    rows = storage.get_storage(BoundaryThing)
    await rows.create(BoundaryThing(id="a", value=1))
    await rows.create(BoundaryThing(id="b", value=1))
    async with storage.locking_transaction(models=(BoundaryThing,)) as conn:
        await rows.patch_if("b", {"value": 2}, where={"value": [1]}, conn=conn)
        with pytest.raises(ConfigError, match="ascending"):
            await rows.get_for_update("a", conn=conn)
        assert not conn.failed
        with pytest.raises(ConfigError, match="id must be a string"):
            await rows.get_for_update(None, conn=conn)
        with pytest.raises(ConfigError, match="thread"):
            await rows.get_for_update("b", conn=object())
        assert not conn.failed
        assert (await rows.get_for_update("b", conn=conn)).value == 2
    assert (await rows.get("b")).value == 2


async def test_pre_sql_lock_refusals_do_not_poison(provider):
    await check_pre_sql_refusal(provider)


@needs_postgres()
@pytest.mark.parametrize("operation", ["read", "write", "undeclared", "ordinary", "locking"])
async def test_postgres_inherited_child_boundary(operation):
    storage = PostgresStorageProvider(_pg_config_for_test().config)
    await storage.initialize()
    try:
        await check_inherited_refusal(storage, operation)
    finally:
        async with storage.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{storage.schema}" CASCADE')
        await storage.aclose()


@needs_postgres()
async def test_postgres_pre_sql_lock_refusals_do_not_poison():
    storage = PostgresStorageProvider(_pg_config_for_test().config)
    await storage.initialize()
    try:
        await check_pre_sql_refusal(storage)
    finally:
        async with storage.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{storage.schema}" CASCADE')
        await storage.aclose()


async def test_release_cancellation_preserves_failed_commit(pg_driver, monkeypatch):
    storage, trace, reset_started, finish_reset = pg_driver
    monkeypatch.setattr(storage.pool, "commit_error", ValueError("unknown commit outcome"), raising=False)
    finish_reset.clear()
    async def run():
        async with storage.locking_transaction(models=(BoundaryThing,)):
            pass
    owner = asyncio.create_task(run())
    try:
        await asyncio.wait_for(reset_started.wait(), 3)
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        assert not owner.done()
        finish_reset.set()
        with pytest.raises(ValueError, match="unknown commit outcome"):
            await asyncio.wait_for(owner, 3)
        assert trace == ["owner-acquire", "begin", "commit", "reset-start", "reset-done"]
    finally:
        finish_reset.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def check_post_sql_failure(storage, monkeypatch):
    rows = storage.get_storage(BoundaryThing)
    await rows.create(BoundaryThing(id="a", value=1))
    await rows.create(BoundaryThing(id="b", value=1))
    with pytest.raises(ConfigError, match="caught storage failure"):
        async with storage.locking_transaction(models=(BoundaryThing,)) as conn:
            await rows.patch_if("a", {"value": 9}, where={"value": [1]}, conn=conn)
            original = rows._decode_row
            def failed_decoder(*args, **kwargs):
                raise ConfigError("post-SQL decoder failure")
            with monkeypatch.context() as scoped:
                scoped.setattr(rows, "_decode_row", failed_decoder)
                with pytest.raises(ConfigError, match="post-SQL decoder failure"):
                    await rows.get_for_update("b", conn=conn)
            assert rows._decode_row == original
            assert conn.failed
    assert (await rows.get("a")).value == 1


async def test_sqlite_post_sql_config_error_is_rollback_only(tmp_path, monkeypatch):
    storage = SqliteStorageProvider(SqliteConfig(path=tmp_path / "decode.sqlite"))
    await storage.initialize()
    try:
        await check_post_sql_failure(storage, monkeypatch)
    finally:
        await storage.aclose()


@needs_postgres()
async def test_postgres_post_sql_config_error_is_rollback_only(monkeypatch):
    storage = PostgresStorageProvider(_pg_config_for_test().config)
    await storage.initialize()
    try:
        await check_post_sql_failure(storage, monkeypatch)
    finally:
        async with storage.pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{storage.schema}" CASCADE')
        await storage.aclose()


async def test_sqlite_inherited_read_snapshot_refuses_before_lock(tmp_path):
    storage = SqliteStorageProvider(SqliteConfig(path=tmp_path / "snapshot.sqlite"))
    await storage.initialize()
    try:
        async with storage.locking_transaction(models=(BoundaryThing,)):
            async def child():
                with pytest.raises(ConfigError, match="inherited"):
                    async with storage.read_snapshot():
                        pytest.fail("inherited snapshot entered")
            await asyncio.wait_for(asyncio.create_task(child()), 0.3)
    finally:
        await storage.aclose()


async def test_late_inherited_child_refuses_after_scope_ends(provider):
    rows = provider.get_storage(BoundaryThing)
    await rows.create(BoundaryThing(id="a", value=1))
    proceed = asyncio.Event()
    async def child():
        await proceed.wait()
        with pytest.raises(ConfigError, match="inherited"):
            await rows.get("a")
    owner = None
    try:
        async with provider.locking_transaction(models=(BoundaryThing,)):
            owner = asyncio.create_task(child())
        proceed.set()
        await asyncio.wait_for(owner, 0.3)
        assert (await rows.get("a")).value == 1
    finally:
        if owner is not None:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("operation", ["read", "write", "ordinary"])
async def test_explicit_independent_postgres_peer_retains_legacy_routing(pg_driver, operation):
    storage, trace, _, _ = pg_driver
    with pytest.raises(ValueError, match="parent rollback"):
        async with storage.locking_transaction(models=(BoundaryThing,)):
            result = await asyncio.wait_for(
                asyncio.create_task(inherited_operation(storage, operation), context=Context()), 0.3,
            )
            if operation == "ordinary":
                assert result == "ordinary-entered"
            else:
                assert result.value == (1 if operation == "read" else 4)
            assert "independent-acquire" in trace
            assert "independent-release" in trace
            raise ValueError("parent rollback")
    assert trace[-3:] == ["rollback", "reset-start", "reset-done"]


async def test_postgres_pre_sql_lock_refusal_does_not_touch_driver(pg_driver):
    storage, trace, _, _ = pg_driver
    rows = storage.get_storage(BoundaryThing)
    async with storage.locking_transaction(models=(BoundaryThing,)) as conn:
        await rows.get_for_update("b", conn=conn)
        before = list(trace)
        with pytest.raises(ConfigError, match="ascending"):
            await rows.get_for_update("a", conn=conn)
        with pytest.raises(ConfigError, match="id must be a string"):
            await rows.get_for_update(None, conn=conn)
        with pytest.raises(ConfigError, match="thread"):
            await rows.get_for_update("b", conn=object())
        assert trace == before
        assert not conn.failed
    assert trace[-3:] == ["commit", "reset-start", "reset-done"]
