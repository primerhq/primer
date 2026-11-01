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


# -- golden characterization (PERF-T16) ---------------------------------------------------------------------------------------------------

from primer.storage.migrations._rows import read_all  # noqa: E402
from primer.storage.migrations.m024_cross_workspace_list_field import (  # noqa: E402
    ExternalToolCall as CallView,
    ToolApprovalRecord as RecordView,
    WorkspaceSession as SessionView,
)
from tests.storage._migration_golden import assert_golden, insert_raw  # noqa: E402


async def _old_apply(sp) -> None:
    """Migration 24 as it was before PERF-T16, frozen: read every row, ``update`` each one that needs a workspace."""
    sessions = sp.get_storage(SessionView)
    workspace_of: dict[str, str | None] = {}
    for model in (CallView, RecordView):
        store = sp.get_storage(model)
        for row in await read_all(store):
            if row.workspace_id is not None or row.session_id is None:
                continue
            if row.session_id not in workspace_of:
                session = await sessions.get(row.session_id)
                workspace_of[row.session_id] = session.workspace_id if session is not None else None
            workspace_id = workspace_of[row.session_id]
            if workspace_id is None:
                continue
            await store.update(row.model_copy(update={"workspace_id": workspace_id}))


async def _golden_seed(sp) -> None:
    await insert_raw(sp, SessionView, {
        "s1": {"workspace_id": "ws-1", "status": "waiting"},
        "s2": {"workspace_id": "ws-2"},
        "s-null": {"workspace_id": None},
        "s-nokey": {"status": "waiting"},
    })
    await insert_raw(sp, CallView, {
        "c01": {"tool_name": "x", "session_id": "s1", "n": 1.5, "nested": {"a": [1, None, {"b": False}]}},
        "c02": {"session_id": "s1", "workspace_id": None, "tool_name": "x"},
        "c03": {"session_id": "s2", "workspace_id": "ws-keep"},
        "c04": {"session_id": None, "tool_name": "x"},
        "c05": {"chat_id": "ch", "tool_name": "x"},
        "c06": {"session_id": "s-gone"},
        "c07": {"session_id": "s-null"},
        "c08": {"session_id": "s-nokey"},
        "c09": {"session_id": ""},
        "c10": {"big": 12345678901234567890, "flag": True, "text": "naïve ✓", "session_id": "s2", "e": 1e-7},
        "c11": {"session_id": "s1", "workspace_id": ""},
        "c12": {},
    })
    await insert_raw(sp, RecordView, {
        "r1": {"session_id": "s1", "decision": "approved"},
        "r2": {"session_id": "s2", "workspace_id": None},
        "r3": {"chat_id": "ch"},
        "r4": {"session_id": "s1", "workspace_id": "ws-x"},
        "r5": {"session_id": "s-gone", "workspace_id": None},
    })


async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path) -> None:
    await assert_golden(tmp_path, _golden_seed, (CallView, RecordView, SessionView), _old_apply, M024CrossWorkspaceListField().apply)
