"""Over REST on a real SQLite store: the remaining stored url fields serve their userinfo password masked (ticket 01a1294c, the 01a11d32 remainder).

The MCP toolset ``config.url`` and the OIDC ``discovery_url`` are plain ``str`` (a URL is compared and keyed as typed, and a git URL may be a ``file://`` under the operator opt-in), so neither is an ``HttpUrl``; the OpenRouter ``app_url`` is an ``HttpUrl | None``. All three used to serve ``user:password@`` in clear next to a masked secret. They serve ``user:**********@`` now: a GET masks the password, a python-mode dump and the stored row keep the real URL, a full-replace PUT of the served body puts the stored credential back for the same origin and user only, a PUT that moves the origin with the mask is a 422 and stores nothing, and a POST carrying the served mask is a 422.
"""

from __future__ import annotations

import pytest

from primer.model.oidc import OidcProvider
from primer.model.providers.llm import LLMProvider
from primer.model.provider import Toolset
from primer.toolset.toolset_checks import repoints_stored_secrets
from tests.api.test_provider_url_userinfo_sqlite import client, sp  # noqa: F401  (fixtures: an app on a real SQLite store)

MASK = "**********"
TOOLSET_URL = "http://svc:s3cr3t@proxy.local:8080/mcp"
TOOLSET_MASKED = f"http://svc:{MASK}@proxy.local:8080/mcp"
TOOLSET_MOVED = f"http://svc:{MASK}@attacker.example:9000/mcp"
OIDC_URL = "https://user:s3cr3t@idp.example.com/.well-known/openid-configuration"
OIDC_MASKED = f"https://user:{MASK}@idp.example.com/.well-known/openid-configuration"
OIDC_MOVED = f"https://user:{MASK}@attacker-idp.example.com/.well-known/openid-configuration"
APP_URL = "http://svc:s3cr3t@app.example/"
APP_MASKED = f"http://svc:{MASK}@app.example/"
APP_MOVED = f"http://svc:{MASK}@attacker-app.example/"
APP_KEY = "sk-or-live-0123456789abcdef"


def _toolset(url: str) -> dict:
    return {"id": "ts-a", "provider": "mcp", "config": {"transport": "http", "config": {"url": url}}}


def _oidc(discovery_url: str) -> dict:
    return {
        "id": "oidc-a",
        "name": "IdP",
        "discovery_url": discovery_url,
        "client_id": "c",
        "client_secret": "cs-0123456789abcdef",
        "scopes": ["openid"],
        "enabled": False,
    }


def _openrouter(app_url: str) -> dict:
    return {
        "id": "or-a",
        "provider": "openrouter",
        "models": [{"name": "m", "context_length": 8192}],
        "config": {"api_key": APP_KEY, "app_url": app_url},
        "limits": {"max_concurrency": 1},
    }


# ---- MCP toolset config.url (str) -----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_mcp_toolset_url_is_served_masked_and_kept_through_a_put(client, sp) -> None:
    created = await client.post("/v1/toolsets?allow_unreachable=true", json=_toolset(TOOLSET_URL))
    assert created.status_code in (200, 201), created.text
    served = (await client.get("/v1/toolsets/ts-a")).json()
    assert served["config"]["config"]["url"] == TOOLSET_MASKED and "s3cr3t" not in str(served)
    assert (await sp.get_storage(Toolset).get("ts-a")).config.config.url == TOOLSET_URL, "SQLite holds the real URL"

    put = await client.put("/v1/toolsets/ts-a?allow_unreachable=true", json=served)

    assert put.status_code == 200, put.text
    assert (await sp.get_storage(Toolset).get("ts-a")).config.config.url == TOOLSET_URL, "read back from SQLite after the PUT"


@pytest.mark.asyncio
async def test_a_put_that_moves_the_toolset_url_and_keeps_the_mask_is_a_422_and_stores_nothing(client, sp) -> None:
    await client.post("/v1/toolsets?allow_unreachable=true", json=_toolset(TOOLSET_URL))
    served = (await client.get("/v1/toolsets/ts-a")).json()
    served["config"]["config"]["url"] = TOOLSET_MOVED

    r = await client.put("/v1/toolsets/ts-a?allow_unreachable=true", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert (await sp.get_storage(Toolset).get("ts-a")).config.config.url == TOOLSET_URL, "the row is untouched"


@pytest.mark.asyncio
async def test_a_post_of_a_toolset_url_that_carries_the_served_mask_is_a_422(client, sp) -> None:
    r = await client.post("/v1/toolsets?allow_unreachable=true", json=_toolset(TOOLSET_MASKED))

    assert r.status_code == 422 and "re-enter the password" in r.text, r.text
    assert await sp.get_storage(Toolset).get("ts-a") is None


# ---- OIDC discovery_url (str) ---------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_oidc_discovery_url_is_served_masked_and_kept_through_a_put(client, sp) -> None:
    created = await client.post("/v1/admin/oidc-providers", json=_oidc(OIDC_URL))
    assert created.status_code in (200, 201), created.text
    served = (await client.get("/v1/admin/oidc-providers/oidc-a")).json()
    assert served["discovery_url"] == OIDC_MASKED and "s3cr3t" not in str(served)
    assert (await sp.get_storage(OidcProvider).get("oidc-a")).discovery_url == OIDC_URL, "SQLite holds the real URL"

    put = await client.put("/v1/admin/oidc-providers/oidc-a", json=served)

    assert put.status_code == 200, put.text
    assert (await sp.get_storage(OidcProvider).get("oidc-a")).discovery_url == OIDC_URL, "read back from SQLite after the PUT"


@pytest.mark.asyncio
async def test_a_put_that_moves_the_oidc_discovery_url_and_keeps_the_mask_is_a_422_and_stores_nothing(client, sp) -> None:
    await client.post("/v1/admin/oidc-providers", json=_oidc(OIDC_URL))
    served = (await client.get("/v1/admin/oidc-providers/oidc-a")).json()
    served["discovery_url"] = OIDC_MOVED

    r = await client.put("/v1/admin/oidc-providers/oidc-a", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert (await sp.get_storage(OidcProvider).get("oidc-a")).discovery_url == OIDC_URL, "the row is untouched"


@pytest.mark.asyncio
async def test_a_post_of_an_oidc_discovery_url_that_carries_the_served_mask_is_a_422(client, sp) -> None:
    r = await client.post("/v1/admin/oidc-providers", json=_oidc(OIDC_MASKED))

    assert r.status_code == 422 and "re-enter the password" in r.text, r.text
    assert await sp.get_storage(OidcProvider).get("oidc-a") is None


# ---- OpenRouter app_url (HttpUrl | None) ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_openrouter_app_url_is_served_masked_and_kept_through_a_put(client, sp) -> None:
    created = await client.post("/v1/llm_providers", json=_openrouter(APP_URL))
    assert created.status_code in (200, 201), created.text
    served = (await client.get("/v1/llm_providers/or-a")).json()
    assert served["config"]["app_url"] == APP_MASKED and "s3cr3t" not in str(served)
    assert str((await sp.get_storage(LLMProvider).get("or-a")).config.app_url) == APP_URL, "SQLite holds the real URL"

    put = await client.put("/v1/llm_providers/or-a", json=served)

    assert put.status_code == 200, put.text
    assert str((await sp.get_storage(LLMProvider).get("or-a")).config.app_url) == APP_URL, "read back from SQLite after the PUT"


@pytest.mark.asyncio
async def test_a_put_that_moves_the_openrouter_app_url_and_keeps_the_mask_is_a_422_and_stores_nothing(client, sp) -> None:
    await client.post("/v1/llm_providers", json=_openrouter(APP_URL))
    served = (await client.get("/v1/llm_providers/or-a")).json()
    served["config"]["app_url"] = APP_MOVED

    r = await client.put("/v1/llm_providers/or-a", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the password" in r.text and "s3cr3t" not in r.text
    assert str((await sp.get_storage(LLMProvider).get("or-a")).config.app_url) == APP_URL, "the row is untouched"


# ---- the toolset repoint pin: a same-origin url mask round-trip is not a repoint (the restore-first fix) ---------------------------------------------------------


def test_repoints_stored_secrets_treats_a_same_origin_masked_url_round_trip_as_no_repoint() -> None:
    """The str flavour restores a same-origin url mask, so a caller that round-trips the served masked url (the same origin and user) is not repointing the stored secrets:
    ``repoints_stored_secrets`` restores BEFORE comparing the endpoint, so it sees the stored url and is not a move. Without the restore-first order the restored
    ``restored != entity`` would flag it as a repoint and a user-tier round-trip would 403."""
    def _ts(url: str) -> Toolset:
        return Toolset.model_validate({"id": "ts-a", "provider": "mcp", "config": {"transport": "http", "config": {"url": url}}})

    assert repoints_stored_secrets(_ts(TOOLSET_MASKED), _ts(TOOLSET_URL)) is False, "a same-origin mask round-trip is not a repoint"
