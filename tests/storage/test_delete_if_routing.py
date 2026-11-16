"""Controlled-driver proofs complement, rather than replace, native PG cases."""

import asyncio
import json

import pytest

from primer.events import registry
from primer.model.except_ import ConfigError, ProviderError
from primer.storage import postgres
from tests.storage.test_delete_if import DeleteThing
from tests.storage.test_locking_transaction_boundaries import pg_driver as pg_driver


@pytest.fixture
def delete_driver(pg_driver, monkeypatch):
    p, trace, reset_started, finish_reset = pg_driver
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    monkeypatch.setattr(p.get_storage(DeleteThing), "_table_ensured", True)
    monkeypatch.setattr(p, "_events_schema_ready", True, raising=False)
    calls = []
    async def append(connection, schema, **kwargs):
        calls.append((connection, asyncio.current_task(), schema, kwargs))
    monkeypatch.setattr(postgres, "_append_crud_event", append)
    return p, trace, reset_started, finish_reset, calls


@pytest.mark.parametrize("timeout", [False, True])
async def test_delete_clean_pool_release_cancellation_propagates(delete_driver, timeout):
    p, trace, reset_started, finish_reset, calls = delete_driver
    finish_reset.clear()
    deadline = None
    async def owner():
        nonlocal deadline
        if timeout:
            async with asyncio.timeout(None) as deadline:
                await p.get_storage(DeleteThing).delete_if("m", where={"guard": [1, 2]})
        else:
            await p.get_storage(DeleteThing).delete_if("m", where={"guard": [1, 2]})
        trace.append("caller-success")
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(reset_started.wait(), 1)
        if timeout:
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.sleep(.01)
        else:
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
        assert not task.done()
        finish_reset.set()
        with pytest.raises(TimeoutError if timeout else asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert trace[-3:] == ["commit", "reset-start", "reset-done"]
        queries = [sql for name, sql in (x for x in trace if isinstance(x, tuple)) if name == "owner"]
        assert len(queries) == 1 and queries[0].startswith('DELETE FROM "public"."deletething"')
        assert "RETURNING id" in queries[0] and "SAVEPOINT" not in queries[0]
        assert len(calls) == 1 and calls[0][0].name == "owner" and calls[0][1] is task
        assert json.loads(calls[0][3]["payload_json"]) == {"id": "m"}
        assert trace.count("owner-acquire") == 1 and "independent-acquire" not in trace
    finally:
        finish_reset.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cleanup", ["release", "rollback"])
async def test_delete_cleanup_failure_surfaces_without_commit(delete_driver, monkeypatch, cleanup):
    p, trace, _, _, _ = delete_driver
    async def failed_cdc(*args, **kwargs):
        raise ProviderError("CDC failed")
    monkeypatch.setattr(postgres, "_append_crud_event", failed_cdc)
    if cleanup == "release":
        async def release(connection):
            raise OSError("pool release failed")
        monkeypatch.setattr(p.pool, "release", release)
    else:
        connection = await p.pool.acquire()
        transaction_type = type(connection.transaction())
        async def rollback(transaction):
            raise OSError("rollback failed")
        async def close():
            trace.append("closed")
        monkeypatch.setattr(transaction_type, "rollback", rollback)
        monkeypatch.setattr(connection, "close", close, raising=False)
    # Retain the delivered cleanup precedence: a failing pool release surfaces
    # its own error with the caught-operation rollback refusal chained beneath.
    with pytest.raises(OSError if cleanup == "release" else ConfigError) as caught:
        async with p.locking_transaction(models=(DeleteThing,)) as token:
            with pytest.raises(ProviderError):
                await p.get_storage(DeleteThing).delete_if("m", where={"guard": [1]}, conn=token)
            assert token.failed
    if cleanup == "release":
        assert isinstance(caught.value.__context__, ConfigError)
    assert "commit" not in trace
    if cleanup == "rollback":
        assert "closed" in trace
