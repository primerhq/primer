from datetime import datetime, timezone

import pytest

from primer.int.claim import ClaimKind
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.enqueue import SessionWakeDeps, wake_session


class _FakeStorage:
    def __init__(self, row):
        self._row = row

    async def get(self, sid):
        return self._row if self._row and self._row.id == sid else None

    async def update(self, row):
        self._row = row
        return row


class _FakeSP:
    def __init__(self, row):
        self._s = _FakeStorage(row)

    def get_storage(self, cls):
        return self._s


class _FakeSlot:
    def __init__(self):
        self.appended = []

    async def append_instruction(self, content):
        self.appended.append(content)


class _FakeWorkspace:
    def __init__(self, slot):
        self._slot = slot
        # Captures messages.jsonl lines the WorkspaceMessageWriter appends
        # (wake_session persists a USER_INPUT record via workspace_io).
        self.message_lines: list[bytes] = []

    async def get_session(self, sid):
        return self._slot

    async def append_message_line(self, session_id, line):
        self.message_lines.append(line)


class _FakeRegistry:
    def __init__(self, ws):
        self._ws = ws

    async def get_workspace(self, wid):
        return self._ws


class _FakeScheduler:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, sid):
        self.enqueued.append(sid)


class _FakeEngine:
    def __init__(self):
        self.upserts = []

    async def upsert(self, kind, sid, *, priority=100, next_attempt_at=None):
        self.upserts.append((kind, sid))


def _row(status, autonomous=None):
    return WorkspaceSession(
        id="sess-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=status,
        autonomous=autonomous,
        created_at=datetime.now(timezone.utc),
    )


def _deps(row):
    slot = _FakeSlot()
    sched = _FakeScheduler()
    eng = _FakeEngine()
    deps = SessionWakeDeps(
        storage_provider=_FakeSP(row),
        scheduler=sched,
        claim_engine=eng,
        workspace_registry=_FakeRegistry(_FakeWorkspace(slot)),
    )
    return deps, slot, sched, eng


@pytest.mark.asyncio
async def test_created_session_is_invoked_and_claimable():
    row = _row(SessionStatus.CREATED)
    deps, slot, sched, eng = _deps(row)
    out = await wake_session(
        workspace_id="ws-1", session_id="sess-1",
        instruction="hello", deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.turn_status == "claimable"
    assert slot.appended == ["hello"]
    assert sched.enqueued == ["sess-1"]
    assert (ClaimKind.SESSION, "sess-1") in eng.upserts


@pytest.mark.asyncio
async def test_running_session_is_steered_without_status_change():
    row = _row(SessionStatus.RUNNING)
    deps, slot, sched, eng = _deps(row)
    out = await wake_session(
        workspace_id="ws-1", session_id="sess-1",
        instruction="steer me", deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.turn_status == "claimable"
    assert slot.appended == ["steer me"]
    assert sched.enqueued == ["sess-1"]


@pytest.mark.asyncio
async def test_paused_session_resumes_and_clears_pause():
    row = _row(SessionStatus.PAUSED)
    row.pause_requested = True
    deps, slot, sched, eng = _deps(row)
    out = await wake_session(
        workspace_id="ws-1", session_id="sess-1",
        instruction=None, deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.pause_requested is False
    assert slot.appended == []  # no instruction supplied
    assert sched.enqueued == ["sess-1"]


@pytest.mark.asyncio
async def test_ended_session_raises_conflict():
    row = _row(SessionStatus.ENDED)
    deps, *_ = _deps(row)
    with pytest.raises(ConflictError):
        await wake_session(
            workspace_id="ws-1", session_id="sess-1",
            instruction="x", deps=deps,
        )


@pytest.mark.asyncio
async def test_missing_session_raises_not_found():
    deps, *_ = _deps(None)
    with pytest.raises(NotFoundError):
        await wake_session(
            workspace_id="ws-1", session_id="sess-1",
            instruction="x", deps=deps,
        )
