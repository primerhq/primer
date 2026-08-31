"""Tests for primer.workspace.session_reconcile.reconcile_sessions_to_workspace_lost.

No prior test file exercised this function at all. Adding coverage now
because 01a04d91-a7a0 changed its behavior: a session reconciled to
ENDED/workspace_lost must also have turn_status/turn_started_at reset,
since a workspace confirmed permanently unreachable is exactly the crash
scenario those fields exist to catch (the worker that was mid-turn on it
is gone and never reached its own cleanup).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.workspace.session_reconcile import reconcile_sessions_to_workspace_lost


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def fake_storage_provider():
    from tests.conftest import _FakeStorageProvider
    return _FakeStorageProvider()


@pytest.mark.asyncio
async def test_reconcile_clears_stale_running_turn_status(
    fake_storage_provider,
) -> None:
    """A session stuck at turn_status='running' (its worker died along
    with the workspace, so run_one_session_turn's own finally/except
    cleanup never ran) must be reset to idle by reconciliation, not left
    ENDED with a permanently-stuck 'running' turn_status."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    started = _now()
    await storage.create(WorkspaceSession(
        id="s-lost",
        workspace_id="w-gone",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=started,
        turn_status="running",
        turn_started_at=started,
    ))

    reconciled = await reconcile_sessions_to_workspace_lost(
        fake_storage_provider, "w-gone",
    )

    assert reconciled == 1
    row = await storage.get("s-lost")
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "workspace_lost"
    assert row.turn_status == "idle"
    assert row.turn_started_at is None


@pytest.mark.asyncio
async def test_reconcile_skips_already_ended_sessions(
    fake_storage_provider,
) -> None:
    """An already-ENDED session on the lost workspace is left untouched -
    reconciliation must not overwrite a real ended_reason/turn_status a
    prior clean exit already wrote."""
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(WorkspaceSession(
        id="s-done",
        workspace_id="w-gone",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.ENDED,
        ended_reason="completed",
        created_at=_now(),
        turn_status="idle",
    ))

    reconciled = await reconcile_sessions_to_workspace_lost(
        fake_storage_provider, "w-gone",
    )

    assert reconciled == 0
    row = await storage.get("s-done")
    assert row.ended_reason == "completed"
