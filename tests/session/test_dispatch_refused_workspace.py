"""A turn that meets a REFUSED workspace fails the turn and leaves the session resumable (ticket 01a1072f).

Before this, ``build_executor`` raising anything ended the session ``failed`` (terminal), and the release then tried
to write its error record into the very workspace that was refused, which raised again inside the release
transaction and kept the lease from dropping. A refused workspace is intact and usable again once it moves to a docker
or kubernetes provider, so the session is PAUSED (the existing ``/resume`` verb re-arms it), the reason is kept on the
row (``messages.jsonl`` lives in the refused workspace and cannot hold it), no turn is counted, and the lease drops
without the adapter's release running at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.int.claim import ClaimKind, Lease
from primer.model.except_ import NotFoundError
from primer.model.workspace_refusal import WorkspaceRefusedError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider

REFUSAL = WorkspaceRefusedError(
    "workspace 'w1': provider 'local' is a local workspace provider, which this deployment does not allow",
    provider_id="local", workspace_id="w1", signals=("runtime_mode is 'worker'",),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease(session_id: str) -> Lease:
    now = _now()
    return Lease(
        kind=ClaimKind.SESSION, entity_id=session_id, claimed_by="worker-1", claimed_at=now, expires_at=now,
        attempt_count=1, last_error=None,
    )


class _Io:
    """Records every message line written, so a test can prove nothing was written into the refused workspace."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)


@pytest.fixture
def storage_provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


async def _seed(storage_provider, **fields) -> WorkspaceSession:
    row = WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING,
        created_at=_now(), turn_status="running", turn_no=4, **fields,
    )
    await storage_provider.get_storage(WorkspaceSession).create(row)
    return row


async def _run(storage_provider, build_executor):
    bus = InMemoryEventBus()
    await bus.initialize()
    io = _Io()
    deps = SessionDispatchDeps(
        storage_provider=storage_provider, workspace_io=io, event_bus=bus, build_executor=build_executor,
    )
    try:
        return await run_one_session_turn(_lease("s1"), deps), io
    finally:
        await bus.aclose()


async def _row(storage_provider) -> WorkspaceSession:
    row = await storage_provider.get_storage(WorkspaceSession).get("s1")
    assert row is not None
    return row


async def test_a_refused_workspace_pauses_the_session_instead_of_ending_it(storage_provider) -> None:
    await _seed(storage_provider, interrupt_requested=True)

    async def build_executor(session):
        raise REFUSAL

    outcome, io = await _run(storage_provider, build_executor)

    row = await _row(storage_provider)
    assert row.status == SessionStatus.PAUSED, "resumable through the existing /resume verb, not terminal"
    assert row.ended_reason is None and row.ended_at is None
    assert row.workspace_refusal == REFUSAL.message
    assert row.turn_status == "idle" and row.turn_started_at is None, "no re-claim loop, no stuck 'running'"
    assert row.agent_phase is None
    assert row.interrupt_requested is False
    assert row.turn_no == 4, "a refused turn is not a turn"
    assert io.lines == [], "nothing may be written into the refused workspace"
    assert outcome.drop_lease is True
    assert outcome.entity_noop is True, (
        "the engine must give the lease back WITHOUT the session adapter's release, which would try to write "
        "its error record into the refused workspace and bump turn_no for a turn that never ran"
    )


async def test_any_other_build_failure_still_ends_the_session_failed(storage_provider) -> None:
    await _seed(storage_provider)

    async def build_executor(session):
        raise NotFoundError("Graph 'g-gone' not found")

    outcome, _ = await _run(storage_provider, build_executor)

    row = await _row(storage_provider)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "failed"
    assert row.workspace_refusal is None
    assert outcome.entity_noop is False


async def test_a_turn_that_starts_clears_an_earlier_refusal(storage_provider) -> None:
    await _seed(storage_provider, workspace_refusal="an earlier refusal")
    seen: dict = {}

    async def build_executor(session):
        seen["refusal_when_building"] = (await storage_provider.get_storage(WorkspaceSession).get("s1")).workspace_refusal
        raise NotFoundError("stop here")

    await _run(storage_provider, build_executor)

    assert seen["refusal_when_building"] is None, "the banner must not outlive the retry that got past the workspace"


async def test_a_session_ended_meanwhile_is_not_brought_back_by_the_refusal(storage_provider) -> None:
    from primer.session.dispatch import pause_session_for_refused_workspace

    await _seed(storage_provider)
    sessions = storage_provider.get_storage(WorkspaceSession)
    ended = (await sessions.get("s1")).model_copy(update={"status": SessionStatus.ENDED, "ended_reason": "cancelled"})
    await sessions.update(ended)

    outcome = await pause_session_for_refused_workspace(sessions, "s1", REFUSAL)

    row = await _row(storage_provider)
    assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
    assert row.workspace_refusal is None
    assert outcome.entity_noop is True
