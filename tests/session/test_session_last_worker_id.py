"""``last_worker_id`` is stamped at claim time and survives the release.

The claim engine hands the lease to the claiming worker (``lease.claimed_by``);
the session adapter stamps that id onto the row when the claim is taken
(``ClaimAdapter.on_claim``), so an in-flight turn is visible: a first turn in
flight already shows its worker in the session list's worker column and in
``GET /v1/sessions?worker_id=``, not an empty cell. A re-claim by another
worker overwrites the stamp (last toucher wins), and the release stops
clearing it: the three ``on_release`` branches no longer write
``last_worker_id: None``, so the stamp persists across the lease drop.

Each test drives the real claim path end to end: ``engine.upsert`` ->
``engine.claim_due`` (the claim, and the stamp) -> ``run_one_session_turn``
(the completed turn) -> ``engine.release`` (the lease drop), then reads the
row back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeWorkspaceIO:
    """Minimal workspace I/O: message-line appends only (mirrors
    test_dispatch.py's local copy)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        key = (session_id, "messages.jsonl")
        self._data[key] = self._data.get(key, b"") + line


class _ScriptedTurnExecutor:
    """A scripted happy-path turn: one text delta plus a Done."""

    async def invoke(self, messages: Any, **kwargs: Any):
        yield TextDelta(text="hi", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")


async def _seed_session(storage_provider: _FakeStorageProvider, session_id: str) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_status="running",
    )
    await storage_provider.get_storage(WorkspaceSession).create(sess)
    return sess


def _engine_for(storage_provider: _FakeStorageProvider) -> InMemoryClaimEngine:
    """The real in-memory claim engine with the real session adapter (the
    engine + adapter pair production startup builds; only the storage is
    fake)."""
    return InMemoryClaimEngine(adapters={
        ClaimKind.SESSION: SessionClaimAdapter(
            session_storage=storage_provider.get_storage(WorkspaceSession),
        ),
    })


async def _claim(engine: InMemoryClaimEngine, session_id: str, worker_id: str):
    """Arm the lease and claim it as ``worker_id`` (the stamp moment)."""
    await engine.upsert(ClaimKind.SESSION, session_id)
    return next(
        lease
        for lease in await engine.claim_due(worker_id, max_count=10)
        if lease.entity_id == session_id
    )


async def _completed_turn(
    storage_provider: _FakeStorageProvider,
    event_bus: InMemoryEventBus,
    lease,
):
    """Run the turn behind ``lease``; returns the outcome. The release is
    the caller's job: the stamp must survive it."""

    async def _build_executor(session: WorkspaceSession):
        return _ScriptedTurnExecutor()

    deps = SessionDispatchDeps(
        storage_provider=storage_provider,
        workspace_io=_FakeWorkspaceIO(),
        event_bus=event_bus,
        build_executor=_build_executor,
    )
    outcome = await run_one_session_turn(lease, deps)
    assert outcome.success is True
    return outcome


@pytest.fixture
async def fake_event_bus():
    bus = InMemoryEventBus()
    await bus.initialize()
    yield bus
    await bus.aclose()


@pytest.fixture
def fake_storage_provider():
    return _FakeStorageProvider()


@pytest.mark.asyncio
async def test_completed_turn_stamps_the_claiming_worker(
    fake_storage_provider: _FakeStorageProvider,
    fake_event_bus: InMemoryEventBus,
) -> None:
    """1. After a completed turn, the row carries the claiming worker's id."""
    session = await _seed_session(fake_storage_provider, "s-stamp-1")
    engine = _engine_for(fake_storage_provider)
    lease = await _claim(engine, session.id, "worker-1")
    outcome = await _completed_turn(fake_storage_provider, fake_event_bus, lease)
    await engine.release(lease, outcome=outcome)

    row = await fake_storage_provider.get_storage(WorkspaceSession).get(session.id)
    assert row is not None
    assert row.last_worker_id == "worker-1"
    # sanity: the turn actually completed (a completed turn, not an armed lease)
    assert row.turn_no == 1


@pytest.mark.asyncio
async def test_stamp_survives_the_release(
    fake_storage_provider: _FakeStorageProvider,
    fake_event_bus: InMemoryEventBus,
) -> None:
    """2. The completed turn drops the lease; the stamp must survive the drop
    (the release stops clearing it)."""
    session = await _seed_session(fake_storage_provider, "s-stamp-2")
    engine = _engine_for(fake_storage_provider)
    lease = await _claim(engine, session.id, "worker-1")
    outcome = await _completed_turn(fake_storage_provider, fake_event_bus, lease)
    assert outcome.drop_lease is True
    await engine.release(lease, outcome=outcome)
    assert await engine.has_live_lease(ClaimKind.SESSION, session.id) is False

    row = await fake_storage_provider.get_storage(WorkspaceSession).get(session.id)
    assert row is not None
    assert row.last_worker_id == "worker-1"


@pytest.mark.asyncio
async def test_second_workers_reclaim_overwrites_the_stamp(
    fake_storage_provider: _FakeStorageProvider,
    fake_event_bus: InMemoryEventBus,
) -> None:
    """4. Last toucher wins: worker-2's re-claim overwrites worker-1's stamp."""
    session = await _seed_session(fake_storage_provider, "s-stamp-3")
    engine = _engine_for(fake_storage_provider)
    lease = await _claim(engine, session.id, "worker-1")
    outcome = await _completed_turn(fake_storage_provider, fake_event_bus, lease)
    await engine.release(lease, outcome=outcome)
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    assert (await storage.get(session.id)).last_worker_id == "worker-1"

    # The completed turn dropped the lease: re-arm it and let worker-2 claim
    # it. The stamp must flip to worker-2 at the claim itself, before any
    # release of the second claim.
    lease2 = await _claim(engine, session.id, "worker-2")
    assert (await storage.get(session.id)).last_worker_id == "worker-2"
    await engine.release(
        lease2, outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
