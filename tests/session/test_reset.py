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


class _Registry:
    def __init__(self, ws):
        self._ws = ws

    async def get_workspace(self, wid):
        return self._ws


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
        storage_provider=_SP(row), workspace_registry=_Registry(ws),
    )
    out, invocation = await reset_session(
        workspace_id="ws-1", session_id="sess-1", deps=deps,
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
        storage_provider=_SP(row), workspace_registry=_Registry(ws),
    )
    out, _invocation = await reset_session(
        workspace_id="ws-1", session_id="sess-1", deps=deps,
    )
    assert out.interrupt_requested is False


@pytest.mark.asyncio
async def test_reset_rejects_non_ended():
    row = _ended_row()
    row.status = SessionStatus.RUNNING
    deps = SessionResetDeps(
        storage_provider=_SP(row), workspace_registry=_Registry(_WS(_Slot())),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps)


@pytest.mark.asyncio
async def test_reset_rejects_workspace_lost():
    row = _ended_row(reason="workspace_lost")
    deps = SessionResetDeps(
        storage_provider=_SP(row), workspace_registry=_Registry(_WS(_Slot())),
    )
    with pytest.raises(ConflictError):
        await reset_session(workspace_id="ws-1", session_id="sess-1", deps=deps)
