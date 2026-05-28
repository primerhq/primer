"""Tests that the auth middleware correctly populates request.state."""

from __future__ import annotations

import pytest

# Pull in the existing test app fixture.
from tests.api.conftest import client, app, fake_provider_registry  # noqa: F401


@pytest.mark.asyncio
async def test_status_authenticated_flag_set_after_register(client):
    """After register, the cookie is set; subsequent /status should
    show authenticated=True with the username populated."""
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.get("/v1/auth/status")
    body = resp.json()
    assert body["has_user"] is True
    assert body["authenticated"] is True
    assert body["username"] == "alice"


@pytest.mark.asyncio
async def test_authenticated_flag_unset_after_logout(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    await client.post("/v1/auth/logout")
    client.cookies.clear()
    resp = await client.get("/v1/auth/status")
    body = resp.json()
    assert body["has_user"] is True
    assert body["authenticated"] is False


@pytest.mark.asyncio
async def test_tampered_cookie_ignored(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    # Replace the cookie with a forged value.
    client.cookies.set("primer_session", "not-a-valid-token")
    resp = await client.get("/v1/auth/status")
    assert resp.json()["authenticated"] is False


@pytest.mark.asyncio
async def test_login_round_trip_sets_session(client):
    await client.post(
        "/v1/auth/register",
        json={"username": "alice", "password": "supersecret"},
    )
    await client.post("/v1/auth/logout")
    client.cookies.clear()
    # Login → cookie set → status authenticated
    await client.post(
        "/v1/auth/login",
        json={"username": "alice", "password": "supersecret"},
    )
    resp = await client.get("/v1/auth/status")
    body = resp.json()
    assert body["authenticated"] is True
    assert body["username"] == "alice"
