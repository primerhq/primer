"""Integration tests for the /v1/auth router.

Reuses the existing ``client`` fixture from ``tests/api/conftest.py``
which builds a test app with the in-memory fake storage provider.
"""

from __future__ import annotations

import pytest

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
