# tests/session/test_reset.py
from datetime import datetime, timezone

import pytest

from primer.model.except_ import ConflictError
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.reset import SessionResetDeps, reset_session


class _Storage:
    def __init__(self, row):
        self._row = row

    async def get(self, sid):
        return self._row if self._row and self._row.id == sid else None

    async def update(self, row):
        self._row = row
        return row


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
    row = _ended_row()
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
    )
    assert out.status == SessionStatus.CREATED
    assert out.ended_reason is None
    assert out.ended_at is None
    assert out.turn_status == "idle"
    assert slot.reopened is True
    assert invocation == 2
    assert len(ws.lines) == 1
    assert SessionMessageKind.INVOCATION_DIVIDER.value in ws.lines[0].decode()


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
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps)


@pytest.mark.asyncio
async def test_reset_rejects_workspace_lost():
    """workspace_lost + the workspace row is ALSO gone: still permanent."""
    row = _ended_row(reason="workspace_lost")
    deps = SessionResetDeps(
        storage_provider=_SP(row),
        workspace_registry=_Registry(_WS(_Slot())),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps)


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
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps)


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
        workspace_id="ws-1", session_id="sess-1", deps=deps,
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
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps)
