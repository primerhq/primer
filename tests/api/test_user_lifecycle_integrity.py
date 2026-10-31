"""T111 (GS-16, GS-19, GS-20): deleting a user frees their SSO identity, concurrent creates of one username
cannot both win, and two concurrent admin changes cannot leave the install with no usable admin."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from tests.api.conftest import raw_client as _raw, app, fake_provider_registry  # noqa: F401

from primer.api.routers import admin_users
from primer.api.routers.sso import _link_identity, _resolve_or_provision_user
from primer.model.except_ import ConflictError
from primer.model.oidc import UserIdentity
from primer.model.providers.storage import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

NOW = datetime.now(timezone.utc)


async def _provider(tmp_path, name="u.sqlite"):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / name)))
    await sp.initialize()
    return sp


# --- GS-16 -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_identity_whose_user_is_gone_is_treated_as_absent(tmp_path):
    sp = await _provider(tmp_path)
    await sp.set_sso_jit_enabled(True)
    users, idents = sp.get_storage(User), sp.get_storage(UserIdentity)
    await idents.create(UserIdentity(user_id="user-gone", provider_id="okta", subject="sub-c", created_at=NOW))
    user = await _resolve_or_provision_user(storage_provider=sp, user_storage=users, identity_storage=idents,
                                            provider_id="okta", subject="sub-c", email=None)
    assert user.id != "user-gone"
    rows = (await idents.list(OffsetPage(offset=0, length=10))).items
    assert [(r.subject, r.user_id) for r in rows] == [("sub-c", user.id)]       # the orphan is replaced, not kept
    await sp.aclose()


@pytest.mark.asyncio
async def test_a_stale_identity_does_not_block_linking_to_a_new_account(tmp_path):
    sp = await _provider(tmp_path)
    users, idents = sp.get_storage(User), sp.get_storage(UserIdentity)
    await idents.create(UserIdentity(user_id="user-gone", provider_id="okta", subject="sub-c", created_at=NOW))
    await users.create(User(id="user-new", username="carol2", created_at=NOW, role="user"))
    linked = await _link_identity(identity_storage=idents, target_user_id="user-new", provider_id="okta",
                                  subject="sub-c", email=None, user_storage=users)
    assert linked.user_id == "user-new"
    await sp.aclose()


@pytest.mark.asyncio
async def test_admin_delete_removes_the_users_identities(client, app):
    made = await client.post("/v1/admin/users", json={"username": "dora", "password": "dorapassword1", "role": "user"})
    uid = made.json()["id"]
    idents = app.state.storage_provider.get_storage(UserIdentity)
    await idents.create(UserIdentity(user_id=uid, provider_id="okta", subject="sub-d", created_at=NOW))
    other = await idents.create(UserIdentity(user_id="user-else", provider_id="okta", subject="sub-e", created_at=NOW))
    assert (await client.delete(f"/v1/admin/users/{uid}")).status_code == 204
    left = (await idents.list(OffsetPage(offset=0, length=10))).items
    assert [r.id for r in left] == [other.id]


@pytest.mark.asyncio
async def test_live_identity_still_resolves_to_its_user(tmp_path):
    sp = await _provider(tmp_path)
    users, idents = sp.get_storage(User), sp.get_storage(UserIdentity)
    await users.create(User(id="user-live", username="live", created_at=NOW, role="user"))
    await idents.create(UserIdentity(user_id="user-live", provider_id="okta", subject="s", created_at=NOW))
    got = await _resolve_or_provision_user(storage_provider=sp, user_storage=users, identity_storage=idents,
                                           provider_id="okta", subject="s", email=None)
    assert got.id == "user-live"
    await sp.aclose()


# --- GS-20 -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_concurrent_creates_of_one_username_answer_201_and_409(client, app):
    body = {"username": "erin", "password": "erinpassword1", "role": "user"}
    first, second = await asyncio.gather(client.post("/v1/admin/users", json=body), client.post("/v1/admin/users", json=body))
    assert sorted([first.status_code, second.status_code]) == [201, 409], (first.text, second.text)
    rows = (await app.state.storage_provider.get_storage(User).list(OffsetPage(offset=0, length=50))).items
    assert [u.username for u in rows].count("erin") == 1


@pytest.mark.asyncio
async def test_the_username_index_rejects_a_duplicate_but_tolerates_existing_ones(tmp_path, caplog):
    sp = await _provider(tmp_path)
    users = sp.get_storage(User)
    await users.create(User(id="user-1", username="dup", created_at=NOW, role="user"))
    with pytest.raises(ConflictError):
        await users.create(User(id="user-2", username="dup", created_at=NOW, role="user"))
    await sp.aclose()
    # A database from before the index: two rows already share a name. Boot must go on, log it, and leave them.
    path = tmp_path / "old.sqlite"
    sp = await _provider(tmp_path, "old.sqlite")
    users = sp.get_storage(User)
    await users.create(User(id="user-1", username="dup", created_at=NOW, role="user"))
    await sp.aclose()
    with sqlite3.connect(path) as raw:
        raw.execute('DROP INDEX "user_username_uniq"')
        raw.execute('INSERT INTO "user" SELECT \'user-2\', data, created_at, updated_at FROM "user" WHERE id=\'user-1\'')
    with caplog.at_level(logging.WARNING):
        sp = await _provider(tmp_path, "old.sqlite")
        page = await sp.get_storage(User).list(OffsetPage(offset=0, length=10))
    assert len(page.items) == 2 and "continuing without it" in caplog.text
    await sp.aclose()


@pytest.mark.asyncio
async def test_jit_retries_a_username_taken_in_the_gap(tmp_path, monkeypatch):
    from primer.api.routers import sso
    sp = await _provider(tmp_path)
    await sp.set_sso_jit_enabled(True)
    users, idents = sp.get_storage(User), sp.get_storage(UserIdentity)
    real = sso._derive_unique_username
    rivals: list[str] = []

    async def derive_then_lose_the_name(user_storage, **kw):
        name = await real(user_storage, **kw)
        if not rivals:
            rivals.append(name)
            await users.create(User(id="user-rival", username=name, created_at=NOW, role="user"))   # a rival takes it first
        return name

    monkeypatch.setattr(sso, "_derive_unique_username", derive_then_lose_the_name)
    got = await _resolve_or_provision_user(storage_provider=sp, user_storage=users, identity_storage=idents,
                                           provider_id="okta", subject="sub-j", email="jo@example.com")
    assert got.username != "jo" and got.id != "user-rival"
    await sp.aclose()


# --- GS-19 -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_demote_and_delete_of_two_admins_leave_one(client, app, monkeypatch):
    made = await client.post("/v1/admin/users", json={"username": "boss2", "password": "boss2password", "role": "admin"})
    b = made.json()["id"]
    store = app.state.storage_provider.get_storage(User)
    a = next(u for u in (await store.list(OffsetPage(offset=0, length=10))).items if u.id != b).id
    real = admin_users._count_protected_admins

    async def slow_count(storage):
        n = await real(storage)
        await asyncio.sleep(0.2)          # both guards read the count before either writes
        return n

    monkeypatch.setattr(admin_users, "_count_protected_admins", slow_count)
    demote, delete = await asyncio.gather(
        client.patch(f"/v1/admin/users/{a}", json={"role": "user"}), client.delete(f"/v1/admin/users/{b}"))
    monkeypatch.setattr(admin_users, "_count_protected_admins", real)
    assert await real(store) >= 1, (demote.status_code, delete.status_code)
