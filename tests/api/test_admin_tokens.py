"""Admin API-key management: /v1/admin/users/{user_id}/tokens."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.auth.passwords import hash_password
from primer.model.api_token import ApiToken
from primer.model.user import User


async def _seed_user(sp, uid, username, role="user"):
    await sp.get_storage(User).create(
        User(id=uid, username=username, role=role,
             password_hash=await hash_password("supersecret"),
             created_at=datetime.now(timezone.utc)))


async def _seed_token(sp, tid, uid, name, revoked=False):
    await sp.get_storage(ApiToken).create(
        ApiToken(id=tid, user_id=uid, name=name,
                 token_hash="a" * 64, prefix="pk_abcde",
                 scopes=["mcp"], created_at=datetime.now(timezone.utc),
                 revoked_at=(datetime.now(timezone.utc) if revoked else None)))


@pytest.mark.asyncio
async def test_admin_lists_another_users_tokens(client, app):
    # testuser (auto-registered) is admin.
    await client.post("/v1/auth/register", json={"username": "boss", "password": "supersecret"})
    sp = app.state.storage_provider
    await _seed_user(sp, "user-bob", "bob")
    await _seed_token(sp, "at-1", "user-bob", "bob-key")

    r = await client.get("/v1/admin/users/user-bob/tokens")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [t["name"] for t in items] == ["bob-key"]
    # summaries only — no secrets
    assert "plaintext" not in items[0] and "token_hash" not in items[0]


@pytest.mark.asyncio
async def test_admin_revokes_another_users_token(client, app):
    await client.post("/v1/auth/register", json={"username": "boss", "password": "supersecret"})
    sp = app.state.storage_provider
    await _seed_user(sp, "user-bob", "bob")
    await _seed_token(sp, "at-1", "user-bob", "bob-key")

    r = await client.delete("/v1/admin/users/user-bob/tokens/at-1")
    assert r.status_code == 204
    row = await sp.get_storage(ApiToken).get("at-1")
    assert row.revoked_at is not None


@pytest.mark.asyncio
async def test_missing_user_or_foreign_token_404(client, app):
    await client.post("/v1/auth/register", json={"username": "boss", "password": "supersecret"})
    sp = app.state.storage_provider
    await _seed_user(sp, "user-bob", "bob")
    await _seed_user(sp, "user-eve", "eve")
    await _seed_token(sp, "at-eve", "user-eve", "eve-key")

    assert (await client.get("/v1/admin/users/user-nope/tokens")).status_code == 404
    # token exists but belongs to eve, not bob -> masked as 404
    assert (await client.delete("/v1/admin/users/user-bob/tokens/at-eve")).status_code == 404


@pytest.mark.asyncio
async def test_non_admin_forbidden(client, app):
    await client.post("/v1/auth/register", json={"username": "boss", "password": "supersecret"})
    sp = app.state.storage_provider
    await _seed_user(sp, "user-u", "normaluser", role="user")
    await _seed_token(sp, "at-1", "user-u", "k")
    # log in as the normal user
    await client.post("/v1/auth/logout"); client.cookies.clear()
    await client.post("/v1/auth/login", json={"username": "normaluser", "password": "supersecret"})
    assert (await client.get("/v1/admin/users/user-u/tokens")).status_code == 403
    assert (await client.delete("/v1/admin/users/user-u/tokens/at-1")).status_code == 403
