"""Resuming a park whose workspace the deployment REFUSES keeps the park and the session (ticket 01a1072f).

The resume branch of ``WorkerPool._run_engine_session`` used to let the refusal reach the generic ``except``: the lease
was released as a failure, the session adapter cleared the park (so the approval or answer the human gave was lost) and
tried to write its error record into the refused workspace. Now the pool asks the registry BEFORE anything is emitted or
cleared: the session is PAUSED with the reason on the row, the park is kept for ``/resume`` to replay, no
``session.resumed`` event is reported for a resume that did not happen, and a cancelled session still ends cancelled.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.int.claim import ClaimKind
from primer.model.scheduler import RuntimeMode, SchedulerProviderType
from primer.model.workspace import Workspace as WorkspaceRow
from primer.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
)
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.workspace.local_policy import LocalWorkspacePolicy
from tests.conftest import _FakeStorageProvider
from tests.worker.test_engine_session_resume import (
    _build_engine,
    _build_pool,
    _claim_session,
    _make_resumable_session,
)

SID = "sess-refused"


class _EventBus:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, key, payload=None, **kwargs):
        self.published.append(key)


async def _setup(*, refusing: bool = True, **session_fields):
    storage_provider = _FakeStorageProvider()
    await storage_provider.get_storage(WorkspaceProvider).create(WorkspaceProvider(
        id="local", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-refused"),
    ))
    await storage_provider.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(
        id="local-tpl", description="t", provider_id="local", backend={"kind": "local"},
    ))
    await storage_provider.get_storage(WorkspaceRow).create(WorkspaceRow(
        id=f"ws-{SID}", template_id="local-tpl", provider_id="local", created_at=datetime.now(timezone.utc),
        phase="running", runtime_meta={"url": "ws://unused", "token": "unused"},
    ))
    sessions = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(sessions)
    pool = _build_pool(storage_provider, engine, event_bus=_EventBus())
    policy = LocalWorkspacePolicy.from_topology(
        runtime_mode=RuntimeMode.WORKER, scheduler_provider=SchedulerProviderType.POSTGRES, enforce=refusing,
    )
    pool._workspace_registry = WorkspaceRegistry(storage_provider, local_policy=policy)
    row = _make_resumable_session(SID, tool_name="ask_user", tool_call_id="tc-1", resume_event_payload={"response": "x"})
    for name, value in session_fields.items():
        setattr(row, name, value)
    await sessions.create(row)
    return pool, engine, sessions


@pytest.fixture
def emitted(monkeypatch) -> list[str]:
    """The event types the resume handler reports, so a test can see whether it claimed a resume happened."""
    events: list[str] = []

    class _Recorder:
        async def emit(self, event_type, **kwargs):
            events.append(event_type)

    monkeypatch.setattr("primer.events.recorder.recorder_for", lambda *a, **kw: _Recorder())
    return events


async def test_a_refused_resume_pauses_the_session_and_keeps_the_park(emitted) -> None:
    pool, engine, sessions = await _setup()
    before = await sessions.get(SID)

    await pool._run_engine_session(await _claim_session(engine, SID))

    row = await sessions.get(SID)
    assert row.status == SessionStatus.PAUSED
    assert row.ended_reason is None
    assert "local workspace provider" in row.workspace_refusal and f"ws-{SID}" in row.workspace_refusal
    assert row.parked_status == "resumable", "the human's answer must stay replayable by /resume"
    assert row.parked_state == before.parked_state
    assert row.turn_no == before.turn_no, "a refused resume is not a turn"
    leases = await engine.claim_due("wrk-engine-resume", max_count=10)
    assert not any(lease.entity_id == SID for lease in leases), "the lease is given back, not re-claimed in a loop"


async def test_a_refused_resume_reports_no_resume(emitted) -> None:
    """The handler emits ``session.resumed`` BEFORE it loads the workspace, so a refusal found there would have
    reported a resume that never happened."""
    pool, engine, _ = await _setup()

    await pool._run_engine_session(await _claim_session(engine, SID))

    assert "session.resumed" not in emitted


async def test_a_cancelled_refused_session_still_ends_cancelled(emitted) -> None:
    pool, engine, sessions = await _setup(cancel_requested=True)

    await pool._run_engine_session(await _claim_session(engine, SID))

    row = await sessions.get(SID)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
    assert row.workspace_refusal is None


async def test_a_refusal_raised_from_inside_a_handler_is_handled_the_same_way(monkeypatch) -> None:
    """The pre-check is not the only way in: any refusal that reaches the pool pauses instead of failing."""
    pool, engine, sessions = await _setup(refusing=False)

    async def handler(lease, row):
        raise WorkspaceRefusedError("refused from deep inside", provider_id="local")

    monkeypatch.setattr(pool, "_resume_engine_session", handler)

    await pool._run_engine_session(await _claim_session(engine, SID))

    row = await sessions.get(SID)
    assert row.status == SessionStatus.PAUSED and row.workspace_refusal == "refused from deep inside"
    assert row.parked_status == "resumable"


async def test_a_deployment_that_allows_local_resumes_as_before(monkeypatch) -> None:
    pool, engine, sessions = await _setup(refusing=False)
    ran: list[str] = []

    async def handler(lease, row):
        from primer.int.claim import ReleaseOutcome
        ran.append(row.id)
        return ReleaseOutcome(success=True, drop_lease=True)

    monkeypatch.setattr(pool, "_resume_engine_session", handler)

    await pool._run_engine_session(await _claim_session(engine, SID))

    assert ran == [SID]
    assert (await sessions.get(SID)).workspace_refusal is None
