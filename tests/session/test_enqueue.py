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
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def __init__(self, row):
        self._s = _FakeStorage(row)

    def get_storage(self, cls):
        return self._s


class _FakeSlot:
    def __init__(self):
        self.appended = []
        self.appended_extra_parts = []
        self.reopened = False

    async def append_instruction(self, content, *, extra_parts=None):
        self.appended.append(content)
        self.appended_extra_parts.append(extra_parts)

    async def reopen(self):
        self.reopened = True


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


class _FakeWorkspaceRow:
    """Minimal stand-in for the persisted Workspace row's phase field."""

    def __init__(self, phase):
        self.phase = phase


class _FakeRegistry:
    def __init__(self, ws, ws_row=None):
        self._ws = ws
        # None mimics "workspace row no longer exists" - get_workspace_row
        # raises NotFoundError, matching the real WorkspaceRegistry.
        self._ws_row = ws_row

    async def get_workspace(self, wid):
        return self._ws

    async def get_workspace_row(self, wid):
        if self._ws_row is None:
            raise NotFoundError(f"workspace {wid!r} does not exist")
        return self._ws_row


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


def _deps(row, ws_row=None):
    slot = _FakeSlot()
    sched = _FakeScheduler()
    eng = _FakeEngine()
    deps = SessionWakeDeps(
        storage_provider=_FakeSP(row),
        scheduler=sched,
        claim_engine=eng,
        workspace_registry=_FakeRegistry(_FakeWorkspace(slot), ws_row=ws_row),
    )
    return deps, slot, sched, eng


@pytest.mark.asyncio
async def test_created_session_is_invoked_and_claimable():
    row = _row(SessionStatus.CREATED)
    deps, slot, sched, eng = _deps(row)
    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="hello",
        deps=deps,
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
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="steer me",
        deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.turn_status == "claimable"
    assert slot.appended == ["steer me"]
    assert sched.enqueued == ["sess-1"]


@pytest.mark.asyncio
async def test_extra_parts_forwarded_to_append_instruction():
    from primer.model.chat import ImagePart

    row = _row(SessionStatus.CREATED)
    deps, slot, sched, eng = _deps(row)
    image = ImagePart(artifact_id="art-1", mime_type="image/png")
    await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="look",
        extra_parts=[image],
        deps=deps,
    )
    assert slot.appended == ["look"]
    assert slot.appended_extra_parts == [[image]]


@pytest.mark.asyncio
async def test_extra_payload_merges_into_user_input_record():
    row = _row(SessionStatus.CREATED)
    deps, slot, sched, eng = _deps(row)
    ws = deps.workspace_registry._ws
    await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="look",
        extra_parts=["unused-marker"],  # only extra_payload is asserted here
        extra_payload={"attachments": ["uploads/pic.png"]},
        deps=deps,
    )
    records = _decode_records(ws)
    user_input = next(r for r in records if r["kind"] == "user_input")
    assert user_input["payload"]["text"] == "look"
    assert user_input["payload"]["attachments"] == ["uploads/pic.png"]


@pytest.mark.asyncio
async def test_no_extra_parts_or_payload_is_unchanged():
    """Every pre-existing caller (external_tools tests, restart, pending
    realize) omits both kwargs -- must behave exactly as before."""
    row = _row(SessionStatus.CREATED)
    deps, slot, sched, eng = _deps(row)
    ws = deps.workspace_registry._ws
    await wake_session(
        workspace_id="ws-1", session_id="sess-1", instruction="hello", deps=deps,
    )
    assert slot.appended_extra_parts == [None]
    records = _decode_records(ws)
    user_input = next(r for r in records if r["kind"] == "user_input")
    assert user_input["payload"] == {"text": "hello"}


@pytest.mark.asyncio
async def test_paused_session_resumes_and_clears_pause():
    row = _row(SessionStatus.PAUSED)
    row.pause_requested = True
    deps, slot, sched, eng = _deps(row)
    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction=None,
        deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.pause_requested is False
    assert slot.appended == []  # no instruction supplied
    assert sched.enqueued == ["sess-1"]


def _decode_records(ws):
    """Decode the messages.jsonl records the writer appended to the fake ws."""
    import json

    records = []
    for blob in ws.message_lines:
        for line in blob.decode().splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


@pytest.mark.asyncio
async def test_ended_restartable_session_reopens_and_runs():
    """A NEW message to an ENDED (restartable) session reopens it: reopen the
    slot, write an INVOCATION_DIVIDER (bumped invocation), then the normal
    wake flow appends the USER_INPUT (after the divider) and runs the turn."""
    row = _row(SessionStatus.ENDED)
    row.ended_reason = "completed"
    deps, slot, sched, eng = _deps(row)
    ws = deps.workspace_registry._ws

    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="again",
        deps=deps,
    )

    assert out.status == SessionStatus.RUNNING
    assert out.turn_status == "claimable"
    assert out.ended_reason is None
    assert out.metadata["invocation"] == 2
    assert slot.reopened is True
    assert slot.appended == ["again"]
    assert sched.enqueued == ["sess-1"]
    assert (ClaimKind.SESSION, "sess-1") in eng.upserts

    records = _decode_records(ws)
    kinds = [r["kind"] for r in records]
    assert "invocation_divider" in kinds
    assert "user_input" in kinds
    # Divider is written BEFORE the USER_INPUT message.
    assert kinds.index("invocation_divider") < kinds.index("user_input")
    divider = next(r for r in records if r["kind"] == "invocation_divider")
    assert divider["payload"]["invocation"] == 2


@pytest.mark.asyncio
async def test_ended_non_restartable_raises_conflict():
    """An ENDED session with a non-restartable ended_reason (workspace_lost /
    force_deleted) still cannot be reopened — wake_session raises."""
    row = _row(SessionStatus.ENDED)
    row.ended_reason = "workspace_lost"
    deps, slot, *_ = _deps(row)
    with pytest.raises(ConflictError):
        await wake_session(
            workspace_id="ws-1",
            session_id="sess-1",
            instruction="x",
            deps=deps,
        )
    assert slot.reopened is False


@pytest.mark.asyncio
async def test_ended_workspace_lost_reopens_once_workspace_healed():
    """01a0533c (live SEV): unlike force_deleted, workspace_lost is a
    probe-blip artifact, not a deliberate end - a new message reaches a
    session that was killed by a transient rollout hiccup once the
    workspace is running again, through wake_session's own public ENDED
    branch (not just the lower-level _reopen_ended_locked/reset_session
    covered in tests/session/test_reset.py)."""
    row = _row(SessionStatus.ENDED)
    row.ended_reason = "workspace_lost"
    deps, slot, *_ = _deps(row, ws_row=_FakeWorkspaceRow(phase="running"))
    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="x",
        deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.ended_reason is None
    assert slot.reopened is True


@pytest.mark.asyncio
async def test_missing_session_raises_not_found():
    deps, *_ = _deps(None)
    with pytest.raises(NotFoundError):
        await wake_session(
            workspace_id="ws-1",
            session_id="sess-1",
            instruction="x",
            deps=deps,
        )
