# tests/session/test_reset.py
from datetime import datetime, timezone

import pytest

from primer.model.except_ import ConflictError
from primer.model.principal import PrincipalRef
from primer.model.workspace_session import (
    AgentSessionBinding,
    ResponderReservation,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.reset import (
    SessionResetDeps,
    _reopen_ended_locked,
    reset_session,
)
from primer.session.responder_attribution import responder_source


class _Storage:
    def __init__(self, row):
        self._row = row

    async def get(self, sid):
        return self._row if self._row and self._row.id == sid else None

    async def update(self, row):
        self._row = row
        return row

    async def patch_if(self, sid, patch=None, *, where, set_paths=None, conn=None):
        # The reopen decides and writes with guarded field patches (audit G7-03, review H2); the reference semantics.
        from primer.model.except_ import NotFoundError
        from tests.storage._patch_reference import patch_model

        for allowed in where.values():
            assert isinstance(allowed, list)
            assert all(
                value is None or isinstance(value, (str, int, float, bool))
                for value in allowed
            ), "production patch_if guards must contain JSON scalars"
        if self._row is None or self._row.id != sid:
            raise NotFoundError(f"no entity with id {sid!r}")
        updated = patch_model(self._row, patch, where=where, set_paths=set_paths)
        if updated is not None:
            self._row = updated
        return updated


class _SP:
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

    def __init__(self, row):
        self._s = _Storage(row)

    def get_storage(self, cls):
        return self._s


class _Slot:
    def __init__(self):
        self.reopened = False

    async def reopen(self):
        self.reopened = True


class _WS:
    def __init__(self, slot):
        self._slot = slot
        self.lines = []

    async def get_session(self, sid):
        return self._slot

    async def append_message_line(self, session_id, line):
        self.lines.append(line)


class _WSRow:
    """Minimal stand-in for the persisted Workspace row's phase field."""

    def __init__(self, phase):
        self.phase = phase


class _Registry:
    def __init__(self, ws, ws_row=None):
        self._ws = ws
        # None mimics "workspace row no longer exists" - get_workspace_row
        # raises NotFoundError, matching the real WorkspaceRegistry.
        self._ws_row = ws_row

    async def get_workspace(self, wid):
        return self._ws

    async def get_workspace_row(self, wid):
        if self._ws_row is None:
            from primer.model.except_ import NotFoundError

            raise NotFoundError(f"workspace {wid!r} does not exist")
        return self._ws_row


def _ended_row(reason="completed", last_seq=5):
    return WorkspaceSession(
        id="sess-1",
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="a1"),
        status=SessionStatus.ENDED,
        ended_reason=reason,
        ended_at=datetime.now(timezone.utc),
        last_seq=last_seq,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_reset_reopens_row_and_writes_divider():
    row = _ended_row().model_copy(update={
        "parked_status": "resumable",
        "parked_event_key": "tool_wait:sess-1:x:tool:0:1",
        "parked_at": datetime.now(timezone.utc),
        "parked_state": {
            "kind": "tool_wait",
            "outstanding_task_ids": ["sess-1/x:tool:0:1"],
            "notifying_task_ids": [],
        },
        "parked_tool_batches": True,
        "resumable_at": datetime.now(timezone.utc),
    })
    slot = _Slot()
    ws = _WS(slot)
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(ws),
    )
    out, invocation = await reset_session(
        workspace_id="ws-1",
        session_id="sess-1",
        deps=deps,
        human_intent=True,
    )
    assert out.status == SessionStatus.CREATED
    assert out.ended_reason is None
    assert out.ended_at is None
    assert out.turn_status == "idle"
    assert slot.reopened is True
    assert invocation == 2
    assert len(ws.lines) == 1
    assert SessionMessageKind.INVOCATION_DIVIDER.value in ws.lines[0].decode()
    assert out.responder_seq == 0
    assert out.responder_reservation_version == 1
    assert out.responder_reservations == {}
    assert out.responder_epoch is not None and len(out.responder_epoch) == 32
    assert out.parked_status is None and out.parked_state is None
    assert out.parked_tool_batches is None and out.resumable_at is None


@pytest.mark.asyncio
async def test_reset_rotates_active_responder_state_in_the_closing_cas():
    person = PrincipalRef(
        type="user", id="u-1", display="alice", role="user", source="local",
    )
    source = responder_source("user_input", {"occurrence": "first"})
    accepted_at = datetime.now(timezone.utc)
    reservation = ResponderReservation(
        domain=source.domain,
        source_identity=source.identity,
        person=person,
        seq=4,
        accepted_at=accepted_at,
    )
    row = _ended_row()
    old_epoch = "a" * 32
    row = row.model_copy(update={
        "responder_epoch": old_epoch,
        "responder_seq": 4,
        "responder_reservation_version": 7,
        "responder_reservations": {source.key: reservation},
        "turn_person": person,
        "turn_person_seq": 4,
        "turn_person_at": accepted_at,
        "turn_person_epoch": old_epoch,
        "turn_person_source_key": source.key,
    })
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(_WS(_Slot())),
    )

    out, _ = await reset_session(
        workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True,
    )

    assert out.responder_epoch != old_epoch
    assert out.responder_seq == 4
    assert out.responder_reservation_version == 8
    assert out.responder_reservations == {}
    assert out.turn_person is None
    assert out.turn_person_seq is None
    assert out.turn_person_at is None
    assert out.turn_person_epoch is None
    assert out.turn_person_source_key is None


@pytest.mark.asyncio
async def test_reopen_retry_does_not_rotate_or_bump_twice():
    original = _ended_row()
    storage_provider = _SP(original)
    registry = _Registry(_WS(_Slot()))
    deps = SessionResetDeps(
        storage_provider=storage_provider,
        workspace_registry=registry,
    )
    first, _ = await reset_session(
        workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True,
    )

    retry = await _reopen_ended_locked(
        row=original,
        workspace_id="ws-1",
        session_id="sess-1",
        sessions=storage_provider._s,
        workspace_registry=registry,
        by_a_person=True,
    )

    assert retry is None
    current = await storage_provider._s.get("sess-1")
    assert current.responder_epoch == first.responder_epoch
    assert current.responder_reservation_version == 1


class _MoveResponderBeforeClose(_Storage):
    def __init__(self, row):
        super().__init__(row)
        self.moved = False

    async def patch_if(self, sid, patch=None, *, where, set_paths=None, conn=None):
        if patch and patch.get("status") == SessionStatus.CREATED and not self.moved:
            self.moved = True
            self._row = self._row.model_copy(update={"responder_epoch": "b" * 32})
        return await super().patch_if(
            sid, patch, where=where, set_paths=set_paths, conn=conn,
        )


class _MoveOccurrenceBeforeClose(_Storage):
    def __init__(self, row, race):
        super().__init__(row)
        self.race = race
        self.moved = False

    async def patch_if(self, sid, patch=None, *, where, set_paths=None, conn=None):
        if patch and patch.get("status") == SessionStatus.CREATED and not self.moved:
            self.moved = True
            if self.race == "binding_epoch":
                updates = {"binding_epoch": self._row.binding_epoch + 1}
            elif self.race == "last_seq":
                updates = {"last_seq": self._row.last_seq + 1}
            else:
                updates = {
                    "cancel_requested": True,
                    "cancel_requested_at": datetime.now(timezone.utc),
                }
            self._row = self._row.model_copy(update=updates)
        return await super().patch_if(
            sid, patch, where=where, set_paths=set_paths, conn=conn,
        )


@pytest.mark.asyncio
async def test_reopen_refuses_a_moved_responder_occurrence_without_partial_clear():
    row = _ended_row().model_copy(update={
        "responder_epoch": "a" * 32,
        "responder_seq": 0,
        "responder_reservation_version": 1,
        "responder_reservations": {},
    })
    storage_provider = _SP(row)
    storage_provider._s = _MoveResponderBeforeClose(row)
    deps = SessionResetDeps(
        storage_provider=storage_provider,
        workspace_registry=_Registry(_WS(_Slot())),
    )

    with pytest.raises(ConflictError):
        await reset_session(
            workspace_id="ws-1",
            session_id="sess-1",
            deps=deps,
            human_intent=True,
        )

    current = await storage_provider._s.get("sess-1")
    assert current.status == SessionStatus.ENDED
    assert current.responder_epoch == "b" * 32
    assert current.responder_seq == 0
    assert current.responder_reservation_version == 1
    assert current.responder_reservations == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["binding_epoch", "last_seq", "cancel_requested"])
async def test_reopen_close_refuses_a_moved_session_occurrence_without_rotation(race):
    person = PrincipalRef(
        type="user", id="u-1", display="alice", role="user", source="local",
    )
    source = responder_source("user_input", {"occurrence": "first"})
    reservation = ResponderReservation(
        domain=source.domain,
        source_identity=source.identity,
        person=person,
        seq=1,
        accepted_at=datetime.now(timezone.utc),
    )
    epoch = "a" * 32
    row = _ended_row().model_copy(update={
        "responder_epoch": epoch,
        "responder_seq": 1,
        "responder_reservation_version": 3,
        "responder_reservations": {source.key: reservation},
        "turn_person": person,
    })
    storage_provider = _SP(row)
    storage_provider._s = _MoveOccurrenceBeforeClose(row, race)
    deps = SessionResetDeps(
        storage_provider=storage_provider,
        workspace_registry=_Registry(_WS(_Slot())),
    )

    with pytest.raises(ConflictError):
        await reset_session(
            workspace_id="ws-1",
            session_id="sess-1",
            deps=deps,
            human_intent=True,
        )

    current = await storage_provider._s.get("sess-1")
    assert current.status == SessionStatus.ENDED
    assert current.responder_epoch == epoch
    assert current.responder_seq == 1
    assert current.responder_reservation_version == 3
    assert current.responder_reservations == {source.key: reservation}
    assert current.turn_person == person
    if race == "binding_epoch":
        assert current.binding_epoch == 1
    elif race == "last_seq":
        assert current.last_seq == row.last_seq + 2
    else:
        assert current.cancel_requested is True
        assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_reset_reopens_a_session_ended_at_the_tool_turn_cap():
    """tool_turn_cap is restartable (an autonomous session's turn was stopped by max_tool_turns; the reopened
    invocation starts a fresh round count)."""
    row = _ended_row(reason="tool_turn_cap")
    slot = _Slot()
    ws = _WS(slot)
    deps = SessionResetDeps(storage_provider=_SP(row), workspace_registry=_Registry(ws))

    out, invocation = await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True)

    assert out.status == SessionStatus.CREATED and out.ended_reason is None
    assert slot.reopened is True and invocation == 2


@pytest.mark.asyncio
async def test_reset_clears_stale_interrupt_requested():
    row = _ended_row()
    row.interrupt_requested = True
    slot = _Slot()
    ws = _WS(slot)
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(ws),
    )
    out, _invocation = await reset_session(
        workspace_id="ws-1",
        session_id="sess-1",
        deps=deps,
        human_intent=True,
    )
    assert out.interrupt_requested is False


@pytest.mark.asyncio
async def test_reset_rejects_non_ended():
    row = _ended_row()
    row.status = SessionStatus.RUNNING
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(_WS(_Slot())),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True)


@pytest.mark.asyncio
async def test_reset_rejects_workspace_lost():
    """workspace_lost + the workspace row is ALSO gone: still permanent."""
    row = _ended_row(reason="workspace_lost")
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(_WS(_Slot())),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True)


@pytest.mark.asyncio
async def test_reset_rejects_workspace_lost_while_workspace_still_failed():
    """01a0533c: the workspace row exists but hasn't healed (phase !=
    running) - the probe blip's damage is still permanent for now."""
    row = _ended_row(reason="workspace_lost")
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(_WS(_Slot()), ws_row=_WSRow(phase="failed")),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True)


@pytest.mark.asyncio
async def test_reset_reopens_workspace_lost_once_workspace_healed():
    """01a0533c (live SEV): a probe-blip workspace_lost is NOT permanent
    once the workspace is running again - the reopen must succeed exactly
    like a normal completed/failed/cancelled restart."""
    row = _ended_row(reason="workspace_lost")
    slot = _Slot()
    ws = _WS(slot)
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(ws, ws_row=_WSRow(phase="running")),
    )
    out, invocation = await reset_session(
        workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True,
    )
    assert out.status == SessionStatus.CREATED
    assert out.ended_reason is None
    assert out.ended_at is None
    assert slot.reopened is True
    assert invocation == 2


@pytest.mark.asyncio
async def test_reset_rejects_force_deleted_even_when_workspace_healthy():
    """force_deleted is a deliberate operator action, not a probe
    artifact - it stays permanent unconditionally, even if a workspace
    row happens to exist and read phase=running (e.g. a same-id
    workspace re-created after the original was force-deleted)."""
    row = _ended_row(reason="force_deleted")
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(_WS(_Slot()), ws_row=_WSRow(phase="running")),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True)


@pytest.mark.asyncio
async def test_reset_clears_the_last_turn_error():
    """A reopened session is a new invocation: the failure of the turn before it no longer describes the row (C-024)."""
    from primer.model.workspace_session import LastTurnError

    row = _ended_row(reason="failed")
    row.last_turn_error = LastTurnError(code="server_error", at=datetime.now(timezone.utc))
    deps = SessionResetDeps(storage_provider=_SP(row), workspace_registry=_Registry(_WS(_Slot())))

    out, _invocation = await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps, human_intent=True)

    assert out.last_turn_error is None
