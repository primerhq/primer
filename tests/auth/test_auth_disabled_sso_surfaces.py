"""Auth-disabled requests must not start or complete an SSO state flow."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import primer.api.routers.sso as sso_router
from primer.model.oidc import UserIdentity
from primer.model.storage import OffsetPage
from primer.model.user import User
from tests.api.conftest import app, raw_client as client  # noqa: F401


pytestmark = pytest.mark.asyncio


def _disable_auth(app) -> None:
    app.state.config = app.state.config.model_copy(
        update={
            "auth": app.state.config.auth.model_copy(update={"enabled": False}),
        }
    )
    app.state.session_secret = None


def _assert_auth_disabled(resp) -> None:
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"] == "/errors/bad-request"
    assert resp.json()["extensions"]["error"] == "auth_disabled"
    assert "primer_sso_state" not in resp.cookies
    assert "primer_session" not in resp.cookies


async def test_auth_disabled_provider_and_identity_lists_keep_system_access(
    client, app,
):
    _disable_auth(app)

    providers = await client.get("/v1/auth/sso/providers")
    identities = await client.get("/v1/auth/sso/identities")

    assert providers.status_code == 200 and providers.json() == []
    assert identities.status_code == 200 and identities.json() == []


async def test_sso_signed_flow_refusals_are_in_openapi(app):
    schema = app.openapi()

    for path, method in (
        ("/v1/auth/sso/{provider_id}/login", "get"),
        ("/v1/auth/sso/{provider_id}/link", "post"),
        ("/v1/auth/sso/{provider_id}/callback", "get"),
        ("/v1/auth/sso/identities/{identity_id}", "delete"),
    ):
        refusal = schema["paths"][path][method]["responses"]["400"]
        assert refusal["description"] == "Bad Request"
        assert "application/problem+json" in refusal["content"]


async def test_sso_login_auth_disabled_refuses_before_starting_state(
    client, app, monkeypatch,
):
    _disable_auth(app)

    async def must_not_start(**_kwargs):
        raise AssertionError("the SSO state flow must not start")

    monkeypatch.setattr(sso_router, "_begin_oidc_flow", must_not_start)
    resp = await client.get(
        "/v1/auth/sso/oidc-unused/login",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    _assert_auth_disabled(resp)


async def test_sso_link_auth_disabled_refuses_before_starting_state(
    client, app, monkeypatch,
):
    _disable_auth(app)

    async def must_not_start(**_kwargs):
        raise AssertionError("the SSO link state flow must not start")

    monkeypatch.setattr(sso_router, "_begin_oidc_flow", must_not_start)
    resp = await client.post(
        "/v1/auth/sso/oidc-unused/link", follow_redirects=False,
    )

    _assert_auth_disabled(resp)


async def test_sso_callback_auth_disabled_refuses_before_completion_or_writes(
    client, app, monkeypatch,
):
    _disable_auth(app)

    async def must_not_complete(**_kwargs):
        raise AssertionError("the SSO callback must not complete")

    monkeypatch.setattr(sso_router, "_complete_oidc_flow", must_not_complete)
    resp = await client.get(
        "/v1/auth/sso/oidc-unused/callback",
        params={"code": "unused", "state": "unused"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    _assert_auth_disabled(resp)
    storage = app.state.storage_provider
    users = await storage.get_storage(User).list(OffsetPage(offset=0, length=1))
    identities = await storage.get_storage(UserIdentity).list(
        OffsetPage(offset=0, length=1),
    )
    assert users.items == []
    assert identities.items == []


async def test_sso_unlink_auth_disabled_refuses_before_read_or_delete(
    client, app, monkeypatch,
):
    storage = app.state.storage_provider
    identity_storage = storage.get_storage(UserIdentity)
    row = await identity_storage.create(
        UserIdentity(
            user_id="system",
            provider_id="oidc-guarded",
            subject="must-remain-linked",
            created_at=datetime.now(timezone.utc),
        )
    )
    real_get = identity_storage.get
    _disable_auth(app)

    async def must_not_read(_identity_id):
        raise AssertionError("auth-disabled unlink must not read identity storage")

    monkeypatch.setattr(identity_storage, "get", must_not_read)
    resp = await client.delete(f"/v1/auth/sso/identities/{row.id}")

    _assert_auth_disabled(resp)
    assert await real_get(row.id) == row
