"""Integration tests for the /v1/auth router.

Reuses the existing ``client`` fixture from ``tests/api/conftest.py``
which builds a test app with the in-memory fake storage provider.
"""

from __future__ import annotations

import pytest

from primer.model.storage import OffsetPage
from primer.model.user import User

# Re-export so pytest can resolve the `client` fixture used below.
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


@pytest.mark.asyncio
async def test_status_no_user_initially(client):
    resp = await client.get("/v1/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_user"] is False
    assert body["authenticated"] is False
    assert body["username"] is None


@pytest.mark.asyncio
async def test_register_creates_user_and_sets_cookie(client):
    resp = await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "alice"
    # Cookie set
    assert "primer_session" in resp.cookies


@pytest.mark.asyncio
async def test_register_first_user_gets_admin_role(client, fake_storage_provider):
    """Single-user v1 always registers exactly one operator account —
    Layer 1 RBAC (Task 2) promotes that first account to admin so
    there's always at least one admin after first boot."""
    resp = await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    assert resp.status_code == 200, resp.text

    storage = fake_storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=1))
    assert page.items[0].role == "admin"


@pytest.mark.asyncio
async def test_register_twice_returns_409(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.post(
        "/v1/auth/register",
        json={"username": "bob", "password": "anothersecret"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "user_already_exists"


@pytest.mark.asyncio
async def test_register_weak_password_422(client):
    resp = await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "short"},  # < 8 chars
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_bad_username_422(client):
    resp = await client.post(
        "/v1/auth/register",
        json={"username": "Bad Username!", "password": "supersecret"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_username"


@pytest.mark.asyncio
async def test_login_success(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    # Logout first (registration auto-logs-in)
    await client.post("/v1/auth/logout")
    client.cookies.clear()

    resp = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "supersecret"},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"
    assert "primer_session" in resp.cookies


@pytest.mark.asyncio
async def test_login_wrong_password_401(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    client.cookies.clear()
    resp = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "WRONG"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_unknown_user_401(client):
    resp = await client.post(
        "/v1/auth/login",
        json={"username": "nobody", "password": "whatever"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_cookie(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 204
    # Cookie deletion sets Max-Age=0
    set_cookie = resp.headers.get("set-cookie", "")
    assert "primer_session" in set_cookie


@pytest.mark.asyncio
async def test_login_remember_false_omits_max_age(client):
    """remember=False → session cookie (no Max-Age) so browser drops on close."""
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    await client.post("/v1/auth/logout")
    client.cookies.clear()

    resp = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "supersecret", "remember": False},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "primer_session=" in set_cookie
    assert "Max-Age" not in set_cookie and "max-age" not in set_cookie


@pytest.mark.asyncio
async def test_login_remember_default_sets_max_age(client):
    """Default remember (true) → persistent cookie with Max-Age."""
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    await client.post("/v1/auth/logout")
    client.cookies.clear()

    resp = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "supersecret"},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "primer_session=" in set_cookie
    assert "Max-Age=604800" in set_cookie  # 7 days


@pytest.mark.asyncio
async def test_status_has_user_after_register(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    # Logout — drop the cookie to test the has_user-but-unauth case.
    await client.post("/v1/auth/logout")
    client.cookies.clear()
    resp = await client.get("/v1/auth/status")
    body = resp.json()
    assert body["has_user"] is True
    assert body["authenticated"] is False  # no cookie

    # Re-login and check authenticated flag
    await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "supersecret"},
    )
    # NOTE: status.authenticated will be True only AFTER middleware runs
    # (Commit 5). This test asserts the structure regardless.
    resp = await client.get("/v1/auth/status")
    assert resp.json()["has_user"] is True


@pytest.mark.asyncio
async def test_status_returns_role_and_must_change(client):
    # First registered user becomes role="admin" (Task 2); registration
    # auto-logs-in so the status probe is authenticated.
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.get("/v1/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["role"] == "admin"
    assert body["must_change_password"] is False


@pytest.mark.asyncio
async def test_login_sso_only_user_401_not_500(client, app):
    # An account provisioned via SSO has no local password (hash is None).
    # A password login must 401 (never 500) and leak nothing about the
    # account's existence.
    from datetime import datetime, timezone
    from primer.model.user import User

    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id="user-sso",
            username="ssouser",
            password_hash=None,
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )
    resp = await client.post(
        "/v1/auth/login",
        json={"username": "ssouser", "password": "anything"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_credentials"

    # Byte-for-byte indistinguishable from an unknown user.
    unknown = await client.post(
        "/v1/auth/login",
        json={"username": "nobody", "password": "anything"},
    )
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == resp.json()["detail"]


@pytest.mark.asyncio
async def test_login_disabled_user_401(client, app):
    # A disabled account with a CORRECT password must still be rejected,
    # indistinguishably from bad creds.
    from datetime import datetime, timezone
    from primer.auth.passwords import hash_password
    from primer.model.user import User

    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id="user-dis",
            username="disabled",
            password_hash=await hash_password("supersecret"),
            created_at=datetime.now(timezone.utc),
            role="user",
            disabled=True,
        )
    )
    resp = await client.post(
        "/v1/auth/login",
        json={"username": "disabled", "password": "supersecret"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_change_password_success(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.post(
        "/v1/auth/change-password",
        json={
            "current_password": "supersecret",
            "new_password": "newsupersecret",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "alice"

    # New password works; the old one no longer does.
    await client.post("/v1/auth/logout")
    client.cookies.clear()
    old = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "supersecret"},
    )
    assert old.status_code == 401
    new = await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "newsupersecret"},
    )
    assert new.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current_401(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.post(
        "/v1/auth/change-password",
        json={"current_password": "WRONG", "new_password": "newsupersecret"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_change_password_clears_must_change_flag(client, app):
    from primer.model.storage import OffsetPage
    from primer.model.user import User

    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    # Force the rotation flag on in storage.
    storage = app.state.storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=1))
    user = page.items[0]
    user.must_change_password = True
    await storage.update(user)

    # Sanity: the flag now surfaces via /auth/status (Task 3).
    pre = await client.get("/v1/auth/status")
    assert pre.json()["must_change_password"] is True

    resp = await client.post(
        "/v1/auth/change-password",
        json={
            "current_password": "supersecret",
            "new_password": "newsupersecret",
        },
    )
    assert resp.status_code == 200, resp.text

    post = await client.get("/v1/auth/status")
    assert post.json()["must_change_password"] is False
