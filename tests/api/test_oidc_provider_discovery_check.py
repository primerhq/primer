"""An SSO provider can only be ENABLED once its discovery document answers (ADM-24 of the 2026-10-08 admin review).

``POST /v1/admin/oidc-providers`` used to store any discovery URL, even ``not-a-url``, enabled by default, and every enabled provider
is listed on the signed-out login screen as "Sign in with <name>". One typo in the admin form therefore put a button on the login
page for everyone, and pressing it navigated the browser to ``/v1/auth/sso/<id>/login`` and a bare JSON 502 page.

The rule now: a discovery URL must be a full http(s) URL (always, no network); a provider that is ENABLED, or is being enabled, or whose
URL changed while enabled, must have its discovery document fetched and parsed through the same ``oidc.discover`` the login flow uses.
A DISABLED provider saves with no network call, and an unrelated edit to an enabled provider does not refetch, so an IdP outage never
blocks a rename.

The IdP is faked with respx at the HTTP level (each test on its own host: ``primer.auth.oidc`` TTL-caches discovery per URL).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx

# Convention: shared API test fixtures (same import pattern as test_oidc_providers_router.py).
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


def _host() -> str:
    return f"idp-{uuid.uuid4().hex[:12]}.example.com"


def _doc(host: str) -> dict:
    return {
        "issuer": f"https://{host}/",
        "authorization_endpoint": f"https://{host}/authorize",
        "token_endpoint": f"https://{host}/token",
        "jwks_uri": f"https://{host}/jwks.json",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def _url(host: str) -> str:
    return f"https://{host}/.well-known/openid-configuration"


def _body(host: str, **over) -> dict:
    return {
        "id": f"oidc-{host.split('.')[0]}", "name": "Test IdP", "discovery_url": _url(host),
        "client_id": "cid", "scopes": ["openid", "email", "profile"], "enabled": True, **over,
    }


async def _admin(client) -> None:
    r = await client.post("/v1/auth/register", json={"username": "ssoadmin", "password": "testpassword"})
    assert r.status_code == 200, r.text


async def _stored_ids(client) -> list[str]:
    return [row["id"] for row in (await client.get("/v1/admin/oidc-providers")).json()["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("url", ["not-a-url", "ftp://idp.example.com/x", "https://", "  "])
async def test_a_discovery_url_that_is_not_a_full_http_url_is_refused_whether_or_not_it_is_enabled(client, url, enabled) -> None:
    await _admin(client)

    with respx.mock(assert_all_called=False) as router:
        resp = await client.post("/v1/admin/oidc-providers", json=_body(_host(), discovery_url=url, enabled=enabled))
        assert not router.calls, "a malformed URL is refused by its shape, without any network call"

    assert resp.status_code == 422, resp.text
    assert resp.json()["extensions"]["error"] == "discovery_url_invalid"
    assert await _stored_ids(client) == []


@pytest.mark.asyncio
async def test_an_enabled_provider_whose_discovery_url_cannot_be_reached_is_refused(client) -> None:
    await _admin(client)
    host = _host()

    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(side_effect=httpx.ConnectError("name or service not known"))
        resp = await client.post("/v1/admin/oidc-providers", json=_body(host))

    assert resp.status_code == 422, resp.text
    ext = resp.json()["extensions"]
    assert ext["error"] == "discovery_failed"
    assert host in ext["message"], "the refusal says which URL could not be fetched"
    assert await _stored_ids(client) == []


@pytest.mark.asyncio
async def test_an_enabled_provider_whose_discovery_endpoint_answers_an_error_is_refused(client) -> None:
    await _admin(client)
    host = _host()

    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(return_value=httpx.Response(404))
        resp = await client.post("/v1/admin/oidc-providers", json=_body(host))

    assert resp.status_code == 422, resp.text
    assert resp.json()["extensions"]["error"] == "discovery_failed"
    assert "404" in resp.json()["extensions"]["message"]
    assert await _stored_ids(client) == []


@pytest.mark.asyncio
async def test_an_enabled_provider_whose_discovery_document_is_missing_fields_is_refused(client) -> None:
    await _admin(client)
    host = _host()

    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(return_value=httpx.Response(200, json={"issuer": f"https://{host}/"}))
        resp = await client.post("/v1/admin/oidc-providers", json=_body(host))

    assert resp.status_code == 422, resp.text
    assert resp.json()["extensions"]["error"] == "discovery_failed"
    assert await _stored_ids(client) == []


@pytest.mark.asyncio
async def test_a_disabled_provider_is_saved_without_any_network_call(client) -> None:
    await _admin(client)
    host = _host()

    with respx.mock(assert_all_called=False) as router:
        dead = router.get(_url(host)).mock(side_effect=httpx.ConnectError("down"))
        resp = await client.post("/v1/admin/oidc-providers", json=_body(host, enabled=False))
        assert not dead.called

    assert resp.status_code == 201, resp.text
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_a_reachable_enabled_provider_is_saved(client) -> None:
    await _admin(client)
    host = _host()

    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(return_value=httpx.Response(200, json=_doc(host)))
        resp = await client.post("/v1/admin/oidc-providers", json=_body(host))

    assert resp.status_code == 201, resp.text
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_enabling_a_stored_disabled_provider_checks_its_document(client) -> None:
    await _admin(client)
    host = _host()
    body = _body(host, enabled=False)
    with respx.mock(assert_all_called=False):
        assert (await client.post("/v1/admin/oidc-providers", json=body)).status_code == 201

    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(side_effect=httpx.ConnectError("down"))
        resp = await client.put(f"/v1/admin/oidc-providers/{body['id']}", json={**body, "enabled": True})

    assert resp.status_code == 422, resp.text
    assert resp.json()["extensions"]["error"] == "discovery_failed"
    assert (await client.get(f"/v1/admin/oidc-providers/{body['id']}")).json()["enabled"] is False


@pytest.mark.asyncio
async def test_renaming_an_enabled_provider_does_not_refetch_while_its_idp_is_down(client) -> None:
    await _admin(client)
    host = _host()
    body = _body(host)
    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(return_value=httpx.Response(200, json=_doc(host)))
        assert (await client.post("/v1/admin/oidc-providers", json=body)).status_code == 201

    with respx.mock(assert_all_called=False) as router:
        down = router.get(_url(host)).mock(side_effect=httpx.ConnectError("outage"))
        resp = await client.put(f"/v1/admin/oidc-providers/{body['id']}", json={**body, "name": "Renamed"})
        assert not down.called, "an unrelated edit must not depend on the IdP being up"

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"


@pytest.mark.asyncio
async def test_changing_the_discovery_url_of_an_enabled_provider_checks_the_new_one(client) -> None:
    await _admin(client)
    host, other = _host(), _host()
    body = _body(host)
    with respx.mock(assert_all_called=False) as router:
        router.get(_url(host)).mock(return_value=httpx.Response(200, json=_doc(host)))
        assert (await client.post("/v1/admin/oidc-providers", json=body)).status_code == 201

    with respx.mock(assert_all_called=False) as router:
        router.get(_url(other)).mock(side_effect=httpx.ConnectError("down"))
        resp = await client.put(f"/v1/admin/oidc-providers/{body['id']}", json={**body, "discovery_url": _url(other)})

    assert resp.status_code == 422, resp.text
    assert (await client.get(f"/v1/admin/oidc-providers/{body['id']}")).json()["discovery_url"] == _url(host)
