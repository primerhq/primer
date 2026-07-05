"""Unit tests for the ``restart_workspace_session`` internal tool.

Mirrors ``tests/api/test_session_restart.py``'s fakes (a workspace slot
with ``reopen``/``append_instruction`` for reset + wake, and
``append_message_line`` so the invocation divider round-trips through
``primer.session.reset.reset_session``) but drives the toolset handler
directly instead of the REST route.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.toolset.workspaces import build_workspaces_toolset


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


class _FakeSlot:
    def __init__(self) -> None:
        self.reopened = False
        self.instructions: list[str] = []

    async def reopen(self) -> None:
        self.reopened = True

    async def append_instruction(self, instruction: str) -> None:
        self.instructions.append(instruction)


class _FakeWorkspace:
    state_path = ".state"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.slots: dict[str, _FakeSlot] = {}

    async def get_session(self, session_id: str) -> _FakeSlot:
        return self.slots.setdefault(session_id, _FakeSlot())

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f"{self.state_path}/sessions/{session_id}/messages.jsonl"
        self._files[path] = self._files.get(path, b"") + line


class _Registry:
    def __init__(self, ws: _FakeWorkspace) -> None:
        self._ws = ws

    async def get_workspace(self, workspace_id: str) -> _FakeWorkspace:
        return self._ws


class _Storage:
    def __init__(self) -> None:
        self._data: dict = {}

    async def get(self, sid):
        return self._data.get(sid)

    async def create(self, row):
        self._data[row.id] = row
        return row

    async def update(self, row):
        self._data[row.id] = row
        return row


class _SP:
    def __init__(self) -> None:
        self._s = _Storage()

    def get_storage(self, cls):
        return self._s


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    def __init__(self) -> None:
        self.upserts: list[tuple] = []

    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        self.upserts.append((kind, entity_id))

    async def delete_lease(self, kind, entity_id):
        pass


def _seed_session(sp: _SP, *, sid: str, wid: str, status, ended_reason=None):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        WorkspaceSession,
    )

    sess = WorkspaceSession(
        id=sid,
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status,
        ended_reason=ended_reason,
        ended_at=_now() if ended_reason is not None else None,
        last_seq=3,
        turn_status="idle",
        created_at=_now(),
    )
    sp._s._data[sid] = sess
    return sess


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def ws() -> _FakeWorkspace:
    return _FakeWorkspace()


@pytest.fixture
def registry(ws) -> _Registry:
    return _Registry(ws)


@pytest.fixture
def session_toolset(sp, registry):
    return build_workspaces_toolset(
        storage_provider=sp,
        workspace_registry=registry,
        scheduler=_FakeScheduler(),
        claim_engine=_FakeClaimEngine(),
        event_bus=None,
    )


class TestRestartWorkspaceSession:
    @pytest.mark.asyncio
    async def test_restart_tool_reopens_ended_session(
        self, session_toolset, sp
    ) -> None:
        from primer.model.workspace_session import SessionStatus

        _seed_session(
            sp,
            sid="sess-1",
            wid="ws-1",
            status=SessionStatus.ENDED,
            ended_reason="completed",
        )
        result = await session_toolset.call(
            tool_name="restart_workspace_session",
            arguments={
                "workspace_id": "ws-1",
                "session_id": "sess-1",
                "input": "restarted by tool",
            },
        )
        assert not result.is_error, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "running"
        assert payload["turn_status"] == "claimable"
        assert payload["metadata"]["invocation"] == 2

    @pytest.mark.asyncio
    async def test_restart_active_session_is_conflict(
        self, session_toolset, sp
    ) -> None:
        from primer.model.workspace_session import SessionStatus

        _seed_session(
            sp, sid="sess-1", wid="ws-1", status=SessionStatus.RUNNING,
        )
        result = await session_toolset.call(
            tool_name="restart_workspace_session",
            arguments={"workspace_id": "ws-1", "session_id": "sess-1"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_restart_missing_session_is_not_found(
        self, session_toolset
    ) -> None:
        result = await session_toolset.call(
            tool_name="restart_workspace_session",
            arguments={"workspace_id": "ws-1", "session_id": "nope"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_restart_without_scheduler_is_unavailable(
        self, sp, registry
    ) -> None:
        ts = build_workspaces_toolset(
            storage_provider=sp,
            workspace_registry=registry,
            scheduler=None,
            claim_engine=None,
            event_bus=None,
        )
        result = await ts.call(
            tool_name="restart_workspace_session",
            arguments={"workspace_id": "ws-1", "session_id": "sess-1"},
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "unavailable"

    @pytest.mark.asyncio
    async def test_restart_is_registered_in_catalog(self, session_toolset) -> None:
        ids = [t.id async for t in session_toolset.list_tools()]
        assert "restart_workspace_session" in ids
