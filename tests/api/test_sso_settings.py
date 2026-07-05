"""HTTP-surface tests for the admin SSO settings route (/v1/admin/sso-settings).

Covers the two ``system_state`` knobs that gate SSO JIT provisioning
(Task 2's ``sso_jit_enabled`` / ``sso_default_access`` setters): GET
reads the current values, PUT updates both, and PUT rejects
``sso_default_access="admin"`` (or any value other than
``"restricted"``/``"user"``/``null``) with 422 — the input-boundary
half of the defense-in-depth clamp already enforced defensively at the
JIT-provisioning path in ``primer/api/routers/sso.py``.
"""

from __future__ import annotations

import pytest

# Convention: shared API test fixtures.
from tests.api.conftest import raw_client as _raw, app, fake_provider_registry  # noqa: F401


@pytest.mark.asyncio
async def test_get_returns_current_settings(client, app):
    r = await client.get("/v1/admin/sso-settings")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got == {"sso_jit_enabled": False, "sso_default_access": None}


@pytest.mark.asyncio
async def test_put_updates_both_fields(client, app):
    r = await client.put(
        "/v1/admin/sso-settings",
        json={"sso_jit_enabled": True, "sso_default_access": "user"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"sso_jit_enabled": True, "sso_default_access": "user"}

    # Persisted -- a fresh GET reflects it.
    r2 = await client.get("/v1/admin/sso-settings")
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"sso_jit_enabled": True, "sso_default_access": "user"}

    # And directly in storage.
    state = await app.state.storage_provider.get_system_state()
    assert state.sso_jit_enabled is True
    assert state.sso_default_access == "user"


@pytest.mark.asyncio
async def test_put_accepts_restricted_and_null(client, app):
    r = await client.put(
        "/v1/admin/sso-settings",
        json={"sso_jit_enabled": True, "sso_default_access": "restricted"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["sso_default_access"] == "restricted"

    r2 = await client.put(
        "/v1/admin/sso-settings",
        json={"sso_jit_enabled": False, "sso_default_access": None},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"sso_jit_enabled": False, "sso_default_access": None}


@pytest.mark.asyncio
async def test_put_rejects_admin_default_access_and_does_not_persist(client, app):
    # Baseline: known-good value first.
    baseline = await client.put(
        "/v1/admin/sso-settings",
        json={"sso_jit_enabled": True, "sso_default_access": "user"},
    )
    assert baseline.status_code == 200, baseline.text

    r = await client.put(
        "/v1/admin/sso-settings",
        json={"sso_jit_enabled": True, "sso_default_access": "admin"},
    )
    assert r.status_code == 422, r.text

    # Not persisted -- the baseline value from before the rejected PUT survives.
    state = await app.state.storage_provider.get_system_state()
    assert state.sso_default_access == "user"


@pytest.mark.asyncio
async def test_put_rejects_arbitrary_string(client, app):
    r = await client.put(
        "/v1/admin/sso-settings",
        json={"sso_jit_enabled": True, "sso_default_access": "superuser"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_non_admin_forbidden(client, app):
    sp = app.state.storage_provider
    from datetime import datetime, timezone

    from primer.auth.passwords import hash_password
    from primer.model.user import User

    await sp.get_storage(User).create(
        User(
            id="user-plain",
            username="plainuser",
            role="user",
            password_hash=await hash_password("supersecret"),
            created_at=datetime.now(timezone.utc),
        )
    )
    await client.post("/v1/auth/logout")
    client.cookies.clear()
    await client.post(
        "/v1/auth/login", json={"username": "plainuser", "password": "supersecret"},
    )
    assert (await client.get("/v1/admin/sso-settings")).status_code == 403
    assert (
        await client.put(
            "/v1/admin/sso-settings",
            json={"sso_jit_enabled": True, "sso_default_access": None},
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_401(raw_client):
    assert (await raw_client.get("/v1/admin/sso-settings")).status_code == 401
    assert (
        await raw_client.put(
            "/v1/admin/sso-settings",
            json={"sso_jit_enabled": True, "sso_default_access": None},
        )
    ).status_code == 401
