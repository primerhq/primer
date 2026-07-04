"""HTTP-surface tests for the admin Users CRUD router (/v1/admin/users).

Task 12 bugfix: the router used to be built on ``make_crud_router``,
which validated the raw request body as a full ``User`` (requiring
``id``, ``created_at``, and a pre-hashed ``password_hash``) and leaked
``password_hash`` via ``response_model=User``. These tests exercise the
provisioning shape the admin console actually sends — plaintext
``password`` in, no ``password_hash`` ever out — plus an integration
assertion that the hashed password round-trips through
``POST /v1/auth/login``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Convention: shared API test fixtures.
from tests.api.conftest import raw_client as _raw, app, fake_provider_registry  # noqa: F401

from primer.auth.passwords import hash_password
from primer.model.user import User


@pytest.mark.asyncio
async def test_create_user_returns_no_password_hash_and_forces_rotation(client):
    r = await client.post(
        "/v1/admin/users",
        json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    assert r.status_code == 201, r.text
    got = r.json()
    assert "password_hash" not in got
    assert got["username"] == "bob"
    assert got["role"] == "user"
    assert got["must_change_password"] is True
    assert "id" in got and got["id"]
    assert "created_at" in got


@pytest.mark.asyncio
async def test_created_user_can_login_with_plaintext_password(client):
    """Integration: proves the hash round-trip end to end, not just that
    a password_hash field was set somewhere."""
    r = await client.post(
        "/v1/admin/users",
        json={"username": "carol", "password": "carolpassword123", "role": "user"},
    )
    assert r.status_code == 201, r.text

    login = await client.post(
        "/v1/auth/login",
        json={"username": "carol", "password": "carolpassword123"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["username"] == "carol"


@pytest.mark.asyncio
async def test_create_user_without_password_has_no_hash_and_no_forced_rotation(client):
    r = await client.post(
        "/v1/admin/users",
        json={"username": "dave", "role": "restricted"},
    )
    assert r.status_code == 201, r.text
    got = r.json()
    assert "password_hash" not in got
    assert got["must_change_password"] is False


@pytest.mark.asyncio
async def test_create_duplicate_username_conflicts(client):
    r = await client.post(
        "/v1/admin/users",
        json={"username": "erin", "password": "erinpassword", "role": "user"},
    )
    assert r.status_code == 201, r.text

    dup = await client.post(
        "/v1/admin/users",
        json={"username": "erin", "password": "otherpassword", "role": "user"},
    )
    assert dup.status_code == 409, dup.text
    assert dup.json()["detail"]["error"] == "user_already_exists"


@pytest.mark.asyncio
async def test_list_and_get_omit_password_hash(client):
    await client.post(
        "/v1/admin/users",
        json={"username": "frank", "password": "frankpassword", "role": "user"},
    )

    r = await client.get("/v1/admin/users")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert all("password_hash" not in u for u in items)
    frank = next(u for u in items if u["username"] == "frank")

    r = await client.get(f"/v1/admin/users/{frank['id']}")
    assert r.status_code == 200, r.text
    assert "password_hash" not in r.json()


@pytest.mark.asyncio
async def test_get_missing_user_404s(client):
    r = await client.get("/v1/admin/users/user-does-not-exist")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_patch_password_reset_allows_login_with_new_password(client):
    create = await client.post(
        "/v1/admin/users",
        json={"username": "grace", "password": "graceoldpassword", "role": "user"},
    )
    assert create.status_code == 201, create.text
    user_id = create.json()["id"]

    patch = await client.patch(
        f"/v1/admin/users/{user_id}", json={"password": "gracenewpassword"},
    )
    assert patch.status_code == 200, patch.text
    got = patch.json()
    assert "password_hash" not in got
    assert got["must_change_password"] is True

    # Old password no longer works.
    bad = await client.post(
        "/v1/auth/login",
        json={"username": "grace", "password": "graceoldpassword"},
    )
    assert bad.status_code == 401, bad.text

    # New password does.
    good = await client.post(
        "/v1/auth/login",
        json={"username": "grace", "password": "gracenewpassword"},
    )
    assert good.status_code == 200, good.text


@pytest.mark.asyncio
async def test_patch_updates_email_role_disabled_only(client):
    create = await client.post(
        "/v1/admin/users",
        json={"username": "henry", "password": "henrypassword", "role": "user"},
    )
    user_id = create.json()["id"]

    patch = await client.patch(
        f"/v1/admin/users/{user_id}",
        json={"email": "henry@example.com", "role": "restricted", "disabled": True},
    )
    assert patch.status_code == 200, patch.text
    got = patch.json()
    assert got["email"] == "henry@example.com"
    assert got["role"] == "restricted"
    assert got["disabled"] is True
    # must_change_password untouched by a non-password patch.
    assert got["must_change_password"] is True


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
    r = await client.patch(f"/v1/admin/users/{admin['id']}", json={"role": "user"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "last_admin_protected"


@pytest.mark.asyncio
async def test_cannot_disable_last_admin(client):
    r = await client.get("/v1/admin/users")
    admin = next(u for u in r.json()["items"] if u["username"] == "testuser")
    r = await client.patch(f"/v1/admin/users/{admin['id']}", json={"disabled": True})
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
    r = await client.patch(f"/v1/admin/users/{admin['id']}", json={"role": "user"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"


@pytest.mark.asyncio
async def test_can_delete_when_second_admin_exists(client, app):
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-admin3",
            username="admin3",
            password_hash=await hash_password("pw3"),
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    r = await client.get("/v1/admin/users")
    admin = next(u for u in r.json()["items"] if u["username"] == "testuser")
    r = await client.delete(f"/v1/admin/users/{admin['id']}")
    assert r.status_code == 204, r.text
