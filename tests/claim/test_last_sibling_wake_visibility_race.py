"""Post-commit readiness tests for concurrent final tool-call siblings.

The storage double below gives each connection its own uncommitted view,
matching Postgres READ COMMITTED closely enough to reproduce the final-two
release ordering without a database. Every terminal release now returns a
candidate signal. The factory hook evaluates its batch fence after commit,
so a non-final release cannot wake early and the later commit cannot miss
the sibling that committed first.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.factory import _post_release_wake_ready
from primer.int.claim import ReleaseOutcome
from primer.model.except_ import NotFoundError
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from tests.storage._patch_reference import patch_model


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task(task_id: str, batch: list[str]) -> ToolCallTask:
    return ToolCallTask(
        id=task_id, session_id="s1", turn_no=0, tool_name="t",
        state=ToolCallTaskState.RUNNING, record_seq=1, created_at=_now(),
        batch_task_ids=batch, claim_token=_token(task_id),
    )


def _token(task_id: str) -> str:
    return f"tok-{task_id}"


def _release(task_id: str) -> ReleaseOutcome:
    return ReleaseOutcome(success=True, drop_lease=True, claim_token=_token(task_id))


class _PerConnVisibilityStorage:
    """Simulates Postgres's own READ COMMITTED isolation: a write made
    under one ``conn`` is visible to reads under that SAME conn
    immediately, but invisible to every OTHER conn (and to conn=None,
    the "committed" view) until ``commit(conn)`` is called."""

    def __init__(self, initial: dict[str, ToolCallTask]) -> None:
        self._committed = dict(initial)
        self._pending: dict[object, dict[str, ToolCallTask]] = {}

    async def get(self, task_id: str, *, conn=None):
        if conn is not None and task_id in self._pending.get(conn, {}):
            return self._pending[conn][task_id]
        return self._committed.get(task_id)

    async def update(self, entity: ToolCallTask, *, conn=None):
        if conn is not None:
            self._pending.setdefault(conn, {})[entity.id] = entity
        else:
            self._committed[entity.id] = entity
        return entity

    async def patch_if(self, task_id: str, patch=None, *, where, set_paths=None, conn=None):
        """READ COMMITTED: the write sees this conn's own pending row, else the committed one."""
        current = await self.get(task_id, conn=conn)
        if current is None:
            raise NotFoundError(f"no entity with id {task_id!r}")
        updated = patch_model(current, patch, where=where, set_paths=set_paths)
        if updated is not None:
            await self.update(updated, conn=conn)
        return updated

    def commit(self, conn) -> None:
        for task_id, entity in self._pending.pop(conn, {}).items():
            self._committed[task_id] = entity


@pytest.mark.asyncio
async def test_concurrent_last_two_commits_recover_the_wake_post_commit() -> None:
    """The later commit sees the complete batch even when neither transaction did."""
    a, b = "s1/x:tool:0:1", "s1/x:tool:0:2"
    batch = [a, b]
    storage = _PerConnVisibilityStorage({
        a: _task(a, batch), b: _task(b, batch),
    })
    adapter = ToolCallClaimAdapter(task_storage=storage)
    conn_a, conn_b = object(), object()

    signal_a = await adapter.on_release(conn_a, a, outcome=_release(a))
    signal_b = await adapter.on_release(conn_b, b, outcome=_release(b))

    assert signal_a is not None
    assert signal_b is not None
    assert signal_a.required_terminal_task_ids == tuple(batch)
    assert signal_b.required_terminal_task_ids == tuple(batch)
    assert not await _post_release_wake_ready(signal_a, storage)

    # The first commit is still not enough, so there is no premature wake.
    storage.commit(conn_a)
    assert not await _post_release_wake_ready(signal_a, storage)

    # The second commit makes either candidate ready. In production each
    # engine invokes its hook immediately after its own commit; therefore
    # the later commit always supplies at least one ready candidate.
    storage.commit(conn_b)
    assert await _post_release_wake_ready(signal_a, storage)
    assert await _post_release_wake_ready(signal_b, storage)
    assert storage._committed[a].state == ToolCallTaskState.DONE
    assert storage._committed[b].state == ToolCallTaskState.DONE


@pytest.mark.asyncio
async def test_sequential_non_final_then_final_release_wakes_only_when_ready() -> None:
    a, b = "s1/x:tool:0:1", "s1/x:tool:0:2"
    batch = [a, b]
    storage = _PerConnVisibilityStorage({
        a: _task(a, batch), b: _task(b, batch),
    })
    adapter = ToolCallClaimAdapter(task_storage=storage)
    conn_a, conn_b = object(), object()

    signal_a = await adapter.on_release(conn_a, a, outcome=_release(a))
    assert signal_a is not None
    storage.commit(conn_a)
    assert not await _post_release_wake_ready(signal_a, storage)

    signal_b = await adapter.on_release(conn_b, b, outcome=_release(b))
    assert signal_b is not None
    assert not await _post_release_wake_ready(signal_b, storage)
    storage.commit(conn_b)

    assert await _post_release_wake_ready(signal_b, storage)


@pytest.mark.asyncio
async def test_single_task_batch_is_ready_only_after_its_commit() -> None:
    task_id = "s1/x:tool:0:1"
    storage = _PerConnVisibilityStorage({task_id: _task(task_id, [task_id])})
    adapter = ToolCallClaimAdapter(task_storage=storage)
    conn = object()

    signal = await adapter.on_release(conn, task_id, outcome=_release(task_id))

    assert signal is not None
    assert not await _post_release_wake_ready(signal, storage)
    storage.commit(conn)
    assert await _post_release_wake_ready(signal, storage)


@pytest.mark.asyncio
async def test_retry_release_never_creates_a_wake_candidate() -> None:
    task_id = "s1/x:tool:0:1"
    storage = _PerConnVisibilityStorage({task_id: _task(task_id, [task_id])})
    adapter = ToolCallClaimAdapter(task_storage=storage)

    signal = await adapter.on_release(
        object(),
        task_id,
        outcome=ReleaseOutcome(success=False, claim_token=_token(task_id)),
    )

    assert signal is None


@pytest.mark.asyncio
async def test_in_memory_engine_post_commit_check_wakes_exactly_once() -> None:
    from primer.claim.in_memory import InMemoryClaimEngine
    from primer.int.claim import ClaimKind

    batch = ["x:tool:0:1", "x:tool:0:2"]

    class _Storage:
        def __init__(self) -> None:
            self._data = {tid: _task(tid, batch) for tid in batch}

        async def get(self, task_id: str, *, conn=None):
            return self._data.get(task_id)

        async def update(self, entity: ToolCallTask, *, conn=None):
            self._data[entity.id] = entity
            return entity

        async def patch_if(self, task_id: str, patch=None, *, where, set_paths=None, conn=None):
            current = self._data.get(task_id)
            if current is None:
                raise NotFoundError(f"no entity with id {task_id!r}")
            updated = patch_model(current, patch, where=where, set_paths=set_paths)
            if updated is not None:
                self._data[task_id] = updated
            return updated

    storage = _Storage()
    adapter = ToolCallClaimAdapter(task_storage=storage)
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: adapter})
    for tid in batch:
        await engine.upsert(ClaimKind.TOOL_CALL, tid)
    leases = await engine.claim_due("worker-A", max_count=2)
    lease_by_id = {lease.entity_id: lease for lease in leases}

    signals: list = []

    async def _hook(signal) -> None:
        if await _post_release_wake_ready(signal, storage):
            signals.append(signal)

    engine.bind_post_release_hook(_hook)

    await asyncio.gather(*[
        engine.release(lease_by_id[tid], outcome=_release(tid))
        for tid in batch
    ])

    assert len(signals) == 1
