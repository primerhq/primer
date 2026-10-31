"""Migration 23: existing collections get an owner; ``system`` becomes public (access-control Phase 3, Task 3).

A Collection row whose access block is absent or empty (no owners, no grants, not public) is owned by the first admin
(the enabled admin User with the oldest created_at), private; ``system`` becomes public whatever else it carries; an
``_internal_*`` collection stays unset (admin-only). A row that already carries owners, grants or public is left alone,
so re-running converges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from primer.model.access import Access
from primer.model.collection import Collection
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m023_collection_ownership import M023CollectionOwnership
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


async def _users(sp, *rows: tuple[str, str, int, bool]) -> None:
    store = sp.get_storage(User)
    for uid, role, age_days, disabled in rows:
        await store.create(
            User(
                id=uid, username=uid, password_hash=None, role=role, disabled=disabled,
                created_at=_T0 + timedelta(days=age_days),
            )
        )


async def _coll(sp, cid: str, access: Access | None = None, *, system: bool = False) -> None:
    row = Collection(id=cid, description="d", system=system)
    await sp.get_storage(Collection).create(row if access is None else row.model_copy(update={"access": access}))


async def _access(sp, cid: str) -> Access:
    return (await sp.get_storage(Collection).get(cid)).access


async def test_registered_as_version_23():
    assert MIGRATIONS[22].version == 23 and isinstance(MIGRATIONS[22], M023CollectionOwnership)


async def test_unset_rows_go_to_the_oldest_enabled_admin(sp):
    await _users(
        sp, ("u-user", "user", 0, False), ("u-gone-admin", "admin", 1, True),
        ("u-old-admin", "admin", 2, False), ("u-new-admin", "admin", 5, False),
    )
    await _coll(sp, "kb-1")
    await _coll(sp, "kb-2")

    await M023CollectionOwnership().apply(sp)

    for cid in ("kb-1", "kb-2"):
        access = await _access(sp, cid)
        assert access.owners == ["u-old-admin"] and access.public is False and access.members == ["u:u-old-admin"], cid


async def test_system_becomes_public_and_internal_collections_stay_admin_only(sp):
    await _users(sp, ("u-admin", "admin", 0, False))
    await _coll(sp, "system", system=True)
    await _coll(sp, "_internal_agents", system=True)
    await _coll(sp, "_internal_tools", system=True)

    await M023CollectionOwnership().apply(sp)

    system = await _access(sp, "system")
    assert system.public is True and system.owners == [] and system.members == ["public"]
    for cid in ("_internal_agents", "_internal_tools"):
        assert await _access(sp, cid) == Access(), cid


async def test_a_system_row_with_other_sharing_only_gains_public(sp):
    await _users(sp, ("u-admin", "admin", 0, False))
    await _coll(sp, "system", Access(owners=["u-admin"], grants={"u-x": "read"}), system=True)

    await M023CollectionOwnership().apply(sp)

    system = await _access(sp, "system")
    assert system.public is True and system.owners == ["u-admin"] and system.grants == {"u-x": "read"}
    assert system.members == ["public", "u:u-admin", "u:u-x"]


async def test_a_row_that_already_has_access_is_left_alone(sp):
    await _users(sp, ("u-admin", "admin", 0, False))
    await _coll(sp, "mine", Access(owners=["u-other"]))
    await _coll(sp, "open", Access(public=True))
    await _coll(sp, "shared", Access(grants={"u-other": "full"}))

    await M023CollectionOwnership().apply(sp)

    assert (await _access(sp, "mine")).owners == ["u-other"]
    assert await _access(sp, "open") == Access(public=True)
    assert await _access(sp, "shared") == Access(grants={"u-other": "full"})


async def test_without_an_enabled_admin_user_rows_are_left_for_admins(sp):
    await _users(sp, ("u-user", "user", 0, False), ("u-gone-admin", "admin", 1, True))
    await _coll(sp, "kb")
    await _coll(sp, "system", system=True)

    await M023CollectionOwnership().apply(sp)

    assert await _access(sp, "kb") == Access()
    assert (await _access(sp, "system")).public is True  # needs no admin


async def test_an_empty_install_is_a_no_op(sp):
    await M023CollectionOwnership().apply(sp)
    assert (await sp.get_storage(Collection).list(OffsetPage(offset=0, length=50))).items == []


async def test_rerunning_changes_nothing(sp):
    await _users(sp, ("u-admin", "admin", 0, False))
    await _coll(sp, "kb")
    await _coll(sp, "system", system=True)
    await _coll(sp, "_internal_graphs", system=True)

    await M023CollectionOwnership().apply(sp)
    first = {c.id: c.access for c in (await sp.get_storage(Collection).list(OffsetPage(offset=0, length=50))).items}
    await M023CollectionOwnership().apply(sp)
    second = {c.id: c.access for c in (await sp.get_storage(Collection).list(OffsetPage(offset=0, length=50))).items}
    assert first == second
