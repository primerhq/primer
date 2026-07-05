"""HTTP-surface tests for /v1/admin/oidc-providers CRUD.

Admin can create + list OIDC SSO providers; the ``client_secret`` field
is a :class:`~pydantic.SecretStr` that pydantic's default JSON dump
always redacts to ``"**********"`` — these tests assert the plaintext
secret never appears in a response body. A ``role="user"`` client is
rejected with 403 on both routes (admin-only per §6.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Convention: shared API test fixtures (see test_rbac_router_wiring.py /
# test_admin_users.py for the same import pattern).
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.auth.passwords import hash_password
from primer.model.user import User


_BODY = {
    "id": "oidc-okta",
    "name": "Okta",
    "discovery_url": "https://okta.example.com/.well-known/openid-configuration",
    "client_id": "abc123",
    "client_secret": "super-secret-plaintext",
    "scopes": ["openid", "email", "profile"],
    "enabled": True,
}


@pytest.mark.asyncio
async def test_admin_create_and_list_masks_client_secret(client):
    # First registration => role='admin' (auth.register handler).
    r = await client.post(
        "/v1/auth/register",
        json={"username": "testadmin", "password": "testpassword"},
    )
    assert r.status_code == 200, r.text

    r = await client.post("/v1/admin/oidc-providers", json=_BODY)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["id"] == "oidc-okta"
    assert created["client_secret"] == "**********"
    assert "super-secret-plaintext" not in r.text

    r = await client.get("/v1/admin/oidc-providers")
    assert r.status_code == 200, r.text
    assert "super-secret-plaintext" not in r.text
    items = r.json()["items"]
    got = next(item for item in items if item["id"] == "oidc-okta")
    assert got["client_secret"] == "**********"
    assert got["name"] == "Okta"


@pytest.mark.asyncio
async def test_role_user_forbidden_on_create_and_list(client, app):
    # First registration => role='admin'.
    r = await client.post(
        "/v1/auth/register",
        json={"username": "testadmin2", "password": "testpassword"},
    )
    assert r.status_code == 200, r.text

    # Seed + log in as a role='user' account.
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-oidc-forbidden",
            username="plainuser",
            password_hash=await hash_password("pw-users-pw"),
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )
    r = await client.post(
        "/v1/auth/login",
        json={"username": "plainuser", "password": "pw-users-pw"},
    )
    assert r.status_code == 200, r.text

    r = await client.post("/v1/admin/oidc-providers", json=_BODY)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "forbidden_role"

    r = await client.get("/v1/admin/oidc-providers")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "forbidden_role"
