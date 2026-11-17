"""Memory target merging and stale cleanup share the delivered row-lock unit."""

import asyncio
from contextvars import Context
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from primer.int.claim import ClaimKind, Lease
from primer.memory.jobs import JOB_LIFETIME, _forget, _upsert_job, run_memory_job
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import ProviderError
from primer.model.memory import AgentMemory, MemoryJob
from primer.storage.sqlite import SqliteStorageProvider
from tests.conftest import _FakeStorageProvider
from tests.storage.test_delete_if import provider  # noqa: F401

NOW = datetime(2026, 11, 17, tzinfo=UTC)
JOB = "reflect:a:s"


def job(**updates):
    return MemoryJob(id=JOB, kind="reflect", agent_id="a", session_id="s", target_turn=1,
                     created_at=NOW, expires_at=NOW + JOB_LIFETIME).model_copy(update=updates)


async def merge(provider, target, *, now=NOW):
    await _upsert_job(provider, JOB, kind="reflect", agent_id="a", session_id="s", target_turn=target, now=now)


@pytest.mark.parametrize("commit", [False, True])
async def test_cleanup_waits_for_actual_target_merge_then_rechecks_its_snapshot(provider, monkeypatch, commit):
    rows = provider.get_storage(MemoryJob)
    await rows.create(job())
    snapshot = (await rows.get(JOB)).model_copy(deep=True)
    original = rows.patch_if
    entered, release, cleanup_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = ProviderError("target writer body failed after its patch")

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        written = await original(*args, **kwargs)
        if not commit:
            raise failure
        return written

    async def cleanup():
        cleanup_started.set()
        await _forget(provider, snapshot)

    monkeypatch.setattr(rows, "patch_if", paused)
    writer = asyncio.create_task(merge(provider, 9))
    follower = None
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            follower = asyncio.create_task(cleanup(), context=Context())
            await cleanup_started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(follower), 0.05)
            release.set()
            if commit:
                await writer
            else:
                with pytest.raises(ProviderError) as caught:
                    await writer
                assert caught.value is failure
            await follower
    finally:
        release.set()
        for task in (writer, follower):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (writer, follower) if t is not None), return_exceptions=True)
    monkeypatch.setattr(rows, "patch_if", original)
    current = await rows.get(JOB)
    if commit:
        assert current is not None
        assert current.target_turn == 9 and current.created_at == snapshot.created_at
        await merge(provider, 2)
        assert (await rows.get(JOB)).target_turn == 9
        await _forget(provider, (await rows.get(JOB)).model_copy(deep=True))
        assert await rows.get(JOB) is None
    else:
        # The writer's full rollback restored the observed row, so cleanup may
        # remove it. A failed target merge is never misread as a committed one.
        assert current is None
    await merge(provider, 12)
    assert (await rows.get(JOB)).target_turn == 12


@pytest.mark.parametrize("encoding", ["offset", "epoch", "missing-defaults"])
async def test_actual_expiry_cleanup_preserves_a_legacy_row_renewed_by_target_merge(provider, encoding):
    rows = provider.get_storage(MemoryJob)
    expired = job(target_turn=0, expires_at=NOW - timedelta(seconds=1))
    await rows.create(expired)
    raw = expired.model_dump(mode="json", exclude={"id"})
    if encoding == "offset":
        raw["created_at"] = "2026-11-17T04:00:00+04:00"
        raw["expires_at"] = "2026-11-17T03:59:59+04:00"
    elif encoding == "epoch":
        raw["created_at"], raw["expires_at"] = expired.created_at.timestamp(), expired.expires_at.timestamp()
    else:
        del raw["target_turn"]
        del raw["reflected_turn"]
    if isinstance(provider, _FakeStorageProvider):
        rows.seed_raw(JOB, raw)
    else:
        async with provider.transaction() as conn:
            if isinstance(provider, SqliteStorageProvider):
                await conn.execute("UPDATE memoryjob SET data = ? WHERE id = ?", (json.dumps(raw), JOB))
            else:
                await conn.execute(f'UPDATE "{provider.schema}".memoryjob SET data = $1::jsonb WHERE id = $2', json.dumps(raw), JOB)
    await provider.get_storage(Agent).create(Agent(id="a", description="test", model=AgentModel(profile_id="p")))
    await provider.get_storage(AgentMemory).create(AgentMemory(id="a", enabled=True))
    emitted = []

    async def emit(event, **kwargs):
        emitted.append((event, kwargs))
        assert event == "memory.reflection.skipped" and kwargs["payload"]["reason"] == "expired"
        # Actual completed-turn target writer, after the worker took its old
        # snapshot but before cleanup locks/rechecks it. A later lower target
        # cannot regress the target or make that old snapshot deletable.
        await merge(provider, 9, now=NOW)
        await merge(provider, 2, now=NOW + timedelta(seconds=1))

    deps = SimpleNamespace(sp=provider, now=lambda: NOW, recorder=SimpleNamespace(emit=emit))
    lease = Lease(ClaimKind.MEMORY, JOB, "worker", NOW, NOW + timedelta(minutes=1), 0, None)
    outcome = await run_memory_job(deps, lease)
    current = await rows.get(JOB)
    assert len(emitted) == 1 and outcome.drop_lease is True
    assert current is not None
    assert current.target_turn == 9 and current.created_at == expired.created_at
    assert current.expires_at == NOW + timedelta(seconds=1) + JOB_LIFETIME
    assert current.agent_id == expired.agent_id and current.session_id == expired.session_id
    # Row retention intentionally does not certify that the dropped lease was
    # re-armed. Fresh cleanup still works after the two implementations compose.
    await _forget(provider, current.model_copy(deep=True))
    assert await rows.get(JOB) is None
