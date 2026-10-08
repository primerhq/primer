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

    async def patch_if(self, id, patch=None, *, where, set_paths=None, conn=None):
        """The guarded field patch of ``Storage.patch_if`` for the top-level equality guards these tests use (a seq reservation)."""
        row = await self.get(id)
        if row is None:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"no entity with id {id!r}")
        if any(getattr(row, field) not in allowed for field, allowed in where.items()):
            return None
        self._row = row.model_copy(update=patch)
        return self._row


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
        human_intent=True,
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
        human_intent=True,
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
        human_intent=True,
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
        human_intent=True,
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
        workspace_id="ws-1", session_id="sess-1", instruction="hello",
        human_intent=True, deps=deps,
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
    ws = deps.workspace_registry._ws
    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction=None,
        human_intent=True,
        deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.pause_requested is False
    assert slot.appended == []  # no instruction supplied
    assert sched.enqueued == ["sess-1"]
    # 01a08c08: clearing a real pause must never be silent, even for the
    # human-intent case where clearing is the right call.
    records = _decode_records(ws)
    superseded = [r for r in records if r["kind"] == "pause_superseded"]
    assert len(superseded) == 1
    assert superseded[0]["payload"]["action"] == "cleared"


@pytest.mark.asyncio
async def test_paused_session_non_human_wake_queues_and_holds_pause():
    """01a08c08 ruling: a non-human wake (trigger fire, agent-to-agent
    steer, a queued message's own replay) must NOT clear an operator's
    pause. The instruction is queued as a PendingSessionMessage instead,
    and the row's request flags and status are left as they were (only last_seq moves,
    by the PAUSE_SUPERSEDED record's reserved seq)."""
    row = _row(SessionStatus.PAUSED)
    row.pause_requested = True
    deps, slot, sched, eng = _deps(row)
    ws = deps.workspace_registry._ws

    queued: list[dict] = []

    async def _fake_store_pending_steer(**kw):
        from primer.model.workspace_session import PendingSessionMessage

        queued.append(kw)
        return PendingSessionMessage(
            id="pending-1", session_id=kw["session"].id,
            parts=[{"type": "text", "text": kw["text"]}],
            enqueued_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )

    import primer.session.pending_messages as pm
    import unittest.mock

    with unittest.mock.patch.object(
        pm, "store_pending_steer", _fake_store_pending_steer,
    ):
        out = await wake_session(
            workspace_id="ws-1",
            session_id="sess-1",
            instruction="fire while paused",
            human_intent=False,
            deps=deps,
        )

    # The row's request state is untouched: still paused, no claimable flip, no status
    # advance, no scheduler/claim-engine pulse (only last_seq moves, by the reserved seq).
    assert out.status == SessionStatus.PAUSED
    assert out.pause_requested is True
    assert out.turn_status != "claimable"
    assert slot.appended == [], "must not append to the FIFO -- it's queued, not delivered"
    assert sched.enqueued == []
    assert eng.upserts == []

    # The instruction was queued, not lost.
    assert len(queued) == 1
    assert queued[0]["text"] == "fire while paused"

    # And it's observable: a PAUSE_SUPERSEDED record with the pending id.
    records = _decode_records(ws)
    superseded = [r for r in records if r["kind"] == "pause_superseded"]
    assert len(superseded) == 1
    assert superseded[0]["payload"]["action"] == "queued"
    assert superseded[0]["payload"]["pending_id"] == "pending-1"


@pytest.mark.asyncio
async def test_non_human_wake_of_an_unpaused_session_is_unaffected():
    """human_intent=False only changes behaviour when pause_requested is
    actually set -- an automated wake of an ordinary RUNNING/CREATED
    session must behave exactly as the human-intent path does."""
    row = _row(SessionStatus.CREATED)
    deps, slot, sched, eng = _deps(row)
    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="trigger fired",
        human_intent=False,
        deps=deps,
    )
    assert out.status == SessionStatus.RUNNING
    assert out.turn_status == "claimable"
    assert slot.appended == ["trigger fired"]
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
        human_intent=True,
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
async def test_a_session_ended_at_the_tool_turn_cap_reopens_and_runs_a_fresh_invocation():
    """An autonomous session whose turn the agent's max_tool_turns stopped ENDS with ended_reason
    "tool_turn_cap". It is restartable: a new human (or trigger) message reopens it and the reopened invocation
    starts a fresh round count, so it cannot loop on its own. Left out of the restartable set, steer and reset
    answered 409 and a steer queued during the capped turn was stranded (the drain swallows the ConflictError)."""
    row = _row(SessionStatus.ENDED)
    row.ended_reason = "tool_turn_cap"
    deps, slot, sched, eng = _deps(row)
    ws = deps.workspace_registry._ws

    out = await wake_session(
        workspace_id="ws-1",
        session_id="sess-1",
        instruction="try again",
        human_intent=True,
        deps=deps,
    )

    assert out.status == SessionStatus.RUNNING
    assert out.ended_reason is None
    assert out.metadata["invocation"] == 2
    assert slot.reopened is True and slot.appended == ["try again"]
    assert sched.enqueued == ["sess-1"]
    kinds = [r["kind"] for r in _decode_records(ws)]
    assert kinds.index("invocation_divider") < kinds.index("user_input")


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
            human_intent=True,
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
        human_intent=True,
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
            human_intent=True,
            deps=deps,
        )


# ---- an earlier Stop does not outlive a later explicit human message ------------------------------
# A human who sends a message after pressing Stop has re-engaged: that message wins. Left on the row,
# the flag would stop the very turn their message starts (before its first token).


async def _wake_with_flag(row, *, human_intent=True, instruction="carry on"):
    row.interrupt_requested = True
    deps, *_ = _deps(row)
    return await wake_session(
        workspace_id="ws-1", session_id="sess-1", instruction=instruction,
        human_intent=human_intent, deps=deps,
    )


@pytest.mark.asyncio
async def test_a_human_message_clears_an_earlier_stop_on_a_session_that_is_waiting():
    out = await _wake_with_flag(_row(SessionStatus.WAITING))
    assert out.interrupt_requested is False


@pytest.mark.asyncio
async def test_a_human_message_clears_an_earlier_stop_on_a_parked_session():
    row = _row(SessionStatus.RUNNING)
    row.parked_status = "parked"
    out = await _wake_with_flag(row)
    assert out.interrupt_requested is False


@pytest.mark.asyncio
async def test_a_human_message_clears_a_stop_recorded_while_the_next_turn_was_queued():
    row = _row(SessionStatus.RUNNING)
    row.turn_status = "claimable"
    out = await _wake_with_flag(row)
    assert out.interrupt_requested is False


@pytest.mark.asyncio
async def test_a_message_sent_while_a_turn_is_executing_does_not_cancel_the_stop_aimed_at_it():
    """The Stop is for the RUNNING turn; a steer queued behind it is not a reason to drop it."""
    row = _row(SessionStatus.RUNNING)
    row.turn_status = "running"
    out = await _wake_with_flag(row)
    assert out.interrupt_requested is True


@pytest.mark.asyncio
async def test_an_automated_wake_does_not_override_a_humans_stop():
    out = await _wake_with_flag(_row(SessionStatus.WAITING), human_intent=False)
    assert out.interrupt_requested is True


@pytest.mark.asyncio
async def test_a_wake_with_no_message_leaves_the_flag_alone():
    out = await _wake_with_flag(_row(SessionStatus.WAITING), instruction=None)
    assert out.interrupt_requested is True
