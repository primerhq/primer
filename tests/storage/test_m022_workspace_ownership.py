"""Migration 22: existing workspaces get an owner (access-control Phase 2, Task 9).

A Workspace row whose access block is absent or empty (no owners, no grants,
not public) is owned by the first admin (the admin User with the oldest
created_at), private; that includes the prime/legacy default workspace (id
``primer``, whose sessions and files are keyed by the id). A row that already
carries owners, grants or public is left alone, so re-running converges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from primer.model.access import Access
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m022_workspace_ownership import M022WorkspaceOwnership
from primer.storage.sqlite import SqliteStorageProvider

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _users(sp, *rows: tuple[str, str, int]) -> None:
    store = sp.get_storage(User)
    for uid, role, age_days in rows:
        await store.create(
            User(id=uid, username=uid, password_hash=None, role=role, created_at=_T0 + timedelta(days=age_days))
        )


def _workspace(workspace_id: str, access: Access | None = None) -> Workspace:
    ws = Workspace(
        id=workspace_id, template_id="local-default", provider_id="p",
        created_at=_T0, runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
    )
    return ws if access is None else ws.model_copy(update={"access": access})


async def _ws(sp, workspace_id: str, access: Access | None = None) -> None:
    await sp.get_storage(Workspace).create(_workspace(workspace_id, access))


async def test_registered_as_version_22():
    assert MIGRATIONS[21].version == 22 and isinstance(MIGRATIONS[21], M022WorkspaceOwnership)


async def test_unset_rows_go_to_the_oldest_admin_including_the_default_workspace(sp):
    await _users(sp, ("u-user", "user", 0), ("u-old-admin", "admin", 1), ("u-new-admin", "admin", 5))
    await _ws(sp, "primer")
    await _ws(sp, "ws-custom")

    await M022WorkspaceOwnership().apply(sp)

    for wid in ("primer", "ws-custom"):
        access = (await sp.get_storage(Workspace).get(wid)).access
        assert access.owners == ["u-old-admin"] and access.public is False, wid


async def test_a_row_that_already_has_access_is_left_alone(sp):
    await _users(sp, ("u-admin", "admin", 0))
    await _ws(sp, "mine", Access(owners=["u-other"]))
    await _ws(sp, "open", Access(public=True))

    await M022WorkspaceOwnership().apply(sp)

    assert (await sp.get_storage(Workspace).get("mine")).access.owners == ["u-other"]
    assert (await sp.get_storage(Workspace).get("open")).access == Access(public=True)


async def test_without_an_admin_rows_are_left_for_admins(sp):
    await _users(sp, ("u-user", "user", 0))
    await _ws(sp, "primer")

    await M022WorkspaceOwnership().apply(sp)

    assert (await sp.get_storage(Workspace).get("primer")).access == Access()


async def test_rerunning_changes_nothing(sp):
    await _users(sp, ("u-admin", "admin", 0))
    await _ws(sp, "primer")
    await _ws(sp, "ws-custom", Access(owners=["u-other"]))

    await M022WorkspaceOwnership().apply(sp)
    first = {w.id: w.access for w in (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=50))).items}
    await M022WorkspaceOwnership().apply(sp)
    second = {w.id: w.access for w in (await sp.get_storage(Workspace).list(OffsetPage(offset=0, length=50))).items}
    assert first == second


async def test_workspaces_go_to_the_oldest_enabled_admin(sp):
    """Final access review F17: a disabled admin, however old, is skipped (as migrations 25 to 27 skip one)."""
    await _users(sp, ("u-old-admin", "admin", 0), ("u-admin", "admin", 1))
    await sp.get_storage(User).update((await sp.get_storage(User).get("u-old-admin")).model_copy(update={"disabled": True}))
    await _ws(sp, "primer")

    await M022WorkspaceOwnership().apply(sp)

    assert (await sp.get_storage(Workspace).get("primer")).access.owners == ["u-admin"]
