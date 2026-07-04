"""RBAC HTTP deps require_user / require_admin — Spec §6.4.

Mounts two throwaway endpoints on the shared test app — one guarded by
``require_user``, one by ``require_admin`` — then drives them as an admin,
an ordinary user, a restricted user, and unauthenticated, asserting the
role matrix and the ``{"error": "forbidden_role"}`` / ``auth_required``
bodies. Users are seeded directly via storage (bypassing single-user
register) and logged in via POST /v1/auth/login for a cookie.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import APIRouter, Depends

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.api.deps import require_admin, require_user
from primer.auth.passwords import hash_password
from primer.model.user import User


def _mount_rbac_test_endpoints(app) -> None:
    """Idempotently mount /v1/_test_require_user + /v1/_test_require_admin."""
    if getattr(app.state, "_rbac_test_mounted", False):
        return
    r = APIRouter(prefix="/v1")

    @r.get("/_test_require_user", dependencies=[Depends(require_user)])
    async def _user_handler() -> dict:
        return {"ok": "user"}

    @r.get("/_test_require_admin", dependencies=[Depends(require_admin)])
    async def _admin_handler() -> dict:
        return {"ok": "admin"}

    app.include_router(r)
    app.state._rbac_test_mounted = True


async def _seed(app, *, uid: str, username: str, role: str) -> None:
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id=uid,
            username=username,
            password_hash=await hash_password("pw"),
            created_at=datetime.now(timezone.utc),
            role=role,
        )
    )


async def _login(client, username: str) -> None:
    client.cookies.clear()
    resp = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": "pw"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_admin_passes_both(app, client):
    _mount_rbac_test_endpoints(app)
    await _seed(app, uid="user-admin", username="admin1", role="admin")
    await _login(client, "admin1")
    assert (await client.get("/v1/_test_require_user")).status_code == 200
    assert (await client.get("/v1/_test_require_admin")).status_code == 200


@pytest.mark.asyncio
async def test_user_passes_require_user_but_not_admin(app, client):
    _mount_rbac_test_endpoints(app)
    await _seed(app, uid="user-user", username="user1", role="user")
    await _login(client, "user1")
    assert (await client.get("/v1/_test_require_user")).status_code == 200
    resp = await client.get("/v1/_test_require_admin")
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_restricted_rejected_by_both(app, client):
    _mount_rbac_test_endpoints(app)
    await _seed(app, uid="user-restr", username="restr1", role="restricted")
    await _login(client, "restr1")
    r_user = await client.get("/v1/_test_require_user")
    assert r_user.status_code == 403
    assert r_user.json()["detail"]["error"] == "forbidden_role"
    r_admin = await client.get("/v1/_test_require_admin")
    assert r_admin.status_code == 403
    assert r_admin.json()["detail"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_unauthenticated_401(app, client):
    _mount_rbac_test_endpoints(app)
    client.cookies.clear()
    r_user = await client.get("/v1/_test_require_user")
    assert r_user.status_code == 401
    assert r_user.json()["detail"]["error"] == "auth_required"
    r_admin = await client.get("/v1/_test_require_admin")
    assert r_admin.status_code == 401
    assert r_admin.json()["detail"]["error"] == "auth_required"
