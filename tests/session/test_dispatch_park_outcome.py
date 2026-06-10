"""Tests that run_one_session_turn returns a park ReleaseOutcome on YieldToWorker.

Verifies the F10c loop fix: when the executor raises YieldToWorker the dispatch
must return drop_lease=True and a populated ParkRequest, so the lease is
released and the session row is written with park columns (not re-claimed).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn


# ---------------------------------------------------------------------------
# Helpers / stubs - mirrors tests/session/test_dispatch.py exactly
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_lease(session_id: str = "s1") -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION,
        entity_id=session_id,
        claimed_by="worker-1",
        claimed_at=now,
        expires_at=now,
        attempt_count=1,
        last_error=None,
    )


class FakeWorkspaceIO:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = defaultdict(bytes)

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self._data[(session_id, "messages.jsonl")] += line

    def read_lines(self, session_id: str, filename: str = "messages.jsonl") -> list[str]:
        raw = self._data.get((session_id, filename), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]


async def _seed_session(
    storage_provider,
    session_id: str = "s1",
) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    storage = storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    return sess


# ---------------------------------------------------------------------------
# Fixtures - same structure as test_dispatch.py
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_workspace_io() -> FakeWorkspaceIO:
    return FakeWorkspaceIO()


@pytest.fixture
async def fake_event_bus():
    bus = InMemoryEventBus()
    await bus.initialize()
    yield bus
    await bus.aclose()


@pytest.fixture
def fake_storage_provider():
    from tests.conftest import _FakeStorageProvider
    return _FakeStorageProvider()


@pytest.fixture
async def seeded_session(fake_storage_provider) -> WorkspaceSession:
    return await _seed_session(fake_storage_provider)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yield_to_worker_returns_park_outcome(
    seeded_session: WorkspaceSession,
    fake_workspace_io: FakeWorkspaceIO,
    fake_event_bus: InMemoryEventBus,
    fake_storage_provider,
) -> None:
    """YieldToWorker must return drop_lease=True and a populated ParkRequest.

    This is the core F10c fix: before this change the dispatch returned
    drop_lease=False / park=None, so the lease persisted and the session
    was re-claimed in an infinite loop with the park columns never written.
    """
    yielded = Yielded(
        tool_name="ask_user",
        event_key="ask_user:s1:tc-1",
        resume_metadata={"prompt": "what is your name?"},
    )
    exc = YieldToWorker(yielded, tool_call_id="tc-1", llm_messages=[])

    class _YieldingExecutor:
        async def invoke(self, messages: list[Any], **kwargs: Any):
            raise exc
            yield  # make this an async generator

    async def _build_executor(session: WorkspaceSession):
        return _YieldingExecutor()

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider,
        workspace_io=fake_workspace_io,
        event_bus=fake_event_bus,
        build_executor=_build_executor,
    )
    lease = _make_lease(seeded_session.id)
    outcome = await run_one_session_turn(lease, deps)

    # Core assertions: lease must be dropped and park must be populated
    assert outcome.success is True
    assert outcome.drop_lease is True
    assert outcome.park is not None

    # ParkRequest must carry the right event_key
    assert outcome.park.parked_event_key == "ask_user:s1:tc-1"

    # The parked_state blob must encode the Yielded sentinel
    blob = outcome.park.parked_state
    assert blob["yielded"]["tool_name"] == "ask_user"

    # tool_call_id must be at the top level of the blob
    assert blob["tool_call_id"] == "tc-1"

    # Timestamps must be populated
    assert outcome.park.parked_at is not None
    assert outcome.park.parked_until is not None
    # parked_until must be after parked_at (timeout applied)
    assert outcome.park.parked_until > outcome.park.parked_at


@pytest.mark.asyncio
async def test_multi_event_park_persists_parked_event_keys(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
):
    from typing import Any
    from primer.model.yield_ import Yielded, YieldToWorker
    yielded = Yielded(tool_name="_approval", event_key="ask_user:s1:tc-1",
                      event_keys=["ask_user:s1:tc-1", "ask_user:s1:tc-2"],
                      resume_metadata={})
    exc = YieldToWorker(yielded, tool_call_id="tc-1", llm_messages=[])

    class _Yielding:
        async def invoke(self, messages: list[Any], **kw: Any):
            raise exc
            yield

    async def _build(session):
        return _Yielding()

    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io,
        event_bus=fake_event_bus, build_executor=_build)
    outcome = await run_one_session_turn(_make_lease(seeded_session.id), deps)
    assert outcome.park is not None
    assert outcome.park.parked_event_keys == ["ask_user:s1:tc-1", "ask_user:s1:tc-2"]
    # The blob's yielded carries the full set too (for resume + dispatch).
    assert outcome.park.parked_state["yielded"]["event_keys"] == \
        ["ask_user:s1:tc-1", "ask_user:s1:tc-2"]
