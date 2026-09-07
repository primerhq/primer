"""Unit tests for ClaimEngineFactory bus-type dispatch."""

from __future__ import annotations

import json
from collections import defaultdict

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.factory import ClaimEngineFactory
from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


class _FakeStorageProvider:
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    """Minimal storage-provider stub for factory tests."""

    def get_storage(self, model_class):
        return None

    @property
    def leases_table(self) -> str:
        return '"test"."leases"'

    @property
    def schema(self) -> str:
        return "test"

    # asyncpg pool stub — never actually used in unit tests
    pool = None


class _FakePostgresEventBus:
    """Non-InMemory bus — factory should choose PostgresClaimEngine."""

    pass


class _FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(self, session_id: str) -> list[str]:
        raw = self._data.get((session_id, "messages.jsonl"), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


class _FakeWorkspaceRegistry:
    def __init__(self) -> None:
        self.workspaces: dict[str, _FakeWorkspaceIO] = {}

    async def get_workspace(self, workspace_id: str) -> _FakeWorkspaceIO:
        return self.workspaces.setdefault(workspace_id, _FakeWorkspaceIO())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_factory_returns_in_memory_engine_for_in_memory_bus():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=InMemoryEventBus(),
    )
    assert isinstance(engine, InMemoryClaimEngine)


def test_factory_returns_postgres_engine_for_non_in_memory_bus():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=_FakePostgresEventBus(),
    )
    assert isinstance(engine, PostgresClaimEngine)


def test_factory_in_memory_engine_has_all_four_adapters():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=InMemoryEventBus(),
    )
    assert isinstance(engine, InMemoryClaimEngine)
    assert ClaimKind.SESSION in engine._adapters
    assert ClaimKind.HARNESS in engine._adapters
    # Pre-existing typo fixed in the same edit that added TOOL_CALL below:
    # this assertion used to repeat HARNESS a second time and never
    # actually checked TRIGGER at all.
    assert ClaimKind.TRIGGER in engine._adapters
    assert ClaimKind.TOOL_CALL in engine._adapters


def test_factory_postgres_engine_has_all_four_adapters():
    engine = ClaimEngineFactory.create(
        storage_provider=_FakeStorageProvider(),
        event_bus=_FakePostgresEventBus(),
    )
    assert isinstance(engine, PostgresClaimEngine)
    assert ClaimKind.SESSION in engine._adapters
    assert ClaimKind.HARNESS in engine._adapters
    assert ClaimKind.TRIGGER in engine._adapters
    assert ClaimKind.TOOL_CALL in engine._adapters


# ---------------------------------------------------------------------------
# 01a068ea: activation — the factory-constructed SESSION adapter must
# actually WRITE a terminal error record, not just have workspace_registry/
# event_bus threaded through as unused params. Drives the same construction
# path production startup uses (ClaimEngineFactory.create), then exercises
# the built adapter's on_release exactly like the claim engine would on a
# worker crash / reclaim.
# ---------------------------------------------------------------------------


class _FakeSessionStorage:
    def __init__(self, session) -> None:
        self._session = session
        self.updated = []

    async def get(self, id: str, *, conn=None):
        return self._session if self._session.id == id else None

    async def update(self, entity, *, conn=None):
        self.updated.append(entity)
        self._session = entity
        return entity


class _StorageProviderWithSessions(_FakeStorageProvider):
    """Routes WorkspaceSession lookups to a real fake Storage; everything
    else (harness/trigger) still gets None, matching _FakeStorageProvider —
    this test only cares about the SESSION adapter's activation."""

    def __init__(self, session_storage) -> None:
        self._session_storage = session_storage

    def get_storage(self, model_class):
        from primer.model.workspace_session import WorkspaceSession

        if model_class is WorkspaceSession:
            return self._session_storage
        return None


@pytest.mark.asyncio
async def test_factory_wired_session_adapter_writes_terminal_error_record():
    from datetime import datetime, timezone
    from primer.model.workspace_session import (
        AgentSessionBinding, SessionStatus, WorkspaceSession,
    )

    session = WorkspaceSession(
        id="s-activation",
        workspace_id="ws-activation",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )
    session_storage = _FakeSessionStorage(session)
    registry = _FakeWorkspaceRegistry()

    engine = ClaimEngineFactory.create(
        storage_provider=_StorageProviderWithSessions(session_storage),
        event_bus=InMemoryEventBus(),
        workspace_registry=registry,
    )
    adapter = engine._adapters[ClaimKind.SESSION]

    await adapter.on_release(
        conn=None,
        entity_id="s-activation",
        outcome=ReleaseOutcome(success=False, last_error="worker_crash"),
    )

    lines = registry.workspaces["ws-activation"].read_lines("s-activation")
    assert lines, (
        "the factory-wired adapter must actually write a terminal error "
        "record, not just accept the params unused"
    )
    record = json.loads(lines[0])
    assert record["kind"] == "error"
    assert record["payload"]["reason"] == "worker_crash"


# ---------------------------------------------------------------------------
# 01a0518b review, required test: ClaimEngineFactory.create's
# bind_post_release_hook wiring is the ONLY production binding of the
# mixed-park wake seam's post-commit hook - every existing wake test
# (test_release_fence.py, test_tool_wait_seam_e2e.py) hand-wires its OWN
# hook closure to drive the scenario, so a factory regression (the hook
# never bound, bound to the wrong engine, or the closure's session lookup
# broken) would pass the whole suite untouched. This drives the REAL
# construction path end-to-end instead: build via the factory, dispatch a
# two-task tool_wait batch through the factory's OWN TOOL_CALL adapter,
# release both siblings, and confirm the session row actually flips to
# resumable - proof the hook fired via the factory's own wiring, not a
# test-authored stand-in.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_bound_hook_wakes_session_on_last_sibling_release():
    from datetime import datetime, timezone

    from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
    from primer.model.workspace_session import (
        AgentSessionBinding, SessionStatus, WorkspaceSession,
    )

    from tests.conftest import _FakeStorageProvider

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    task_storage = storage_provider.get_storage(ToolCallTask)

    session_id = "s-factory-wake"
    # scoped_task_id="x:tool:0:1" -> node segment "x" (tool_wait_event_key's
    # own split) - matches the chat/workspace surface's node_id=None
    # convention, so this is the exact key the factory-wired adapter's
    # last-sibling check must reconstruct.
    session = WorkspaceSession(
        id=session_id,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.WAITING,
        turn_status="idle",
        parked_status="parked",
        parked_event_key=f"tool_wait:{session_id}:0:x",
        parked_state={"kind": "tool_wait"},
        created_at=datetime.now(timezone.utc),
    )
    await session_storage.create(session)

    batch = ["x:tool:0:1", "x:tool:0:2"]
    for tid in batch:
        await task_storage.create(ToolCallTask(
            id=tid, session_id=session_id, turn_no=0, tool_name="t",
            state=ToolCallTaskState.QUEUED, record_seq=1,
            created_at=datetime.now(timezone.utc), batch_task_ids=list(batch),
        ))

    engine = ClaimEngineFactory.create(
        storage_provider=storage_provider, event_bus=InMemoryEventBus(),
    )
    for tid in batch:
        await engine.upsert(ClaimKind.TOOL_CALL, tid)
    leases = await engine.claim_due(
        "worker-A", max_count=2, kinds=[ClaimKind.TOOL_CALL],
    )
    assert len(leases) == 2

    # Releasing the FIRST sibling must not wake the session yet.
    lease_by_id = {lease.entity_id: lease for lease in leases}
    first, second = batch[0], batch[1]
    task = await task_storage.get(first)
    await task_storage.update(task.model_copy(update={
        "result_state": {"id": first, "output": "result A", "error": False},
    }))
    await engine.release(
        lease_by_id[first], outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    row = await session_storage.get(session_id)
    assert row.parked_status == "parked"

    # Releasing the LAST sibling must wake it - via the factory's OWN
    # bound hook, not a test-authored one.
    task = await task_storage.get(second)
    await task_storage.update(task.model_copy(update={
        "result_state": {"id": second, "output": "result B", "error": False},
    }))
    await engine.release(
        lease_by_id[second], outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    row = await session_storage.get(session_id)
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"tool_wait_ready": True}
    assert row.parked_state["resume_event_key"] == f"tool_wait:{session_id}:0:x"
