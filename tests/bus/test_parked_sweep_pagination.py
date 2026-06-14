"""hotpaths #4: parked-session sweeps page through ALL rows (beyond 200).

The timer scheduler + timeout sweeper previously read only the first 200
parked sessions, silently leaving every park past the cap stuck. Seed 250
due parked sessions and assert all 250 keys are returned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from primer.bus.scheduler_tasks import (
    _find_due_timer_keys,
    _find_expired_non_timer_keys,
)
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.storage.sqlite import SqliteStorageProvider


def _parked(session_id: str, event_key: str, parked_until: datetime):
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    sess.parked_status = "parked"
    sess.parked_event_key = event_key
    sess.parked_until = parked_until
    sess.parked_at = datetime.now(timezone.utc)
    return sess


@pytest.mark.asyncio
async def test_timer_sweep_returns_all_beyond_200(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    n = 250
    for i in range(n):
        await storage.create(_parked(f"sess-{i}", f"timer:s{i}", past))

    keys = await _find_due_timer_keys(storage)
    assert len(keys) == n
    assert set(keys) == {f"timer:s{i}" for i in range(n)}
    await provider.aclose()


@pytest.mark.asyncio
async def test_timeout_sweep_returns_all_beyond_200(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    n = 250
    for i in range(n):
        await storage.create(_parked(f"sess-{i}", f"ask_user:s{i}", past))

    keys = await _find_expired_non_timer_keys(storage)
    assert len(keys) == n
    assert set(keys) == {f"ask_user:s{i}" for i in range(n)}
    await provider.aclose()
