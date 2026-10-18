"""The collections catalogue row says whether internal search is on (platform revamp, design section 11).

The Toolsets page's "Internal search: off" chip reads it here: GET /v1/tools is open to every user, while
GET /v1/internal_collections/config is admin-only.
"""

from __future__ import annotations

import pytest

from primer.model.internal import INTERNAL_COLLECTIONS_CONFIG_ID, InternalCollectionsConfig

ROUTES = ("/v1/toolsets/builtin", "/v1/tools")


def _collections(items: list[dict]) -> dict:
    return next(it for it in items if it["id"] == "collections")


async def _rows(client, route: str) -> list[dict]:
    r = await client.get(route)
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.mark.parametrize("route", ROUTES)
async def test_internal_search_is_off_without_a_config_row(client, route):
    assert _collections(await _rows(client, route))["internal_search"] is False


@pytest.mark.parametrize("route", ROUTES)
async def test_internal_search_is_on_with_a_config_row(client, fake_storage_provider, route):
    await fake_storage_provider.get_storage(InternalCollectionsConfig).create(InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID, embedding_provider_id="emb", embedding_model="m", search_provider_id="ssp",
    ))
    assert _collections(await _rows(client, route))["internal_search"] is True


@pytest.mark.parametrize("route", ROUTES)
async def test_internal_search_is_unknown_when_the_config_read_fails(client, fake_storage_provider, monkeypatch, route):
    async def _down(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(fake_storage_provider.get_storage(InternalCollectionsConfig), "find", _down)
    assert _collections(await _rows(client, route))["internal_search"] is None


@pytest.mark.parametrize("route", ROUTES)
async def test_only_the_collections_row_carries_internal_search(client, route):
    for it in await _rows(client, route):
        if it["id"] != "collections":
            assert "internal_search" not in it, it


async def test_a_plain_user_reads_internal_search_from_the_catalogue(raw_client, app):
    from datetime import datetime, timezone

    from primer.auth.passwords import hash_password
    from primer.model.user import User

    r = await raw_client.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
    assert r.status_code == 200, r.text  # the first account is the admin
    await app.state.storage_provider.get_storage(User).create(User(
        id="user-plain", username="plain", password_hash=await hash_password("pw-plain-pw"),
        created_at=datetime.now(timezone.utc), role="user",
    ))
    login = await raw_client.post("/v1/auth/login", json={"username": "plain", "password": "pw-plain-pw"})
    assert login.status_code == 200, login.text

    tools = await raw_client.get("/v1/tools")
    assert tools.status_code == 200, tools.text
    assert _collections(tools.json()["items"])["internal_search"] is False
    probe = await raw_client.get("/v1/internal_collections/config", params={"allow_missing": "true"})
    assert probe.status_code == 403, probe.text  # why the console reads the catalogue, not this route
