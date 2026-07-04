"""A user disabled AFTER login is unauthenticated on the next request (§9)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.auth.passwords import hash_password
from primer.model.user import User


@pytest.mark.asyncio
async def test_disabled_after_login_is_unauthenticated_next_request(client, app):
    # Bootstrap: first registered user is the admin operator.
    await client.post("/v1/auth/register", json={"username": "boss", "password": "supersecret"})

    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id="user-dana",
            username="dana",
            password_hash=await hash_password("supersecret"),
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )

    # Log in as the (enabled) normal user — obtains a valid session cookie.
    await client.post("/v1/auth/logout")
    client.cookies.clear()
    await client.post("/v1/auth/login", json={"username": "dana", "password": "supersecret"})
    assert (await client.get("/v1/auth/status")).json()["authenticated"] is True

    # Admin disables dana out-of-band (as PATCH /v1/admin/users would, Task 11).
    dana = await storage.get("user-dana")
    await storage.update(dana.model_copy(update={"disabled": True}))

    # Same cookie, next request: the middleware nulls the disabled user.
    assert (await client.get("/v1/auth/status")).json()["authenticated"] is False
