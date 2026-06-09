from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind, ReleaseOutcome


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
async def test_chat_adapter_forwards_conn():
    from primer.claim.adapters.chats import ChatClaimAdapter
    from primer.model.chats import Chat

    row = Chat(id="c1", agent_id="a1", created_at=_now(), turn_status="running")
    storage = _RecordingStorage(row)
    adapter = ChatClaimAdapter(chat_storage=storage)
    sentinel = object()
    await adapter.on_release(
        sentinel, "c1", outcome=ReleaseOutcome(success=True, drop_lease=True)
    )
    assert storage.get_conns == [sentinel]
    assert storage.update_conns == [sentinel]


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
        kind = ClaimKind.CHAT
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.CHAT: _Adapter()})
    await engine.upsert(ClaimKind.CHAT, "c1")
    leases = await engine.claim_due("worker-A", max_count=1)
    lease_a = leases[0]
    assert lease_a.claimed_by == "worker-A"
    # Simulate re-claim by worker B.
    engine._leases[(ClaimKind.CHAT, "c1")].claimed_by = "worker-B"
    await engine.release(lease_a, outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert calls == []  # on_release NOT called (stale worker fenced out)
    assert engine._leases[(ClaimKind.CHAT, "c1")].claimed_by == "worker-B"  # lease untouched


@pytest.mark.asyncio
async def test_in_memory_release_runs_when_still_owned():
    from primer.claim.in_memory import InMemoryClaimEngine

    calls = []

    class _Adapter:
        kind = ClaimKind.CHAT
        entity_table = "chat"

        def eligibility_sql(self):
            return "true"

        async def on_release(self, conn, entity_id, *, outcome):
            calls.append(entity_id)

    engine = InMemoryClaimEngine(adapters={ClaimKind.CHAT: _Adapter()})
    await engine.upsert(ClaimKind.CHAT, "c2")
    leases = await engine.claim_due("worker-A", max_count=1)
    await engine.release(leases[0], outcome=ReleaseOutcome(success=True, drop_lease=True))
    assert calls == ["c2"]  # owned -> on_release ran


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
