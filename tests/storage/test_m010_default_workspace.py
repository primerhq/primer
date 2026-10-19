"""Migration 10: record an existing install's default workspace and name it prime (pivot plan P2 task 5).

An install that predates ``SystemState.default_workspace_id`` keeps the id its default workspace has always had
(``primer``): the id is only recorded, never moved (that workspace's sessions, files and Kubernetes objects are keyed
by the id). This versioned step does, for an upgraded database, what the ensure pass does for a database whose
migrations were baselined (``auto_bootstrap: false``): ``primer.bootstrap.prime.adopt_legacy_default_workspace``. On
a fresh install it changes nothing: the runner baselines a fresh database straight to the latest version, and the
ensure pass has already created and recorded ``prime``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "m010.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _legacy_row(name: str | None = "primer") -> Workspace:
    """A pre-upgrade default workspace: the id ``primer`` is the id every install created before the field carried it."""
    return Workspace(
        id="primer",
        name=name,
        template_id="local-default",
        provider_id="local",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
    )


def _migration():
    from primer.storage.migrations.m010_default_workspace import M010DefaultWorkspace

    return M010DefaultWorkspace()


# ---- what it records and renames -----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_and_renames_the_legacy_workspace(sp: StorageProvider) -> None:
    """An existing install keeps its id; the row is named ``prime`` for display and the id is recorded."""
    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_legacy_row())

    await _migration().apply(sp)

    assert (await sp.get_system_state()).default_workspace_id == "primer"
    row = await workspaces.get("primer")
    assert row is not None and row.name == "prime"


@pytest.mark.asyncio
async def test_without_a_legacy_row_it_records_nothing(sp: StorageProvider) -> None:
    """A fresh install's database (no ``primer`` row, nothing recorded): there is nothing to adopt, and nothing is invented."""
    await _migration().apply(sp)

    assert (await sp.get_system_state()).default_workspace_id is None
    page = await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=10))
    assert len(page.items) == 0


@pytest.mark.asyncio
async def test_a_recorded_default_is_never_adopted_away(sp: StorageProvider) -> None:
    """An id already recorded wins whatever else exists: a ``primer`` row beside it is an ordinary workspace, not renamed."""
    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_legacy_row())
    await sp.set_default_workspace_id("prime")

    await _migration().apply(sp)

    assert (await sp.get_system_state()).default_workspace_id == "prime"
    row = await workspaces.get("primer")
    assert row is not None and row.name == "primer"


# ---- it runs once -------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_is_idempotent_and_quiet_once_recorded(sp: StorageProvider, caplog: pytest.LogCaptureFixture) -> None:
    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_legacy_row())

    with caplog.at_level(logging.DEBUG, logger="primer"):
        await _migration().apply(sp)
    first = [r for r in caplog.records if r.name.startswith("primer.") and r.levelno >= logging.INFO]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="primer"):
        await _migration().apply(sp)
    second = [r for r in caplog.records if r.name.startswith("primer.") and r.levelno >= logging.INFO]

    assert (await workspaces.get("primer")).name == "prime"
    assert (await sp.get_system_state()).default_workspace_id == "primer"
    assert len(first) >= 1 and second == [], "a second run says nothing"


@pytest.mark.asyncio
async def test_an_existing_install_runs_it_once(sp: StorageProvider) -> None:
    """The versioned step runs when the database catches up, and a rename a person makes afterwards is never undone."""
    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_legacy_row())
    await sp.set_schema_version(9)

    from primer.storage.migrations import LATEST_VERSION, run_migrations

    version = await run_migrations(sp, is_fresh_install=False)
    assert version == LATEST_VERSION
    assert (await workspaces.get("primer")).name == "prime"
    assert (await sp.get_system_state()).default_workspace_id == "primer"

    row = await workspaces.get("primer")
    await workspaces.update(row.model_copy(update={"name": "home"}))
    await run_migrations(sp, is_fresh_install=False)  # nothing newer than the stored version; the migration must not re-run
    assert (await workspaces.get("primer")).name == "home"


# ---- registration --------------------------------------------------------------------------------------------------------------------------------------------------


def test_is_registered_as_version_10() -> None:
    """Contiguous with m009. (The latest version is whatever the registry's last migration says; this pins index 9, not the end.)"""
    from primer.storage.migrations import LATEST_VERSION, MIGRATIONS
    from primer.storage.migrations.m010_default_workspace import M010DefaultWorkspace

    assert isinstance(MIGRATIONS[9], M010DefaultWorkspace)
    assert MIGRATIONS[9].version == 10 and LATEST_VERSION >= 10
    assert MIGRATIONS[9].description == "Record the default workspace id and name it prime"


@pytest.mark.asyncio
async def test_the_runner_applies_it_to_a_database_at_version_nine(sp: StorageProvider) -> None:
    from primer.storage.migrations import LATEST_VERSION, run_migrations

    workspaces = sp.get_storage(Workspace)
    await workspaces.create(_legacy_row())
    await sp.set_schema_version(9)

    version = await run_migrations(sp, is_fresh_install=False)

    assert version == LATEST_VERSION and (await sp.get_system_state()).schema_version == LATEST_VERSION
    assert (await sp.get_system_state()).default_workspace_id == "primer"
    assert (await workspaces.get("primer")).name == "prime"
