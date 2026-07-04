"""HTTP-surface tests for the admin Users CRUD router (/v1/admin/users)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Convention: shared API test fixtures.
from tests.api.conftest import raw_client as _raw, app, fake_provider_registry  # noqa: F401

from primer.auth.passwords import hash_password
from primer.model.user import User


@pytest.mark.asyncio
async def test_admin_users_crud_round_trip(client):
    now = datetime.now(timezone.utc).isoformat()
    body = {
        "id": "user-bob",
        "username": "bob",
        "password_hash": await hash_password("bobpassword"),
        "created_at": now,
        "role": "user",
    }
    r = await client.post("/v1/admin/users", json=body)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["id"] == "user-bob"
    assert got["role"] == "user"
    # A password was supplied at create → forced must_change_password.
    assert got["must_change_password"] is True

    r = await client.get("/v1/admin/users/user-bob")
    assert r.status_code == 200, r.text

    r = await client.get("/v1/admin/users")
    assert r.status_code == 200, r.text
    assert any(u["id"] == "user-bob" for u in r.json()["items"])

    upd = {**body, "username": "bobby"}
    r = await client.put("/v1/admin/users/user-bob", json=upd)
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "bobby"

    r = await client.delete("/v1/admin/users/user-bob")
    assert r.status_code in (200, 204), r.text


@pytest.mark.asyncio
async def test_cannot_delete_last_admin(client):
    # The client fixture registered 'testuser' as the FIRST user → role=admin.
    r = await client.get("/v1/admin/users")
    admin = next(u for u in r.json()["items"] if u["username"] == "testuser")
    r = await client.delete(f"/v1/admin/users/{admin['id']}")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "last_admin_protected"


@pytest.mark.asyncio
async def test_cannot_demote_last_admin(client):
    r = await client.get("/v1/admin/users")
    admin = next(u for u in r.json()["items"] if u["username"] == "testuser")
    demoted = {**admin, "role": "user"}
    r = await client.put(f"/v1/admin/users/{admin['id']}", json=demoted)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "last_admin_protected"


@pytest.mark.asyncio
async def test_can_demote_when_second_admin_exists(client, app):
    # Seed a second protected admin directly in storage.
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-admin2",
            username="admin2",
            password_hash=await hash_password("pw2"),
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    r = await client.get("/v1/admin/users")
    admin = next(u for u in r.json()["items"] if u["username"] == "testuser")
    demoted = {**admin, "role": "user"}
    r = await client.put(f"/v1/admin/users/{admin['id']}", json=demoted)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"
