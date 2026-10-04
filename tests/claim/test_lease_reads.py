"""ClaimEngine.lease_exists and prune_dead_leases (Phase 3 stage 7a, slice S1-B).

``lease_exists`` is the batch form of ``has_lease``: which of these entities still have a lease ROW, so
the arm step can give the others one. ``prune_dead_leases`` deletes the unheld lease rows of a kind
whose entity is missing or finished, which the Postgres claim query never claims (it joins the entity
table) and nothing else deletes. The in-memory engine is tested here; the Postgres statement is held to
the same scenarios in test_postgres_engine.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.claim.adapters.harnesses import HarnessClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimAdapter, ClaimEngine, ClaimKind, ReleaseOutcome
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from tests.conftest import _InMemoryStorage


def _task(task_id: str, state: ToolCallTaskState) -> ToolCallTask:
    return ToolCallTask(
        id=task_id, session_id="s1", turn_no=0, tool_name="t", state=state, record_seq=1,
        created_at=datetime.now(UTC),
    )


async def _engine_with(*tasks: ToolCallTask) -> InMemoryClaimEngine:
    storage = _InMemoryStorage(ToolCallTask)
    for task in tasks:
        await storage.create(task)
    return InMemoryClaimEngine(adapters={
        ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=storage),
        ClaimKind.HARNESS: HarnessClaimAdapter(harness_storage=None),
    })


# ---- lease_exists -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_exists_answers_the_subset_that_has_a_row():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.TOOL_CALL, "a")
    await engine.upsert(ClaimKind.TOOL_CALL, "b")

    assert await engine.lease_exists(ClaimKind.TOOL_CALL, ["a", "b", "c"]) == {"a", "b"}
    assert await engine.lease_exists(ClaimKind.TOOL_CALL, ["c"]) == set()
    assert await engine.lease_exists(ClaimKind.TOOL_CALL, []) == set()
    assert await engine.lease_exists(ClaimKind.TOOL_CALL, ["a", "a"]) == {"a"}


@pytest.mark.asyncio
async def test_lease_exists_counts_claimed_and_expired_rows_and_is_per_kind():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.TOOL_CALL, "claimed")
    await engine.upsert(ClaimKind.TOOL_CALL, "expired")
    await engine.upsert(ClaimKind.HARNESS, "other-kind")
    await engine.claim_due("w", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    engine._leases[(ClaimKind.TOOL_CALL, "expired")].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await engine.lease_exists(ClaimKind.TOOL_CALL, ["claimed", "expired", "other-kind"]) == {
        "claimed", "expired",
    }


@pytest.mark.asyncio
async def test_lease_exists_agrees_with_has_lease_row_by_row():
    engine = InMemoryClaimEngine(adapters={})
    await engine.upsert(ClaimKind.TOOL_CALL, "a")
    ids = ["a", "b"]
    assert await engine.lease_exists(ClaimKind.TOOL_CALL, ids) == {
        i for i in ids if await engine.has_lease(ClaimKind.TOOL_CALL, i)
    }


# ---- the ABC defaults are conservative ---------------------------------------------------------------------


class _Bare(ClaimEngine):
    async def claim_due(self, worker_id, *, max_count, kinds=None): return []
    async def heartbeat(self, worker_id, kind_ids): return []
    async def release(self, lease, *, outcome): ...
    async def mark_resumable(self, kind, entity_id, *, priority=50): ...
    async def watch_ready(self): yield  # pragma: no cover
    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None): ...
    async def delete_lease(self, kind, entity_id): ...


@pytest.mark.asyncio
async def test_an_engine_that_cannot_answer_claims_every_lease_exists_and_prunes_nothing():
    """The caller arms the ids NOT in the answer and prunes what the engine reports, so 'cannot tell'
    must make it do nothing."""
    engine = _Bare()
    assert await engine.lease_exists(ClaimKind.TOOL_CALL, ["a", "b"]) == {"a", "b"}
    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 0


# ---- prune_dead_leases ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_deletes_leases_of_finished_and_missing_tasks_and_keeps_the_rest():
    engine = await _engine_with(
        _task("queued", ToolCallTaskState.QUEUED), _task("running", ToolCallTaskState.RUNNING),
        _task("gated", ToolCallTaskState.GATED), _task("done", ToolCallTaskState.DONE),
        _task("failed", ToolCallTaskState.FAILED),
    )
    for tid in ("queued", "running", "gated", "done", "failed", "ghost"):
        await engine.upsert(ClaimKind.TOOL_CALL, tid)

    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 3
    survivors = {tid for tid in ("queued", "running", "gated", "done", "failed", "ghost")
                 if await engine.has_lease(ClaimKind.TOOL_CALL, tid)}
    assert survivors == {"queued", "running", "gated"}
    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 0, "idempotent"


@pytest.mark.asyncio
async def test_prune_never_deletes_a_lease_a_worker_holds():
    """A held lease of a finished task is its holder's to release (the release is fenced on claimed_by);
    deleting it from under them would turn that release into a silent no-op."""
    engine = await _engine_with(_task("held", ToolCallTaskState.DONE), _task("lapsed", ToolCallTaskState.DONE))
    await engine.upsert(ClaimKind.TOOL_CALL, "held")
    await engine.upsert(ClaimKind.TOOL_CALL, "lapsed")
    await engine.claim_due("w", max_count=10, kinds=[ClaimKind.TOOL_CALL])
    engine._leases[(ClaimKind.TOOL_CALL, "lapsed")].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 1
    assert await engine.has_lease(ClaimKind.TOOL_CALL, "held") is True
    assert await engine.has_lease(ClaimKind.TOOL_CALL, "lapsed") is False


@pytest.mark.asyncio
async def test_prune_touches_only_the_kind_it_was_asked_about():
    engine = await _engine_with(_task("done", ToolCallTaskState.DONE))
    await engine.upsert(ClaimKind.TOOL_CALL, "done")
    await engine.upsert(ClaimKind.HARNESS, "done")        # same id, other kind

    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 1
    assert await engine.has_lease(ClaimKind.HARNESS, "done") is True


@pytest.mark.asyncio
async def test_a_kind_whose_adapter_defines_no_dead_rule_is_never_pruned():
    """The missing-entity rule would delete every lease of a kind the engine cannot judge; the opt-in
    keeps that out of kinds that never asked for it."""
    class _NoRule(ClaimAdapter):
        kind = ClaimKind.HARNESS
        entity_table = "chats"
        def eligibility_sql(self): return "true"
        async def on_release(self, conn, entity_id, *, outcome): ...

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _NoRule()})
    await engine.upsert(ClaimKind.HARNESS, "h-1")
    assert await engine.prune_dead_leases(ClaimKind.HARNESS) == 0
    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is True
    assert await engine.prune_dead_leases(ClaimKind.TRIGGER) == 0, "no adapter registered at all"


@pytest.mark.asyncio
async def test_a_finished_release_drops_its_own_lease_so_there_is_nothing_to_prune():
    running = _task("t1", ToolCallTaskState.RUNNING).model_copy(update={"claim_token": "x"})
    engine = await _engine_with(running)
    await engine.upsert(ClaimKind.TOOL_CALL, "t1")
    (lease,) = await engine.claim_due("w", max_count=1, kinds=[ClaimKind.TOOL_CALL])
    await engine.release(lease, outcome=ReleaseOutcome(success=True, drop_lease=True, claim_token="x"))

    adapter = engine._adapters[ClaimKind.TOOL_CALL]
    assert await adapter.is_dead("t1") is True, "the release finished the task"
    assert await engine.has_lease(ClaimKind.TOOL_CALL, "t1") is False
    assert await engine.prune_dead_leases(ClaimKind.TOOL_CALL) == 0


@pytest.mark.asyncio
async def test_a_lease_claimed_while_prune_awaits_the_entity_is_not_deleted():
    """`is_dead` awaits, and `claim_due` mutates a lease row in place meanwhile: the held check must be repeated after
    the await, with nothing awaited between it and the delete (mutation: decide held-ness once, before the await)."""
    import asyncio

    class _Storage(_InMemoryStorage):
        async def get(self, id, *, conn=None):
            await asyncio.sleep(0)            # suspend: another task runs here
            return await super().get(id, conn=conn)

    storage = _Storage(ToolCallTask)
    await storage.create(_task("t1", ToolCallTaskState.DONE))
    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=storage)})
    await engine.upsert(ClaimKind.TOOL_CALL, "t1")

    pruning = asyncio.create_task(engine.prune_dead_leases(ClaimKind.TOOL_CALL))
    await asyncio.sleep(0)                    # prune has decided the lease is unheld and is awaiting the entity
    (claimed,) = await engine.claim_due("w", max_count=1, kinds=[ClaimKind.TOOL_CALL])
    assert await pruning == 0

    assert claimed.entity_id == "t1"
    assert await engine.has_live_lease(ClaimKind.TOOL_CALL, "t1") is True, "a held lease was deleted"

