"""Migration 24: external tool calls and approval records record the workspace of their session (access-control Phase 2, Task 5).

``workspace_id`` is read off the row's session; a row with no session, or whose session is gone, stays without one; a row that already has one is
left alone; re-running converges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.model.external_tool import ExternalToolCall
from primer.model.provider import SqliteConfig
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m024_cross_workspace_list_field import M024CrossWorkspaceListField
from primer.storage.sqlite import SqliteStorageProvider

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _seed(sp) -> None:
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag"), status=SessionStatus.WAITING, created_at=_NOW,
    ))
    calls, records = sp.get_storage(ExternalToolCall), sp.get_storage(ToolApprovalRecord)
    await calls.create(ExternalToolCall(id="c-old", session_id="s1", tool_call_id="t1", tool_name="x", created_at=_NOW))
    await calls.create(ExternalToolCall(id="c-gone", session_id="s-gone", tool_call_id="t2", tool_name="x", created_at=_NOW))
    await calls.create(ExternalToolCall(id="c-chat", chat_id="ch", tool_call_id="t3", tool_name="x", created_at=_NOW))
    await calls.create(ExternalToolCall(id="c-set", session_id="s1", workspace_id="ws-keep", tool_call_id="t4", tool_name="x", created_at=_NOW))
    await records.create(ToolApprovalRecord(id="r-old", tool_name="x", session_id="s1", decided_at=_NOW, decision="approved"))
    await records.create(ToolApprovalRecord(id="r-chat", tool_name="x", chat_id="ch", decided_at=_NOW, decision="approved"))


async def _workspaces(sp) -> dict[str, str | None]:
    calls, records = sp.get_storage(ExternalToolCall), sp.get_storage(ToolApprovalRecord)
    out = {c: (await calls.get(c)).workspace_id for c in ("c-old", "c-gone", "c-chat", "c-set")}
    out.update({r: (await records.get(r)).workspace_id for r in ("r-old", "r-chat")})
    return out


async def test_the_migration_backfills_the_workspace_from_the_session(sp) -> None:
    await _seed(sp)

    await M024CrossWorkspaceListField().apply(sp)

    assert await _workspaces(sp) == {
        "c-old": "ws-1", "c-gone": None, "c-chat": None, "c-set": "ws-keep", "r-old": "ws-1", "r-chat": None,
    }


async def test_the_migration_is_idempotent(sp) -> None:
    await _seed(sp)
    await M024CrossWorkspaceListField().apply(sp)
    once = await _workspaces(sp)

    await M024CrossWorkspaceListField().apply(sp)

    assert await _workspaces(sp) == once


def test_the_migration_is_registered_after_23() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions[versions.index(23):versions.index(24) + 1] == [23, 24]
