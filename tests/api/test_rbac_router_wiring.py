"""RBAC include-time router wiring (§6.2).

An admin-seeded client reaches an admin-gated route; a role='user'
client is rejected (403 forbidden_role) on the same admin route but
reaches a require_user feature route.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from primer.auth.passwords import hash_password
from primer.model.user import User


@pytest.mark.asyncio
async def test_admin_route_admin_ok_user_forbidden_feature_ok(client, app):
    # First registration => role='admin' (register handler, Task 2).
    r = await client.post(
        "/v1/auth/register",
        json={"username": "testuser", "password": "testpassword"},
    )
    assert r.status_code == 200, r.text

    # Admin reaches an admin-gated provider route.
    r = await client.get("/v1/llm_providers")
    assert r.status_code == 200, r.text

    # Seed a second, role='user' account and log in as that user
    # (re-login replaces the admin cookie on this client).
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-u",
            username="u",
            password_hash=await hash_password("pw-users-pw"),
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )
    r = await client.post(
        "/v1/auth/login",
        json={"username": "u", "password": "pw-users-pw"},
    )
    assert r.status_code == 200, r.text

    # role='user' is REJECTED on the admin-gated route ...
    r = await client.get("/v1/llm_providers")
    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_role"

    # ... but REACHES a require_user feature route.
    r = await client.get("/v1/agents")
    assert r.status_code == 200, r.text
