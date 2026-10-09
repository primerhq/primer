"""The OIDC client secret is kept only for the discovery origin it was stored for, through the ROUTE (ticket 01a1212a, round 1 of #711).

OIDC providers are written through ``/v1/admin/oidc-providers`` whose ``on_pre_update`` is ``_preserve_client_secret_if_blank``, NOT through ``preserve_masked_secrets``: a client that sends the
mask, ``""``, ``null`` or no ``client_secret`` at all means "unchanged", and the stored secret was put back whatever ``discovery_url`` had become. The login flow fetches that document and POSTs
``client_id:client_secret`` as Basic auth to the ``token_endpoint`` it names, so an update that pointed the discovery URL at a host of the caller's choosing and left the secret blank made the
server hand the real secret to that host (the reviewer's end-to-end probe). A blank secret under a MOVED origin is a 422 now, naming no secret and leaving the row untouched; the same host with
another path keeps the secret as before; a secret the person typed is theirs. Before this file the family table called ``preserve_masked_secrets`` on an ``OidcProvider`` directly, which no route does.

Every provider here is created DISABLED, so no discovery document is fetched (these tests are about the secret, not about discovery: ``test_oidc_provider_discovery_check.py`` is).
"""

from __future__ import annotations

import pytest

from primer.model.oidc import OidcProvider
from tests.api.conftest import app, fake_provider_registry, raw_client as client  # noqa: F401  (fixtures)

SECRET = "oidc-secret-0123456789"
HOME = "https://idp.home.example/.well-known/openid-configuration"
AWAY = "https://attacker.example/.well-known/openid-configuration"
BODY = {"id": "oidc-1", "name": "Home IdP", "discovery_url": HOME, "client_id": "c", "client_secret": SECRET, "scopes": ["openid"], "enabled": False}


async def _admin_with_provider(client, username: str) -> dict:
    r = await client.post("/v1/auth/register", json={"username": username, "password": "testpassword"})
    assert r.status_code == 200, r.text
    created = await client.post("/v1/admin/oidc-providers", json=BODY)
    assert created.status_code == 201, created.text
    return (await client.get("/v1/admin/oidc-providers/oidc-1")).json()


async def _stored(app) -> OidcProvider:
    stored = await app.state.storage_provider.get_storage(OidcProvider).get("oidc-1")
    assert stored is not None
    return stored


def _blank(served: dict, how: str) -> dict:
    body = dict(served)
    if how == "omitted":
        del body["client_secret"]
    else:
        body["client_secret"] = {"mask": "**********", "null": None, "empty": ""}[how]
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["mask", "null", "omitted", "empty"])
async def test_a_moved_discovery_host_with_a_blank_secret_is_a_422_and_the_row_is_untouched(client, app, how: str) -> None:
    served = await _admin_with_provider(client, f"admin-moved-{how}")
    body = {**_blank(served, how), "discovery_url": AWAY}

    r = await client.put("/v1/admin/oidc-providers/oidc-1", json=body)

    assert r.status_code == 422, r.text
    assert "re-enter" in r.text and SECRET not in r.text
    stored = await _stored(app)
    assert stored.discovery_url == HOME and stored.client_secret.get_secret_value() == SECRET, "the stored row is untouched"


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", ["http://idp.home.example/.well-known/openid-configuration", "https://idp.home.example:8443/.well-known/openid-configuration", "https://other.home.example/x"])
async def test_another_scheme_port_or_host_is_another_origin(client, app, moved: str) -> None:
    served = await _admin_with_provider(client, "admin-origins")

    r = await client.put("/v1/admin/oidc-providers/oidc-1", json={**_blank(served, "mask"), "discovery_url": moved})

    assert r.status_code == 422, r.text
    assert (await _stored(app)).client_secret.get_secret_value() == SECRET


@pytest.mark.asyncio
async def test_the_same_host_with_another_path_keeps_the_secret(client, app) -> None:
    served = await _admin_with_provider(client, "admin-path")

    r = await client.put("/v1/admin/oidc-providers/oidc-1", json={**_blank(served, "mask"), "discovery_url": "https://IDP.home.example:443/other/.well-known/openid-configuration"})

    assert r.status_code == 200, r.text
    stored = await _stored(app)
    assert stored.discovery_url.endswith("/other/.well-known/openid-configuration") and stored.client_secret.get_secret_value() == SECRET


@pytest.mark.asyncio
async def test_a_moved_discovery_host_with_a_new_secret_stores_it(client, app) -> None:
    served = await _admin_with_provider(client, "admin-new-secret")

    r = await client.put("/v1/admin/oidc-providers/oidc-1", json={**served, "discovery_url": AWAY, "client_secret": "a-different-secret-value"})

    assert r.status_code == 200, r.text
    stored = await _stored(app)
    assert stored.discovery_url == AWAY and stored.client_secret.get_secret_value() == "a-different-secret-value"


@pytest.mark.asyncio
async def test_a_provider_with_no_stored_secret_has_nothing_to_hand_over(client, app) -> None:
    r = await client.post("/v1/auth/register", json={"username": "admin-no-secret", "password": "testpassword"})
    assert r.status_code == 200, r.text
    created = await client.post("/v1/admin/oidc-providers", json={**BODY, "client_secret": None})
    assert created.status_code == 201, created.text

    r = await client.put("/v1/admin/oidc-providers/oidc-1", json={**BODY, "client_secret": None, "discovery_url": AWAY})

    assert r.status_code == 200, r.text
    assert (await _stored(app)).discovery_url == AWAY


@pytest.mark.asyncio
async def test_an_update_that_does_not_move_the_origin_still_keeps_a_blank_secret(client, app) -> None:
    served = await _admin_with_provider(client, "admin-rename")

    r = await client.put("/v1/admin/oidc-providers/oidc-1", json={**_blank(served, "omitted"), "name": "Renamed"})

    assert r.status_code == 200, r.text
    stored = await _stored(app)
    assert stored.name == "Renamed" and stored.client_secret.get_secret_value() == SECRET
