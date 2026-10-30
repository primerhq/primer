"""An edit never undoes a sharing change made between its read and its write (security review finding 5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.access.person import Person
from primer.access.service import AccessUpdate, set_access, update_keeping_access
from primer.model.access import Access
from primer.model.common import Identifiable
from primer.model.except_ import ConflictError
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider


class Gizmo(Identifiable):
    name: str = "g"
    access: Access = Access()


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        for uid in ("u1", "u2"):
            await provider.get_storage(User).create(
                User(id=uid, username=uid, password_hash=None, created_at=datetime.now(UTC), role="user")
            )
        await provider.get_storage(Gizmo).create(Gizmo(id="g1", access=Access(owners=["u1"], grants={"u2": "full"})))
        yield provider
    finally:
        await provider.aclose()


async def test_an_edit_keeps_the_stored_sharing_and_writes_the_rest(sp):
    store = sp.get_storage(Gizmo)
    existing = await store.get("g1")
    edited = existing.model_copy(update={"name": "renamed", "access": Access(owners=["u2"], public=True)})
    written = await update_keeping_access(store, edited, existing, what="Gizmo 'g1'")
    assert written.name == "renamed"
    assert written.access == existing.access


async def test_an_edit_after_a_revocation_is_a_conflict_and_writes_nothing(sp):
    store = sp.get_storage(Gizmo)
    users = sp.get_storage(User)
    stale = await store.get("g1")  # the full grantee reads the row ...
    await set_access(store, users, "g1", Person("u1", "user"), AccessUpdate(owners=["u1"]), what="Gizmo 'g1'")  # ... the owner revokes
    with pytest.raises(ConflictError):
        await update_keeping_access(store, stale.model_copy(update={"name": "late"}), stale, what="Gizmo 'g1'")
    now = await store.get("g1")
    assert now.name == "g" and now.access.grants == {}
