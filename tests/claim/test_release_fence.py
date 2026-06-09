from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.int.claim import ReleaseOutcome


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
