"""T112 (GS-37, GS-39, GS-41, GS-42): SSO sessions follow their provider and identity, the callback URL is redacted
in logs, linking is a POST, and the public SSO routes answer fixed words (the detail goes to the log with an error id)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pytest
import respx

from tests.api.conftest import raw_client as client, app  # noqa: F401
from tests.api.test_sso_flow import (  # noqa: F401
    _IdpFixture, _callback, _login, _login_as, _query, _seed_provider, rsa_keypair,
)

from primer.common.log import redact_credentials
from primer.model.oidc import OidcProvider, UserIdentity
from primer.model.storage import OffsetPage

SESSION_PROBE = "/v1/auth/sso/identities"        # any route behind require_user


async def _sso_session(client, app, idp, priv, provider, sub="sub-h"):
    """Run a real SSO login so the client holds a cookie whose src is the provider."""
    await app.state.storage_provider.set_sso_jit_enabled(True)
    await app.state.storage_provider.set_sso_default_access("user")
    qs = _query((await _login(client, provider.id)).headers["location"])
    idp.queue_id_token(priv, idp.base_claims(sub=sub, nonce=qs["nonce"]))
    cb = await _callback(client, provider.id, code="c", state=qs["state"])
    assert cb.status_code == 302, cb.text
    assert (await client.get(SESSION_PROBE)).status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_sso_cookie_dies_with_a_disabled_provider(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await _sso_session(client, app, idp, priv, provider)
    store = app.state.storage_provider.get_storage(OidcProvider)
    await store.update(provider.model_copy(update={"enabled": False}))
    assert (await client.get(SESSION_PROBE)).status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_sso_cookie_dies_with_a_deleted_provider(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await _sso_session(client, app, idp, priv, provider)
    await app.state.storage_provider.get_storage(OidcProvider).delete(provider.id)
    assert (await client.get(SESSION_PROBE)).status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_sso_cookie_dies_when_its_identity_is_unlinked(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await _sso_session(client, app, idp, priv, provider)
    old_cookie = client.cookies.get(app.state.config.auth.cookie_name)
    rows = (await client.get(SESSION_PROBE)).json()
    assert (await client.delete(f"{SESSION_PROBE}/{rows[0]['id']}")).status_code == 204
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        other.cookies.set(app.state.config.auth.cookie_name, old_cookie)
        assert (await other.get(SESSION_PROBE)).status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_unlinking_another_provider_keeps_the_callers_session(client, app, rsa_keypair):
    priv, pub = rsa_keypair
    idp, idp2 = _IdpFixture(pub), _IdpFixture(pub)
    idp.register()
    idp2.register()
    provider, provider2 = await _seed_provider(app, idp), await _seed_provider(app, idp2)
    await _sso_session(client, app, idp, priv, provider)
    user_id = (await app.state.storage_provider.get_storage(UserIdentity).list(OffsetPage(offset=0, length=5))).items[0].user_id
    other = await app.state.storage_provider.get_storage(UserIdentity).create(
        UserIdentity(user_id=user_id, provider_id=provider2.id, subject="s2", created_at=datetime.now(timezone.utc)))
    assert (await client.delete(f"{SESSION_PROBE}/{other.id}")).status_code == 204
    assert (await client.get(SESSION_PROBE)).status_code == 200


@pytest.mark.asyncio
async def test_password_session_is_not_tied_to_any_provider(client, app):
    await _login_as(client, app, user_id="user-pw", username="pw")
    assert (await client.get(SESSION_PROBE)).status_code == 200


def test_redaction_masks_the_sso_callback_query():
    line = 'GET /v1/auth/sso/oidc-abc/callback?code=SECRETCODE&state=SECRETSTATE&x=1 HTTP/1.1'
    out = redact_credentials(line)
    assert "SECRETCODE" not in out and "SECRETSTATE" not in out
    assert "x=1" in out and "/v1/auth/sso/oidc-abc/callback?" in out
    assert redact_credentials("GET /v1/other/callback?code=keep") == "GET /v1/other/callback?code=keep"


@pytest.mark.asyncio
@respx.mock
async def test_get_link_starts_nothing_and_post_link_does(client, app, rsa_keypair):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await _login_as(client, app, user_id="user-l", username="l")
    got = await client.get(f"/v1/auth/sso/{provider.id}/link", follow_redirects=False)
    assert got.status_code == 405 and "primer_sso_state" not in got.cookies
    posted = await client.post(f"/v1/auth/sso/{provider.id}/link")
    assert posted.status_code == 200, posted.text
    assert posted.json()["url"].startswith(idp.authorization_endpoint)
    assert "primer_sso_state" in posted.cookies


@pytest.mark.asyncio
@respx.mock
async def test_login_failure_answers_fixed_words_and_logs_the_detail(client, app, caplog):
    host = "idp-leak-host.internal.example"
    respx.get(f"https://{host}/.well-known/openid-configuration").mock(side_effect=httpx.ConnectError("boom-detail-xyz"))
    provider = await app.state.storage_provider.get_storage(OidcProvider).create(
        OidcProvider(name="Leaky", discovery_url=f"https://{host}/.well-known/openid-configuration", client_id="c", client_secret=None))
    with caplog.at_level(logging.WARNING):
        r = await _login(client, provider.id)
    body = r.text
    assert r.status_code == 502 and "boom-detail-xyz" not in body and host not in body
    error_id = r.json()["extensions"]["error_id"]
    assert error_id in caplog.text and "boom-detail-xyz" in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_callback_failure_answers_fixed_words_and_logs_the_detail(client, app, rsa_keypair, caplog):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    qs = _query((await _login(client, provider.id)).headers["location"])
    idp._queue.append(httpx.Response(400, text="endpoint-body-secret-123"))
    with caplog.at_level(logging.WARNING):
        r = await _callback(client, provider.id, code="c", state=qs["state"])
    assert r.status_code == 400 and "endpoint-body-secret-123" not in r.text
    error_id = r.json()["extensions"]["error_id"]
    assert error_id in caplog.text and "endpoint-body-secret-123" in caplog.text
