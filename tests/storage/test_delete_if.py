"""Opt-in guarded delete: exact guards, explicit ownership and atomic CDC."""

import asyncio
from contextvars import Context
import json
from typing import Any

from pydantic import Field
import pytest
import pytest_asyncio

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.events import registry
from primer.int.storage import Storage
from primer.model.common import Identifiable
from primer.model.except_ import ConfigError, ProviderError
from primer.model.provider import SqliteConfig
from primer.storage._locking import LockingTransaction, model_operation
from primer.storage._patch import PatchSpecError
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.pg_gate import postgres_marks
from tests.storage.test_storage_contract import _pg_config_for_test


class DeleteThing(Identifiable):
    guard: Any = 1
    nested: dict[str, Any] = Field(default_factory=dict)
    count: int = 7


class OtherDeleteThing(Identifiable):
    guard: int = 1


@pytest_asyncio.fixture(params=["sqlite", "fake", pytest.param("postgres", marks=postgres_marks())])
async def provider(request, tmp_path, monkeypatch):
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    if request.param == "fake":
        yield _FakeStorageProvider()
        return
    p = (SqliteStorageProvider(SqliteConfig(path=tmp_path / "delete.sqlite"))
         if request.param == "sqlite" else PostgresStorageProvider(_pg_config_for_test().config))
    await p.initialize()
    try:
        yield p
    finally:
        if request.param == "postgres":
            async with p.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA "{p.schema}" CASCADE')
        await p.aclose()


async def deleted_events(provider):
    return await provider.get_event_store().read_after(0, event_type_prefix="delete_test.deleted")


async def test_unsupported_backend_refuses_without_fallback():
    with pytest.raises(NotImplementedError):
        await Storage.delete_if(object(), "a", where={"guard": [1]})


@pytest.mark.parametrize(("value", "allowed", "match"), [
    (1, [1, 2], True), (1.0, [1], True), (True, [1], False),
    (1, [True], False), ("1", [1], False), (1, ["1"], False),
    (None, [None], True), (False, [False], True), (False, [0], False),
])
async def test_typed_guards(provider, value, allowed, match):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="a", guard=value))
    assert await rows.delete_if("a", where={"guard": allowed}) is match
    assert (await rows.get("a") is None) is match
    assert len(await deleted_events(provider)) == int(match)


@pytest.mark.parametrize("nested", [{"value": "yes"}, {"value": None}, {}, {"value": 7}])
async def test_nested_and_multiple_guards(provider, nested):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="a", nested=nested))
    expected = nested.get("value") in (None, "yes")
    assert await rows.delete_if("a", where={("nested", "value"): [None, "yes"], "guard": [1]}) is expected
    assert await rows.delete_if("missing", where={"guard": [1]}) is False
    assert len(await deleted_events(provider)) == int(expected)


async def test_old_defaulted_guard(provider):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="a"))
    raw = {"guard": 1, "nested": {}}
    if isinstance(provider, _FakeStorageProvider):
        rows.seed_raw("a", raw)
    else:
        async with provider.transaction() as c:
            table = rows.locking_identity()[2]
            if isinstance(provider, SqliteStorageProvider):
                await c.execute(f'UPDATE "{table}" SET data = ? WHERE id = ?', (json.dumps(raw), "a"))
            else:
                await c.execute(f'UPDATE "{provider.schema}"."{table}" SET data = $1::jsonb WHERE id = $2', json.dumps(raw), "a")
    assert await rows.delete_if("a", where={"count": [7]}) is True


@pytest.mark.parametrize("where", [{}, {"guard": []}, {"guard": "1"}, {"id": ["a"]},
                                   {"guard": [float("nan")]}, {("guard",): [1], "guard": [2]}])
async def test_bad_guards_refuse_before_entering_unit(provider, where, monkeypatch):
    rows = provider.get_storage(DeleteThing)
    def forbidden(**kwargs):
        raise AssertionError("malformed guard reached transaction entry")
    monkeypatch.setattr(provider, "locking_transaction", forbidden)
    with pytest.raises(PatchSpecError):
        await rows.delete_if("missing", where=where)


@pytest.mark.parametrize("explicit", [False, True])
async def test_legacy_transaction_refusal_preserves_legacy_writes(provider, explicit):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    async with provider.transaction() as raw:
        await rows.create(DeleteThing(id="a"), conn=raw)
        with pytest.raises(ConfigError):
            await rows.delete_if("m", where={"guard": [1]}, **({"conn": raw} if explicit else {}))
        await rows.create(DeleteThing(id="z"), conn=raw)
    assert await rows.get("a") is not None and await rows.get("m") is not None and await rows.get("z") is not None
    assert await deleted_events(provider) == []


async def test_raw_connection_refusal(provider):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    with pytest.raises(ConfigError):
        await rows.delete_if("m", where={"guard": [1]}, conn=object())
    assert await rows.get("m") is not None


async def test_token_boundaries_do_not_poison_owner(provider):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    other = _FakeStorageProvider().get_storage(DeleteThing)
    async with provider.locking_transaction(models=(DeleteThing,)) as token:
        for target, kwargs in [(rows, {}), (rows, {"conn": object()}),
                               (other, {"conn": token}),
                               (provider.get_storage(OtherDeleteThing), {"conn": token})]:
            with pytest.raises(ConfigError):
                await target.delete_if("m", where={"guard": [1]}, **kwargs)
        async def child(explicit):
            with pytest.raises(ConfigError):
                await rows.delete_if("m", where={"guard": [1]}, **({"conn": token} if explicit else {}))
        await asyncio.wait_for(asyncio.create_task(child(False)), 1)
        await asyncio.wait_for(asyncio.create_task(child(True)), 1)
        assert not token.failed
        assert await rows.delete_if("m", where={"guard": [1]}, conn=token) is True
    with pytest.raises(ConfigError):
        await rows.delete_if("m", where={"guard": [1]}, conn=token)
    forged = LockingTransaction(provider, None, (DeleteThing,))
    with pytest.raises(ConfigError):
        await rows.delete_if("m", where={"guard": [1]}, conn=forged)


async def test_false_receipt_does_not_bypass_generic_order(provider):
    rows = provider.get_storage(DeleteThing)
    @model_operation
    async def delete_if(self, id, *, conn):
        return False
    async with provider.locking_transaction(models=(DeleteThing,)) as token:
        assert await delete_if(rows, "b", conn=token) is False
        await rows.create(DeleteThing(id="c"), conn=token)
        with pytest.raises(ConfigError, match="ascending"):
            await delete_if(rows, "b", conn=token)
        assert not token.failed


@pytest.mark.parametrize("exists", [False, True])
async def test_false_attempt_keeps_order_high_water(provider, exists):
    rows = provider.get_storage(DeleteThing)
    if exists:
        await rows.create(DeleteThing(id="b"))
    async with provider.locking_transaction(models=(DeleteThing,)) as token:
        assert await rows.delete_if("b", where={"guard": [99]}, conn=token) is False
        await rows.create(DeleteThing(id="c"), conn=token)
        with pytest.raises(ConfigError, match="ascending"):
            await rows.delete_if("b", where={"guard": [1]}, conn=token)
        assert not token.failed


async def test_true_key_can_be_revisited_after_later_key(provider):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="b"))
    async with provider.locking_transaction(models=(DeleteThing,)) as token:
        assert await rows.delete_if("b", where={"guard": [1]}, conn=token) is True
        await rows.create(DeleteThing(id="c"), conn=token)
        assert await rows.delete_if("b", where={"guard": [1]}, conn=token) is False


async def test_cdc_actor_and_outer_rollback(provider):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    with pytest.raises(RuntimeError):
        async with provider.locking_transaction(models=(DeleteThing,)) as token:
            await rows.create(DeleteThing(id="a"), conn=token)
            assert await rows.delete_if("m", where={"guard": [1]}, conn=token) is True
            raise RuntimeError("abort")
    assert await rows.get("a") is None and await rows.get("m") is not None
    assert await deleted_events(provider) == []
    with acting_as(Person("delete-person", "user")):
        assert await rows.delete_if("m", where={"guard": [1]}) is True
    events = await deleted_events(provider)
    assert len(events) == 1 and events[0].actor == "user:delete-person"
    assert events[0].payload == {"id": "m"} and events[0].entity_id == "m"


@pytest.mark.parametrize("cancel", [False, True])
async def test_caught_cdc_failure_aborts_whole_unit(provider, monkeypatch, cancel):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    error = asyncio.CancelledError if cancel else ProviderError
    if isinstance(provider, _FakeStorageProvider):
        events = provider.get_event_store()
        original = events.append
        async def failing(**kwargs):
            value = await original(**kwargs)
            if kwargs["event_type"].endswith(".deleted"):
                raise error("CDC failed after append")
            return value
        monkeypatch.setattr(events, "append", failing)
    else:
        from primer.storage import sqlite, postgres
        module = sqlite if isinstance(provider, SqliteStorageProvider) else postgres
        original = module._append_crud_event
        async def failing(*args, **kwargs):
            await original(*args, **kwargs)
            if kwargs["event_type"].endswith(".deleted"):
                raise error("CDC failed after append")
        monkeypatch.setattr(module, "_append_crud_event", failing)
    with pytest.raises(ConfigError, match="caught storage"):
        async with provider.locking_transaction(models=(DeleteThing,)) as token:
            await rows.create(DeleteThing(id="a"), conn=token)
            with pytest.raises(error):
                await rows.delete_if("m", where={"guard": [1]}, conn=token)
            assert token.failed
            with pytest.raises(ConfigError, match="rollback"):
                await rows.get("m", conn=token)
    assert await rows.get("a") is None and await rows.get("m") is not None
    assert await deleted_events(provider) == []


async def test_peer_durable_write_waits_for_aborted_delete(provider):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    started = asyncio.Event()
    async def peer():
        started.set()
        await rows.update(DeleteThing(id="m", guard=9))
    task = None
    try:
        with pytest.raises(RuntimeError):
            async with provider.locking_transaction(models=(DeleteThing,)) as token:
                assert await rows.delete_if("m", where={"guard": [1]}, conn=token) is True
                task = asyncio.create_task(peer(), context=Context())
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(.03)
                assert not task.done()
                raise RuntimeError("abort")
        await asyncio.wait_for(task, 5)
        assert (await rows.get("m")).guard == 9
        assert await deleted_events(provider) == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_genuine_task_cancellation_after_cdc_poisoning(provider, monkeypatch):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    appended, caught, finish = (asyncio.Event() for _ in range(3))
    if isinstance(provider, _FakeStorageProvider):
        target, attr = provider.get_event_store(), "append"
    else:
        from primer.storage import sqlite, postgres
        target, attr = (sqlite if isinstance(provider, SqliteStorageProvider) else postgres), "_append_crud_event"
    original = getattr(target, attr)
    async def blocked(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs["event_type"].endswith(".deleted"):
            appended.set()
            await asyncio.Event().wait()
        return result
    monkeypatch.setattr(target, attr, blocked)
    async def owner():
        async with provider.locking_transaction(models=(DeleteThing,)) as token:
            await rows.create(DeleteThing(id="a"), conn=token)
            with pytest.raises(asyncio.CancelledError):
                await rows.delete_if("m", where={"guard": [1]}, conn=token)
            assert token.failed
            caught.set()
            await finish.wait()
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(appended.wait(), 2)
        task.cancel()
        await asyncio.wait_for(caught.wait(), 1)
        finish.set()
        with pytest.raises(ConfigError, match="caught storage"):
            await asyncio.wait_for(task, 5)
        assert await rows.get("m") is not None and await rows.get("a") is None
        assert await deleted_events(provider) == []
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("commit", [False, True])
async def test_fake_pending_events_never_hide_independent_events(commit, monkeypatch):
    p = _FakeStorageProvider()
    monkeypatch.setitem(registry._KIND_BY_MODEL, DeleteThing, "delete_test")
    rows, events = p.get_storage(DeleteThing), p.get_event_store()
    await rows.create(DeleteThing(id="m"))
    started = asyncio.Event()
    async def peer():
        started.set()
        return await events.append(event_type="independent", payload={"keep": True})
    other = None
    try:
        try:
            async with p.locking_transaction(models=(DeleteThing,)) as token:
                assert await rows.delete_if("m", where={"guard": [1]}, conn=token)
                assert await deleted_events(p) == []
                other = asyncio.create_task(peer(), context=Context())
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(.01)
                assert not other.done()
                if not commit:
                    raise RuntimeError("abort")
        except RuntimeError:
            assert not commit
        peer_id = await asyncio.wait_for(other, 2)
        all_events = await events.read_after(0)
        assert len(all_events) == 1 + int(commit)
        assert all_events[-1].id == peer_id and all_events[-1].payload == {"keep": True}
        assert len(await deleted_events(p)) == int(commit)
        assert await events.read_after(peer_id) == []
    finally:
        if other is not None and not other.done():
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)


async def test_declared_preparation_precedes_delete_core(provider, monkeypatch):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    async with provider.locking_transaction(models=(DeleteThing,)) as token:
        async def forbidden(*args, **kwargs):
            raise AssertionError("schema preparation entered under token")
        if not isinstance(provider, _FakeStorageProvider):
            monkeypatch.setattr(rows, "_ensure_table", forbidden)
            monkeypatch.setattr(provider, "_ensure_events_schema", forbidden)
        assert await rows.delete_if("m", where={"guard": [1]}, conn=token) is True
    assert len(await deleted_events(provider)) == 1


async def test_invalid_id_admission_is_unpoisoned(provider):
    rows = provider.get_storage(DeleteThing)
    async with provider.locking_transaction(models=(DeleteThing,)) as token:
        with pytest.raises(ConfigError, match="string"):
            await rows.delete_if(None, where={"guard": [1]}, conn=token)
        assert not token.failed
        await rows.create(DeleteThing(id="z"), conn=token)
    assert await rows.get("z") is not None


@pytest.mark.parametrize("operation", ["patch", "delete"])
async def test_guard_error_boundary_matches_patch_if(provider, operation):
    rows = provider.get_storage(DeleteThing)
    await rows.create(DeleteThing(id="m"))
    with pytest.raises(ConfigError, match="caught storage"):
        async with provider.locking_transaction(models=(DeleteThing,)) as token:
            await rows.create(DeleteThing(id="a"), conn=token)
            with pytest.raises(PatchSpecError):
                if operation == "patch":
                    await rows.patch_if("m", {"guard": 2}, where={}, conn=token)
                else:
                    await rows.delete_if("m", where={}, conn=token)
            assert token.failed
    assert await rows.get("a") is None and (await rows.get("m")).guard == 1
