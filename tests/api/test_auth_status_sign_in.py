"""``GET /v1/auth/status`` says how the session signed in (platform revamp D7: the Profile page's sign-in line).

``sign_in_method`` and ``sign_in_provider`` are read from ``request.state.actor``, the ``Principal`` that the auth middleware resolves
(``primer/api/middleware/auth.py``). Every case below runs the real middleware and router: a password cookie, the cookie the SSO
callback mints, a bearer API key, auth switched off, and no session at all.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from httpx import ASGITransport

from primer.api.routers.auth import _sign_in_of
from primer.auth.tokens import sign_session
from primer.model.oidc import OidcProvider
from primer.model.principal import Principal
from primer.model.user import User


async def _sso_client(app, provider_id: str) -> httpx.AsyncClient:
    """A second account signed in through single sign-on: the cookie the SSO callback mints (primer/api/routers/sso.py:686)."""
    user = await app.state.storage_provider.get_storage(User).create(User(
        id="user-sso", username="sso-ana", password_hash=None, created_at=datetime.now(timezone.utc), role="admin"))
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=user.id, username=user.username, secret=app.state.session_secret, src=provider_id, epoch=user.session_epoch))
    return c


async def test_a_password_session_reports_password(client):
    body = (await client.get("/v1/auth/status")).json()
    assert (body["sign_in_method"], body["sign_in_provider"]) == ("password", None)


async def test_an_sso_session_reports_the_providers_name(app):
    await app.state.storage_provider.get_storage(OidcProvider).create(OidcProvider(
        id="oidc-okta", name="Okta", discovery_url="https://idp.example/.well-known/openid-configuration", client_id="primer"))
    async with await _sso_client(app, "oidc-okta") as c:
        body = (await c.get("/v1/auth/status")).json()
    assert (body["sign_in_method"], body["sign_in_provider"]) == ("sso", "Okta")


async def test_an_sso_session_whose_provider_is_gone_reports_its_id(app):
    async with await _sso_client(app, "oidc-gone") as c:
        body = (await c.get("/v1/auth/status")).json()
    assert (body["sign_in_method"], body["sign_in_provider"]) == ("sso", "oidc-gone")


async def test_a_bearer_key_reports_api_token(client, app):
    plaintext = (await client.post("/v1/auth/tokens", json={"name": "probe"})).json()["plaintext"]
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": f"Bearer {plaintext}"}) as c:
        body = (await c.get("/v1/auth/status")).json()
    assert (body["sign_in_method"], body["sign_in_provider"]) == ("api_token", None)


async def test_auth_off_reports_auth_disabled(app):
    app.state.config = app.state.config.model_copy(
        update={"auth": app.state.config.auth.model_copy(update={"enabled": False})})
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get("/v1/auth/status")).json()
    assert (body["sign_in_method"], body["sign_in_provider"]) == ("auth_disabled", None)


async def test_signed_out_reports_neither(raw_client):
    body = (await raw_client.get("/v1/auth/status")).json()
    assert body["authenticated"] is False
    assert (body["sign_in_method"], body["sign_in_provider"]) == (None, None)


async def test_an_sso_session_still_answers_when_the_provider_read_fails(app, monkeypatch, caplog):
    """The probe is the console's boot read: a provider table that cannot be read must not turn it into a 500."""
    async def unreachable(*_args, **_kwargs):
        raise RuntimeError("provider table unreachable")

    monkeypatch.setattr(app.state.storage_provider.get_storage(OidcProvider), "get", unreachable)
    with caplog.at_level(logging.ERROR, logger="primer.api.routers.auth"):
        async with await _sso_client(app, "oidc-okta") as c:
            r = await c.get("/v1/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert (body["sign_in_method"], body["sign_in_provider"]) == ("sso", "oidc-okta")
    failures = [rec for rec in caplog.records if rec.name == "primer.api.routers.auth" and rec.exc_info]
    assert len(failures) == 1 and "oidc-okta" in failures[0].getMessage()


async def test_only_an_sso_session_reads_the_provider_table(client, raw_client, app, monkeypatch):
    """Every console boot calls the probe, signed in or not: only an SSO session may cost a provider read."""
    storage = app.state.storage_provider.get_storage(OidcProvider)
    real_get = storage.get
    reads: list[str] = []

    async def counting_get(provider_id, **kwargs):
        reads.append(provider_id)
        return await real_get(provider_id, **kwargs)

    monkeypatch.setattr(storage, "get", counting_get)
    plaintext = (await client.post("/v1/auth/tokens", json={"name": "probe"})).json()["plaintext"]
    await client.get("/v1/auth/status")  # a password session
    await raw_client.get("/v1/auth/status")  # signed out
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": f"Bearer {plaintext}"}) as c:
        await c.get("/v1/auth/status")  # a bearer key
    assert reads == []
    async with await _sso_client(app, "oidc-gone") as c:
        await c.get("/v1/auth/status")  # single sign-on
    assert reads == ["oidc-gone"]
    app.state.config = app.state.config.model_copy(
        update={"auth": app.state.config.auth.model_copy(update={"enabled": False})})
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/v1/auth/status")  # auth off
    assert reads == ["oidc-gone"]


async def test_an_actor_of_another_kind_reports_neither():
    """The middleware resolves only user, api_token and system actors for a request; an actor of any other kind is not a sign-in."""
    request = SimpleNamespace(state=SimpleNamespace(
        actor=Principal(type="trigger", id="trigger-1", display="nightly", source="internal")))
    assert await _sign_in_of(request) == (None, None)
