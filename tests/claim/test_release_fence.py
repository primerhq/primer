from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, PostReleaseWake, ReleaseOutcome


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _RecordingStorage:
    def __init__(self, row):
        self._row = row
        self.get_conns = []
        self.update_conns = []

    async def get(self, id, *, conn=None):
        self.get_conns.append(conn)
        return self._row

    async def update(self, entity, *, conn=None):
        self.update_conns.append(conn)
        self._row = entity
        return entity




@pytest.mark.asyncio
async def test_sessions_adapter_forwards_conn():
    from primer.claim.adapters.sessions import SessionClaimAdapter
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    row = WorkspaceSession(
        id="s1",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
    )
    storage = _RecordingStorage(row)
    adapter = SessionClaimAdapter(session_storage=storage)
    sentinel = object()
    # Success / non-park release: avoids the WorkspaceIO failure branch.
    await adapter.on_release(
        sentinel, "s1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    assert storage.update_conns
    assert set(storage.update_conns) == {sentinel}


@pytest.mark.asyncio
async def test_harnesses_adapter_forwards_conn():
    from primer.claim.adapters.harnesses import HarnessClaimAdapter
    from primer.model.harness import Harness, HarnessOperation

    row = Harness(
        id="h1",
        slug="my-harness",
        name="My Harness",
        git_url="https://example.com/repo.git",
        created_at=_now(),
        pending_operation=HarnessOperation.FETCH,
    )
    storage = _RecordingStorage(row)
    adapter = HarnessClaimAdapter(harness_storage=storage)
    sentinel = object()
    await adapter.on_release(
        sentinel, "h1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    assert storage.update_conns == [sentinel]


@pytest.mark.asyncio
async def test_in_memory_release_is_fenced_on_ownership():
    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    await engine.upsert(ClaimKind.HARNESS, "c1")
    leases = await engine.claim_due("worker-A", max_count=1)
    lease_a = leases[0]
    assert lease_a.claimed_by == "worker-A"
    # Simulate re-claim by worker B.
    engine._leases[(ClaimKind.HARNESS, "c1")].claimed_by = "worker-B"
    await engine.release(lease_a, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert calls == []  # on_release NOT called (stale worker fenced out)
    assert engine._leases[(ClaimKind.HARNESS, "c1")].claimed_by == "worker-B"  # lease untouched


@pytest.mark.asyncio
async def test_in_memory_release_runs_when_still_owned():
    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.HARNESS
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Adapter()})
    await engine.upsert(ClaimKind.HARNESS, "c2")
    leases = await engine.claim_due("worker-A", max_count=1)
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert calls == ["c2"]  # owned -> on_release ran


@pytest.mark.asyncio
async def test_in_memory_release_fires_post_release_hook_after_mutation():
    """01a0518b review, required test: the bound hook must fire strictly
    AFTER release()'s own lease mutation is applied - "post-commit"
    degrades to "post-mutation" for the in-memory engine (no real
    transaction), per PostReleaseWake's own docstring. Proven by having
    the hook itself inspect engine state: if it ever ran BEFORE the
    drop, the lease would still be present when it fires."""
    from primer.claim.in_memory import InMemoryClaimEngine

    class _Adapter:
        kind = ClaimKind.TOOL_CALL
        entity_table = "toolcalltask"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return PostReleaseWake(
                session_id="s1", event_key="tool_wait:s1:0",
                payload={"tool_wait_ready": True},
            )

    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: _Adapter()})
    await engine.upsert(ClaimKind.TOOL_CALL, "t1")
    leases = await engine.claim_due("worker-A", max_count=1)

    hook_calls: list[PostReleaseWake] = []
    lease_already_dropped: list[bool] = []

    async def _hook(signal: PostReleaseWake) -> None:
        hook_calls.append(signal)
        lease_already_dropped.append(
            (ClaimKind.TOOL_CALL, "t1") not in engine._leases
        )

    engine.bind_post_release_hook(_hook)
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert hook_calls == [PostReleaseWake(
        session_id="s1", event_key="tool_wait:s1:0",
        payload={"tool_wait_ready": True},
    )]
    assert lease_already_dropped == [True]


@pytest.mark.asyncio
async def test_in_memory_release_skips_hook_when_signal_is_none():
    """Every OTHER adapter (and this one, absent a batch) returns None -
    the hook must never fire for those."""
    from primer.claim.in_memory import InMemoryClaimEngine

    class _Adapter:
        kind = ClaimKind.TOOL_CALL
        entity_table = "toolcalltask"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            return None

    engine = InMemoryClaimEngine(adapters={ClaimKind.TOOL_CALL: _Adapter()})
    await engine.upsert(ClaimKind.TOOL_CALL, "t1")
    leases = await engine.claim_due("worker-A", max_count=1)

    hook_calls = []
    engine.bind_post_release_hook(lambda signal: hook_calls.append(signal))
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))

    assert hook_calls == []


@pytest.mark.asyncio
async def test_triggers_adapter_forwards_conn():
    from primer.claim.adapters.triggers import TriggerClaimAdapter
    from primer.model.trigger import DelayedTriggerConfig, Trigger

    row = Trigger(
        id="t1",
        slug="my-trigger",
        name="My Trigger",
        config=DelayedTriggerConfig(fire_at=_now()),
        created_at=_now(),
        next_fire_at=_now(),
    )
    storage = _RecordingStorage(row)
    adapter = TriggerClaimAdapter(storage=storage)
    sentinel = object()
    await adapter.on_release(
        sentinel, "t1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    assert storage.update_conns == [sentinel]
