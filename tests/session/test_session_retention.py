"""T104 (GR-6, D16): the per-session cleanup is one helper; destroying a workspace deletes its sessions; an optional, default-off sweep deletes ENDED
sessions past `session_retention_days` in bounded batches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.api.config import AppConfig
from primer.model.canvas import SessionCanvas
from primer.model.provider import SqliteConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider

NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


async def _session(sp, sid: str, *, status=SessionStatus.ENDED, ended_days_ago: float | None = 40, workspace="ws-1"):
    row = WorkspaceSession(
        id=sid, workspace_id=workspace, binding=AgentSessionBinding(agent_id="agent-a"), status=status, created_at=NOW - timedelta(days=60),
        ended_at=None if ended_days_ago is None else NOW - timedelta(days=ended_days_ago),
    )
    await sp.get_storage(WorkspaceSession).create(row)
    await sp.get_storage(SessionCanvas).create(SessionCanvas(id=sid, workspace_id=workspace))


async def _ids(sp) -> set[str]:
    from primer.model.storage import OffsetPage

    return {s.id for s in (await sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=200))).items}


def test_the_config_knob_exists_and_is_off_by_default():
    assert AppConfig.model_fields["session_retention_days"].default is None
    assert AppConfig(session_retention_days=14).session_retention_days == 14
    with pytest.raises(ValueError):
        AppConfig(session_retention_days=0)


async def test_the_helper_removes_the_session_and_what_hangs_off_it(sp):
    from primer.session.purge import delete_session_rows

    await _session(sp, "s-1")
    await delete_session_rows(sp, "s-1")
    assert await _ids(sp) == set()
    assert await sp.get_storage(SessionCanvas).get("s-1") is None


async def test_the_sweep_deletes_only_ended_sessions_past_the_window(sp):
    from primer.session.purge import sweep_ended_sessions

    await _session(sp, "old", ended_days_ago=40)
    await _session(sp, "recent", ended_days_ago=3)
    await _session(sp, "waiting", status=SessionStatus.WAITING, ended_days_ago=None)
    await _session(sp, "no-end-stamp", ended_days_ago=None)
    assert await sweep_ended_sessions(sp, retention_days=30, batch=50, now=NOW) == 1
    assert await _ids(sp) == {"recent", "waiting", "no-end-stamp"}


async def test_the_sweep_is_bounded_per_tick_and_the_knob_unset_deletes_nothing(sp):
    from primer.session.purge import sweep_ended_sessions

    for i in range(5):
        await _session(sp, f"old-{i}", ended_days_ago=40 + i)
    assert await sweep_ended_sessions(sp, retention_days=None, batch=2, now=NOW) == 0
    assert len(await _ids(sp)) == 5
    assert await sweep_ended_sessions(sp, retention_days=30, batch=2, now=NOW) == 2
    assert len(await _ids(sp)) == 3
    assert await sweep_ended_sessions(sp, retention_days=30, batch=2, now=NOW) == 2
    assert await sweep_ended_sessions(sp, retention_days=30, batch=2, now=NOW) == 1
    assert await _ids(sp) == set()


async def test_the_sweeper_is_a_leader_task_that_ticks_the_sweep(sp):
    from primer.bus.scheduler_tasks import SessionRetentionSweeper
    from primer.int.coordinator import ROLE_SESSION_RETENTION

    await _session(sp, "old", ended_days_ago=40)
    sweeper = SessionRetentionSweeper(storage_provider=sp, retention_days=30, batch=10)
    assert sweeper.role == ROLE_SESSION_RETENTION
    assert await sweeper.sweep_once(now=NOW) == 1


async def test_destroying_a_workspace_deletes_its_sessions_after_ending_them(sp, monkeypatch):
    from primer.workspace import session_reconcile
    from primer.session.purge import purge_workspace_sessions

    await _session(sp, "a", status=SessionStatus.WAITING, ended_days_ago=None, workspace="ws-gone")
    await _session(sp, "b", ended_days_ago=1, workspace="ws-gone")
    await _session(sp, "other", workspace="ws-keep")
    assert await purge_workspace_sessions(sp, "ws-gone") == 2
    assert await _ids(sp) == {"other"}
    assert await sp.get_storage(SessionCanvas).get("a") is None and await sp.get_storage(SessionCanvas).get("other") is not None


async def test_destroy_ends_the_sessions_then_deletes_them_and_the_row(sp, monkeypatch):
    """The registry's destroy: reconcile (ends open sessions as ``workspace_lost``, as before), then delete the workspace's sessions, then the row."""
    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.model.workspace import (
        LocalWorkspaceConfig, Workspace, WorkspaceProvider, WorkspaceProviderType, WorkspaceRuntimeMeta,
    )
    from primer.workspace import session_reconcile

    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(id="local-1", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-t104"))
    )
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-gone", template_id="t", provider_id="local-1", created_at=NOW, runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"),
    ))
    await _session(sp, "open", status=SessionStatus.WAITING, ended_days_ago=None, workspace="ws-gone")
    await _session(sp, "done", workspace="ws-gone")

    class _Backend:
        async def initialize(self) -> None: ...
        async def destroy(self, workspace_id: str) -> None: ...
        async def aclose(self) -> None: ...

    reconciled: list[str] = []
    real = session_reconcile.reconcile_sessions_to_workspace_lost

    async def spy(storage_provider, workspace_id):
        out = await real(storage_provider, workspace_id)
        row = await storage_provider.get_storage(WorkspaceSession).get("open")
        reconciled.append(f"{row.status.value}:{row.ended_reason}")
        return out

    monkeypatch.setattr("primer.api.registries.workspace_registry.reconcile_sessions_to_workspace_lost", spy)
    registry = WorkspaceRegistry(sp, factory=lambda provider: _Backend())
    await registry.destroy("ws-gone")

    assert reconciled == ["ended:workspace_lost"]              # ended with the same reason as before ...
    assert await _ids(sp) == set()                             # ... then deleted with the workspace
    assert await sp.get_storage(Workspace).get("ws-gone") is None
