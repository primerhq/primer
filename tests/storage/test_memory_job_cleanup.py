"""Memory cleanup compares a locked decoded snapshot before deleting it.

The production topics completion path is exercised without a model call. Native
Postgres cases participate in the existing storage CI lane; local cases use
private SQLite and the transaction-aware fake.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
from datetime import UTC, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from primer.int.claim import ClaimKind, Lease
from primer.memory.jobs import run_memory_job, run_topics
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import NotFoundError
from primer.model.memory import AgentMemory, MemoryJob
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.storage.test_delete_if import provider  # noqa: F401

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def _job(**changes):
    return MemoryJob(id="topics:a", kind="topics", agent_id="a", created_at=NOW,
                     expires_at=NOW + timedelta(days=7)).model_copy(update=changes)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    async def derived(*args):
        return None
    monkeypatch.setattr("primer.memory.topics.derive_topics", derived)


async def _complete(provider, snapshot):
    await run_topics(SimpleNamespace(sp=provider), snapshot, None, None)


async def _raw(provider, job, data):
    rows = provider.get_storage(MemoryJob)
    if isinstance(provider, _FakeStorageProvider):
        rows.seed_raw(job.id, data)
    else:
        async with provider.transaction() as conn:
            if isinstance(provider, SqliteStorageProvider):
                await conn.execute('UPDATE memoryjob SET data = ? WHERE id = ?', (json.dumps(data), job.id))
            else:
                await conn.execute(f'UPDATE "{provider.schema}".memoryjob SET data = $1::jsonb WHERE id = $2',
                                   json.dumps(data), job.id)


async def test_unchanged_completion_and_missing_retry(provider):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    await _complete(provider, old)
    assert await rows.get(old.id) is None
    await _complete(provider, old)
    assert await rows.get(old.id) is None


@pytest.mark.parametrize("changes", [
    {"target_turn": 9}, {"reflected_turn": 2}, {"expires_at": NOW + timedelta(days=8)},
    {"created_at": NOW + timedelta(seconds=1)}, {"agent_id": "replacement"},
    {"kind": "reflect"}, {"session_id": "new-session"},
])
async def test_stale_completion_preserves_changed_row_and_fresh_completion_can_remove_it(provider, changes):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    current = old.model_copy(update=changes)
    await rows.update(current)
    await _complete(provider, old)
    assert (await rows.get(old.id)).model_dump() == current.model_dump()
    await _complete(provider, (await rows.get(old.id)).model_copy(deep=True))
    assert await rows.get(old.id) is None


@pytest.mark.parametrize("encoding", ["plus-zero", "offset", "epoch", "missing-defaults"])
async def test_readable_legacy_spelling_does_not_strand_unchanged_cleanup(provider, encoding):
    rows = provider.get_storage(MemoryJob)
    job = _job()
    await rows.create(job)
    data = job.model_dump(mode="json", exclude={"id"})
    for name in ("created_at", "expires_at"):
        value = getattr(job, name)
        data[name] = (value.isoformat() if encoding == "plus-zero" else
                      value.astimezone(timezone(timedelta(hours=4))).isoformat() if encoding == "offset" else
                      value.timestamp() if encoding == "epoch" else data[name])
    if encoding == "missing-defaults":
        for name in ("session_id", "target_turn", "reflected_turn"):
            del data[name]
    await _raw(provider, job, data)
    await _complete(provider, (await rows.get(job.id)).model_copy(deep=True))
    assert await rows.get(job.id) is None


async def test_equivalent_timestamp_reencoding_after_snapshot_still_cleans_up(provider):
    rows = provider.get_storage(MemoryJob)
    job = _job()
    await rows.create(job)
    old = (await rows.get(job.id)).model_copy(deep=True)
    data = job.model_dump(mode="json", exclude={"id"})
    data["expires_at"] = job.expires_at.astimezone(timezone(timedelta(hours=4))).isoformat()
    await _raw(provider, job, data)
    await _complete(provider, old)
    assert await rows.get(job.id) is None


@pytest.mark.parametrize("failure", ["false", "exception", "unsupported"])
async def test_delete_refusal_preserves_row_without_fallback_and_later_cleanup_succeeds(provider, monkeypatch, caplog, failure):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    real_delete_if = rows.delete_if
    attempts = []

    async def refuse(*args, **kwargs):
        attempts.append(kwargs.get("conn"))
        if failure == "exception":
            raise ConnectionError("delete unavailable")
        if failure == "unsupported":
            raise NotImplementedError("guarded deletion unsupported")
        return False

    async def forbidden(*args, **kwargs):
        raise AssertionError("unconditional deletion fallback")

    monkeypatch.setattr(rows, "delete_if", refuse)
    monkeypatch.setattr(rows, "delete", forbidden)
    await _complete(provider, old)
    assert len(attempts) == 1 and attempts[0] is not None
    assert (await rows.get(old.id)).model_dump() == old.model_dump()
    assert old.id in caplog.text
    monkeypatch.setattr(rows, "delete_if", real_delete_if)
    await _complete(provider, old)
    assert await rows.get(old.id) is None


@pytest.mark.parametrize("point", ["get_for_update", "delete_if"])
async def test_real_cancellation_propagates_and_does_not_delete(provider, monkeypatch, point):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    entered = asyncio.Event()
    real = getattr(rows, point)

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rows, point, blocked)
    task = asyncio.create_task(_complete(provider, old))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(rows, point, real)
    assert (await rows.get(old.id)).model_dump() == old.model_dump()
    await _complete(provider, old)
    assert await rows.get(old.id) is None


async def test_row_lock_spans_comparison_and_delete_against_independent_writer(provider, monkeypatch):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    newer = old.model_copy(update={"expires_at": NOW + timedelta(days=8)})
    entered, release, writer_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_delete = rows.delete_if

    async def hold(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_delete(*args, **kwargs)

    async def writer():
        writer_started.set()
        try:
            await rows.update(newer)
        except NotFoundError:
            await rows.create(newer)

    monkeypatch.setattr(rows, "delete_if", hold)
    cleanup = asyncio.create_task(_complete(provider, old))
    peer = None
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            peer = asyncio.create_task(writer(), context=Context())
            await writer_started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(peer), timeout=0.05)
            release.set()
            await cleanup
            await peer
    finally:
        release.set()
        for task in (cleanup, peer):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (cleanup, peer) if t is not None), return_exceptions=True)
    assert (await rows.get(old.id)).model_dump() == newer.model_dump()


@pytest.mark.parametrize("gate", ["expired", "agent-missing"])
async def test_gate_cleanup_does_not_erase_a_job_renewed_after_its_read(provider, monkeypatch, gate):
    rows = provider.get_storage(MemoryJob)
    old = _job(kind="reflect", target_turn=2, expires_at=NOW - timedelta(seconds=1))
    await rows.create(old)
    newer = old.model_copy(update={"target_turn": 9, "expires_at": NOW + timedelta(days=7)})
    agents = provider.get_storage(Agent)
    await provider.get_storage(AgentMemory).create(AgentMemory(id="a", enabled=True))
    renewed = False

    async def renew():
        nonlocal renewed
        await rows.update(newer)
        renewed = True

    async def emit(*args, **kwargs):
        assert gate == "expired"
        await renew()

    if gate == "expired":
        await agents.create(Agent(id="a", description="test", model=AgentModel(profile_id="p")))
    else:
        async def missing(*args, **kwargs):
            await renew()
            return None
        monkeypatch.setattr(agents, "get", missing)
    deps = SimpleNamespace(sp=provider, now=lambda: NOW, recorder=SimpleNamespace(emit=emit))
    lease = Lease(ClaimKind.MEMORY, old.id, "worker", NOW, NOW + timedelta(minutes=1), 0, None)
    outcome = await run_memory_job(deps, lease)
    assert renewed and outcome.drop_lease is True
    assert (await rows.get(old.id)).model_dump() == newer.model_dump()


async def test_no_retry_after_ambiguous_commit_can_delete_a_replacement(provider, monkeypatch, caplog):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    replacement = old.model_copy(update={"created_at": NOW + timedelta(seconds=1)})
    original = provider.locking_transaction
    attempts = 0

    @asynccontextmanager
    async def ambiguous(**kwargs):
        nonlocal attempts
        attempts += 1
        async with original(**kwargs) as conn:
            yield conn
        # A commit acknowledgement can fail after durable deletion. Another
        # writer may recreate the ID before the caller observes the error.
        await rows.create(replacement)
        raise ConnectionError("commit acknowledgement lost")

    monkeypatch.setattr(provider, "locking_transaction", ambiguous)
    await _complete(provider, old)
    assert attempts == 1
    assert (await rows.get(old.id)).model_dump() == replacement.model_dump()
    assert "commit acknowledgement lost" in caplog.text


async def test_identical_recreation_is_not_an_occurrence_fence(provider):
    """The deliberately narrow snapshot contract cannot distinguish an ABA."""
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    await rows.delete(old.id)
    await rows.create(old.model_copy(deep=True))
    await _complete(provider, old)
    assert await rows.get(old.id) is None


async def test_unsupported_locking_retains_the_row_without_legacy_fallback(provider, monkeypatch, caplog):
    rows = provider.get_storage(MemoryJob)
    await rows.create(_job())
    old = (await rows.get("topics:a")).model_copy(deep=True)
    original = provider.locking_transaction

    @asynccontextmanager
    async def unsupported(**kwargs):
        raise NotImplementedError("row locking unsupported")
        yield  # pragma: no cover - model the optional async context-manager capability

    async def forbidden(*args, **kwargs):
        raise AssertionError("legacy delete fallback")

    monkeypatch.setattr(provider, "locking_transaction", unsupported)
    monkeypatch.setattr(rows, "delete", forbidden)
    await _complete(provider, old)
    assert (await rows.get(old.id)).model_dump() == old.model_dump()
    assert "row locking unsupported" in caplog.text
    monkeypatch.setattr(provider, "locking_transaction", original)
    await _complete(provider, old)
    assert await rows.get(old.id) is None
