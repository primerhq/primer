"""A failed SSO sign-in in a browser ends on the login screen, not on a raw JSON page (ADM-24b).

``GET /v1/auth/sso/<id>/login`` and ``/callback`` are TOP-LEVEL browser navigations: the login button sets
``window.location`` and the IdP sends the browser back. Every refusal there used to answer an RFC7807 JSON document
(502 ``provider_unreachable``, 403 ``sso_jit_disabled`` for a person with no account, 400 ``missing_code`` for a
consent the user cancelled), a bare page with no way back but the browser button.

A request that asks for HTML (what a navigation sends) now gets a ``303`` to ``/console/?sso_error=<code>``, which the
login screen turns into a sentence. Any other request (an API client, a test, ``curl``) keeps the JSON answer and its
status. Only the closed set of error codes this module defines is put in the URL, never the exception text. Link mode
(an authenticated user attaching an identity) is not a login and keeps its JSON.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from tests.api.conftest import raw_client as client, app  # noqa: F401
from tests.api.test_sso_flow import (  # noqa: F401  (rsa_keypair / attacker_rsa_keypair are fixtures)
    _IdpFixture,
    _callback,
    _link,
    _login,
    _login_as,
    _query,
    _second_client,
    _seed_provider,
    attacker_rsa_keypair,
    rsa_keypair,
)

from primer.model.oidc import UserIdentity
from primer.model.user import User

# What Chrome and Firefox send on a top-level navigation.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"}


def _sso_error(response: httpx.Response) -> str | None:
    """The ``sso_error`` code of a failure redirect; asserts the redirect itself is the documented one."""
    assert response.status_code == 303, f"{response.status_code} {response.text[:200]}"
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path) == ("", "", "/console/"), response.headers["location"]
    assert list(parse_qs(target.query)) == ["sso_error"], "the redirect must carry the error code and nothing else"
    return parse_qs(target.query)["sso_error"][0]


async def _get(client, path: str, *, headers: dict | None = None, **params) -> httpx.Response:
    return await client.get(path, params=params, headers=headers or {}, follow_redirects=False)


# ---- the login start -------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_login_start_the_provider_cannot_answer_ends_on_the_login_screen(client, app, rsa_keypair):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    respx.get(idp.discovery_url).mock(side_effect=httpx.ConnectError("the IdP is down"))
    provider = await _seed_provider(app, idp)

    r = await _get(client, f"/v1/auth/sso/{provider.id}/login", headers=BROWSER)

    assert _sso_error(r) == "provider_unreachable"


@pytest.mark.asyncio
@respx.mock
async def test_the_same_start_keeps_its_json_502_for_a_client_that_does_not_ask_for_html(client, app, rsa_keypair):
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    respx.get(idp.discovery_url).mock(side_effect=httpx.ConnectError("the IdP is down"))
    provider = await _seed_provider(app, idp)

    for headers in ({}, {"Accept": "application/json"}, {"Accept": "*/*"}):
        r = await _get(client, f"/v1/auth/sso/{provider.id}/login", headers=headers)
        assert r.status_code == 502, (headers, r.text)
        assert r.json()["extensions"]["error"] == "provider_unreachable"


@pytest.mark.asyncio
async def test_an_unknown_provider_ends_on_the_login_screen_for_a_browser_and_stays_404_json_otherwise(client, app):
    browser = await _get(client, "/v1/auth/sso/no-such-provider/login", headers=BROWSER)
    api = await _login(client, "no-such-provider")

    assert _sso_error(browser) == "provider_not_found"
    assert api.status_code == 404 and api.json()["extensions"]["error"] == "provider_not_found"


@pytest.mark.asyncio
async def test_the_failure_redirect_never_follows_a_return_to(client, app):
    """``return_to`` is caller input; the failure page is always the console's own login screen."""
    for hostile in ("//evil.example/x", "https://evil.example/", "/\\evil.example", "/console/#/w/primer"):
        r = await _get(client, "/v1/auth/sso/no-such-provider/login", headers=BROWSER, return_to=hostile)

        assert r.headers["location"] == "/console/?sso_error=provider_not_found", hostile


@pytest.mark.asyncio
@respx.mock
async def test_a_login_start_that_works_still_redirects_to_the_provider_for_a_browser(client, app, rsa_keypair):
    """The failure path must not catch the success path: a browser still gets the 302 to the IdP and its state cookie."""
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)

    r = await _get(client, f"/v1/auth/sso/{provider.id}/login", headers=BROWSER)

    assert r.status_code == 302
    assert r.headers["location"].startswith(idp.authorization_endpoint)
    assert "primer_sso_state" in r.cookies


# ---- the callback ----------------------------------------------------------------------------------------------------


async def _start(client, app, rsa_keypair, **provider_overrides):
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp, **provider_overrides)
    login = await _login(client, provider.id)
    qs = _query(login.headers["location"])
    return priv, idp, provider, qs


@pytest.mark.asyncio
@respx.mock
async def test_a_person_with_no_account_ends_on_the_login_screen(client, app, rsa_keypair):
    priv, idp, provider, qs = await _start(client, app, rsa_keypair)
    idp.queue_id_token(priv, idp.base_claims(sub="sub-nobody", nonce=qs["nonce"]))

    r = await _get(client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, code="c", state=qs["state"])

    assert _sso_error(r) == "sso_jit_disabled"
    assert "primer_session" not in r.cookies, "a refused sign-in must not mint a session"


@pytest.mark.asyncio
@respx.mock
async def test_a_cancelled_consent_ends_on_the_login_screen(client, app, rsa_keypair):
    """The IdP sends the browser back with ``error=access_denied`` and no ``code`` when the user declines."""
    _, _, provider, qs = await _start(client, app, rsa_keypair)

    r = await _get(
        client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, error="access_denied", state=qs["state"],
    )

    assert _sso_error(r) == "missing_code"


@pytest.mark.asyncio
@respx.mock
async def test_a_callback_with_no_state_cookie_ends_on_the_login_screen(client, app, rsa_keypair):
    _, _, provider, qs = await _start(client, app, rsa_keypair)
    client.cookies.clear()

    r = await _get(client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, code="c", state=qs["state"])

    assert _sso_error(r) == "invalid_state"


@pytest.mark.asyncio
@respx.mock
async def test_an_id_token_that_does_not_verify_ends_on_the_login_screen_without_echoing_why(
    client, app, rsa_keypair, attacker_rsa_keypair,
):
    _, idp, provider, qs = await _start(client, app, rsa_keypair)
    # Signed by a key the IdP never published: the validation failure text names the problem, and must stay out of the URL.
    forging_key = attacker_rsa_keypair[0]
    idp.queue_id_token(forging_key, idp.base_claims(sub="sub-forged", nonce=qs["nonce"]))

    r = await _get(client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, code="c", state=qs["state"])

    assert _sso_error(r) == "sso_validation_failed"
    assert r.headers["location"] == "/console/?sso_error=sso_validation_failed", "only the code may reach the URL"


@pytest.mark.asyncio
@respx.mock
async def test_a_disabled_account_ends_on_the_login_screen(client, app, rsa_keypair):
    priv, idp, provider, qs = await _start(client, app, rsa_keypair)
    sp = app.state.storage_provider
    user = await sp.get_storage(User).create(
        User(
            id="user-off", username="off", password_hash=None,
            created_at=datetime.now(timezone.utc), role="user", disabled=True,
        )
    )
    await sp.get_storage(UserIdentity).create(
        UserIdentity(
            user_id=user.id, provider_id=provider.id, subject="sub-off", created_at=datetime.now(timezone.utc),
        )
    )
    idp.queue_id_token(priv, idp.base_claims(sub="sub-off", nonce=qs["nonce"]))

    r = await _get(client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, code="c", state=qs["state"])

    assert _sso_error(r) == "account_disabled"
    assert "primer_session" not in r.cookies


@pytest.mark.asyncio
@respx.mock
async def test_the_same_callback_failures_keep_their_json_status_for_a_client_that_does_not_ask_for_html(
    client, app, rsa_keypair,
):
    priv, idp, provider, qs = await _start(client, app, rsa_keypair)
    idp.queue_id_token(priv, idp.base_claims(sub="sub-nobody-2", nonce=qs["nonce"]))

    r = await _callback(client, provider.id, code="c", state=qs["state"])

    assert r.status_code == 403
    assert r.json()["extensions"]["error"] == "sso_jit_disabled"


@pytest.mark.asyncio
@respx.mock
async def test_a_successful_callback_still_redirects_into_the_console_for_a_browser(client, app, rsa_keypair):
    """JIT on: the person gets an account and a session, and the redirect is the console, with no error code."""
    priv, idp, provider, qs = await _start(client, app, rsa_keypair)
    await app.state.storage_provider.set_sso_jit_enabled(True)
    await app.state.storage_provider.set_sso_default_access("user")
    idp.queue_id_token(priv, idp.base_claims(sub="sub-welcome", nonce=qs["nonce"]))

    r = await _get(client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, code="c", state=qs["state"])

    assert r.status_code == 302 and r.headers["location"] == "/console/"
    assert "primer_session" in r.cookies


# ---- link mode is not a login ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_link_keeps_its_json_even_for_a_browser(client, app, rsa_keypair):
    """An authenticated user attaching an identity another account owns: the login screen is the wrong place to land."""
    priv, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    bob = await _login_as(client, app, user_id="user-bob-b", username="bob-b")
    await app.state.storage_provider.get_storage(UserIdentity).create(
        UserIdentity(
            user_id=bob.id, provider_id=provider.id, subject="sub-taken-b", created_at=datetime.now(timezone.utc),
        )
    )

    async with _second_client(app) as client2:
        await _login_as(client2, app, user_id="user-carol-b", username="carol-b")
        link = await _link(client2, provider.id)
        qs = _query(link.headers["location"])
        idp.queue_id_token(priv, idp.base_claims(sub="sub-taken-b", nonce=qs["nonce"]))

        r = await _get(client2, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, code="c", state=qs["state"])

    assert r.status_code == 409, r.text
    assert r.json()["extensions"]["error"] == "identity_already_linked"


@pytest.mark.asyncio
@respx.mock
async def test_a_cancelled_link_keeps_its_json_even_for_a_browser(client, app, rsa_keypair):
    """The cancel raises ``missing_code`` BEFORE the callback reads the state's mode, so the link flag must already be set."""
    _, pub = rsa_keypair
    idp = _IdpFixture(pub)
    idp.register()
    provider = await _seed_provider(app, idp)
    await _login_as(client, app, user_id="user-dana", username="dana")
    link = await _link(client, provider.id)
    qs = _query(link.headers["location"])

    r = await _get(
        client, f"/v1/auth/sso/{provider.id}/callback", headers=BROWSER, error="access_denied", state=qs["state"],
    )

    assert r.status_code == 400, r.text
    assert r.json()["extensions"]["error"] == "missing_code"
