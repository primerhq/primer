"""Workspace allocation has an external boundary and one durable publication decision."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from primer.access.person import Person
from primer.bus.scheduler_tasks import SessionMessagingSweeper
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.except_ import AccessDeniedError, ConflictError, NotFoundError
from primer.model.session_creation import SessionCreationIntent
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.workspace.session_creation import PREPARATION_GRACE, abandon_preparation, recover_preparations
from primer.workspace.session_factory import start_workspace_session
from tests.pg_gate import postgres_marks
from tests.storage.test_storage_contract import provider  # noqa: F401 (SQLite + native PG)
from tests.workspace.test_session_factory import (
    _FakeClaimEngine, _FakeLiveWorkspace, _FakeScheduler, _full_deps, _seed_agent, _seed_workspace,
)


class HeldSlot(_FakeLiveWorkspace):
    def __init__(self):
        super().__init__()
        self.allocated = asyncio.Event()
        self.release = asyncio.Event()

    async def start_session(self, binding, **kwargs):
        await super().start_session(binding, **kwargs)
        self.allocated.set()
        await self.release.wait()


async def _world(sp):
    await _seed_workspace(sp)
    await _seed_agent(sp)
    slot = HeldSlot()
    return slot, _full_deps(sp, _FakeScheduler(), _FakeClaimEngine(), slot)


async def _start(deps, **kwargs):
    return await start_workspace_session(
        deps=deps, workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"),
        initial_instructions="first instruction", auto_start=True, graph_input=None,
        metadata=None, parent_session_id="lineage-only", **kwargs,
    )


@pytest.mark.parametrize("change", ["end", "finish_turn", "restart", "target", "binding", "access", "expire"])
async def test_change_during_slot_allocation_refuses_late_publication(provider, change):
    slot, deps = await _world(provider)
    rows = provider.get_storage(WorkspaceSession)
    workspaces = provider.get_storage(Workspace)
    await workspaces.patch_if("ws-1", {"access": Access(owners=["u1"]).model_dump(mode="json")}, where={"phase": ["pending"]})
    await rows.create(WorkspaceSession(
        id="caller", workspace_id="ws-1", status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        binding=AgentSessionBinding(agent_id="ag-1"), turn_no=2,
    ))
    receipts = provider.get_storage(SessionCreationIntent)
    async with asyncio.timeout(10):
        task = asyncio.create_task(_start(deps, calling_session_id="caller", calling_turn_no=2,
                                          workspace_person=Person("u1", "user")))
        try:
            await slot.allocated.wait()
            sid = slot.started[0]["id"]
            reserved = await receipts.get(sid)
            assert reserved.state == "preparing" and await rows.get(sid) is None
            if change == "end":
                await rows.patch_if("caller", {"status": "ended"}, where={"turn_no": [2]})
            elif change == "finish_turn":
                await rows.patch_if("caller", {"completed_turn_no": 2}, where={"turn_no": [2]})
            elif change == "restart":
                await rows.patch_if("caller", {"binding_epoch": 17}, where={"turn_no": [2]})
            elif change == "target":
                await workspaces.patch_if("ws-1", {"phase": "terminating"}, where={"phase": ["pending"]})
            elif change == "binding":
                await provider.get_storage(Agent).patch_if("ag-1", {"tools": ["new-tool"]}, where={"description": ["x"]})
            elif change == "access":
                await workspaces.patch_if("ws-1", {"access": Access(public=True).model_dump(mode="json")}, where={"phase": ["pending"]})
            else:
                await recover_preparations(provider, now=reserved.created_at + PREPARATION_GRACE - timedelta(seconds=1))
                assert (await receipts.get(sid)).state == "preparing"
                await recover_preparations(provider, now=reserved.created_at + PREPARATION_GRACE + timedelta(seconds=1))
                assert (await receipts.get(sid)).state == "abandoned"
            slot.release.set()
            expected = {"target": NotFoundError, "access": AccessDeniedError}.get(change, ConflictError)
            with pytest.raises(expected):
                await task
        finally:
            slot.release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert (await receipts.get(sid)).state == "abandoned"
    assert await rows.get(sid) is None
    assert deps.claim_engine.upserts == [] and deps.scheduler.enqueued == []
    assert len(slot.started) == 1


async def test_cancelled_owner_leaves_only_an_abandoned_receipt(provider):
    slot, deps = await _world(provider)
    async with asyncio.timeout(10):
        task = asyncio.create_task(_start(deps))
        try:
            await slot.allocated.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            slot.release.set()
            await asyncio.gather(task, return_exceptions=True)
    sid = slot.started[0]["id"]
    assert (await provider.get_storage(SessionCreationIntent).get(sid)).state == "abandoned"
    assert await provider.get_storage(WorkspaceSession).get(sid) is None
    assert not deps.claim_engine.upserts


async def test_lost_commit_reply_leaves_complete_retryable_first_turn(provider, monkeypatch):
    slot, deps = await _world(provider)
    slot.release.set()
    original = provider.locking_transaction

    @asynccontextmanager
    async def lose_publish_reply(*, models):
        async with original(models=models) as conn:
            yield conn
        if Workspace in models:
            raise ConnectionError("lost publish reply")

    monkeypatch.setattr(provider, "locking_transaction", lose_publish_reply)
    with pytest.raises(ConnectionError, match="lost publish reply"):
        await _start(deps)
    sid = slot.started[0]["id"]
    receipt = await provider.get_storage(SessionCreationIntent).get(sid)
    assert receipt.state == "committed"
    assert await abandon_preparation(provider, receipt) is None
    row = await provider.get_storage(WorkspaceSession).get(sid)
    assert row.status == SessionStatus.RUNNING and row.turn_status == "claimable" and row.started_at
    assert row.parent_session_id == "lineage-only" and row.last_seq == 1
    assert b"first instruction" in slot.message_lines[sid]
    assert "first instruction" not in receipt.model_dump_json()
    assert not deps.claim_engine.upserts
    deps.claim_engine.has_lease = AsyncMock(return_value=False)
    sweep = SessionMessagingSweeper(storage_provider=provider, scheduler=deps.scheduler,
                                    claim_engine=deps.claim_engine, workspace_registry=deps.workspace_registry)
    assert await sweep._heal_unclaimed_turns() == 1
    assert deps.claim_engine.upserts[0][1] == sid


async def test_recovery_cannot_abandon_another_occurrence(provider):
    store = provider.get_storage(SessionCreationIntent)
    receipt = await store.create(SessionCreationIntent(id="sess-test", workspace_id="ws"))
    stale = SessionCreationIntent(id=receipt.id, workspace_id=receipt.workspace_id)
    assert stale.occurrence != receipt.occurrence
    assert await abandon_preparation(provider, stale) is None
    assert (await store.get(receipt.id)).state == "preparing"
    await recover_preparations(provider, now=datetime.now(UTC) + PREPARATION_GRACE + timedelta(seconds=1))
    assert (await store.get(receipt.id)).state == "abandoned"


@pytest.mark.parametrize("provider", [pytest.param("postgres", marks=postgres_marks())], indirect=True)
async def test_peer_workspace_retirement_after_snapshot_rolls_publication_back(provider, monkeypatch):
    """A PG reader can pass the precheck while another connection is about to retire the workspace."""
    slot, deps = await _world(provider)
    slot.release.set()
    receipts = provider.get_storage(SessionCreationIntent)
    workspaces = provider.get_storage(Workspace)
    original = receipts.patch_if
    retiring = asyncio.Event()
    retired = asyncio.Event()

    async def intercept(*args, **kwargs):
        if kwargs.get("conn") is not None:
            retiring.set()
            await retired.wait()
        return await original(*args, **kwargs)

    async def retire():
        await retiring.wait()
        row = await workspaces.patch_if("ws-1", {"phase": "terminating"}, where={"phase": ["pending"]})
        assert row is not None
        retired.set()

    monkeypatch.setattr(receipts, "patch_if", intercept)
    async with asyncio.timeout(10):
        peer = asyncio.create_task(retire())
        try:
            with pytest.raises(NotFoundError):
                await _start(deps)
            await peer
        finally:
            peer.cancel()
            await asyncio.gather(peer, return_exceptions=True)
    sid = slot.started[0]["id"]
    assert (await receipts.get(sid)).state == "abandoned"
    assert await provider.get_storage(WorkspaceSession).get(sid) is None
    assert not deps.claim_engine.upserts
