from __future__ import annotations

import pytest

from primer.api.registries.provider_registry import _build_default_toolset_factory
from primer.common.secret_box import SecretBox
from primer.model.provider import Toolset
from primer.toolset.oauth.service import ToolsetOAuthService
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.oauth._service_helpers import authorize, connect, make_toolset


CONNECT_ADMIN_REASON = (
    "Connecting, reconnecting or disconnecting an MCP toolset's OAuth sign-in requires the admin role: every agent that uses the toolset acts as "
    "whoever connected it."
)


@pytest.fixture
async def oauth_app(app, fake_storage_provider, fake_provider_registry):
    await app.state.mcp_oauth.aclose()
    service = ToolsetOAuthService(
        storage_provider=fake_storage_provider,
        secret_box=SecretBox.from_key_material("routes-test-key-0123456789"),
    )
    app.state.mcp_oauth = service
    fake_provider_registry._toolset_factory = _build_default_toolset_factory(oauth=service)
    yield app
    await service.aclose()


async def _login_as(raw_client, app, role: str) -> None:
    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)


async def _store(app, toolset: Toolset) -> None:
    await app.state.storage_provider.get_storage(Toolset).create(toolset)


async def test_start_is_admin_only(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "user")
    refused = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert refused.status_code == 403
    assert CONNECT_ADMIN_REASON in refused.text

    raw_client.cookies.clear()
    await _login_as(raw_client, oauth_app, "admin")
    started = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert started.status_code == 200, started.text
    assert started.json()["authorize_url"]


async def test_start_for_a_toolset_without_oauth_is_400(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth=None))
    await _login_as(raw_client, oauth_app, "admin")
    response = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert response.status_code == 400
    assert "no OAuth settings" in response.text


async def test_start_for_a_builtin_toolset_is_400(raw_client, oauth_app) -> None:
    await _login_as(raw_client, oauth_app, "admin")
    response = await raw_client.post("/v1/toolsets/system/oauth/start")
    assert response.status_code == 400
    assert "no OAuth settings" in response.text


async def test_start_for_a_missing_toolset_is_404(raw_client, oauth_app) -> None:
    await _login_as(raw_client, oauth_app, "admin")
    assert (await raw_client.post("/v1/toolsets/missing/oauth/start")).status_code == 404


async def test_start_without_a_public_url_uses_the_request(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "admin")
    response = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert response.json()["redirect_uri"] == "http://test/v1/toolsets/kb/oauth/callback"


async def test_start_uses_the_public_url(raw_client, oauth_app, mock_oauth_mcp) -> None:
    oauth_app.state.mcp_oauth._public_url = "https://primer.example"  # noqa: SLF001
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "admin")
    response = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert response.json()["redirect_uri"] == "https://primer.example/v1/toolsets/kb/oauth/callback"


async def test_a_browser_callback_redirects_to_the_toolset_overlay(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "admin")
    started = (await raw_client.post("/v1/toolsets/kb/oauth/start")).json()
    back = authorize(started["authorize_url"])
    raw_client.cookies.clear()
    response = await raw_client.get(
        "/v1/toolsets/kb/oauth/callback",
        params=back,
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/console/?mcp_oauth=connected&mcp_oauth_toolset=kb#/?view=platform:toolsets&overlay=toolsets::kb"


async def test_an_api_callback_answers_json(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "admin")
    started = (await raw_client.post("/v1/toolsets/kb/oauth/start")).json()
    back = authorize(started["authorize_url"])
    raw_client.cookies.clear()
    response = await raw_client.get("/v1/toolsets/kb/oauth/callback", params=back)
    assert response.status_code == 200, response.text
    assert response.json()["status"]["state"] == "connected"


async def test_a_replayed_callback_is_invalid_state(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "admin")
    back = authorize((await raw_client.post("/v1/toolsets/kb/oauth/start")).json()["authorize_url"])
    raw_client.cookies.clear()
    assert (await raw_client.get("/v1/toolsets/kb/oauth/callback", params=back)).status_code == 200
    replay = await raw_client.get("/v1/toolsets/kb/oauth/callback", params=back)
    assert replay.status_code == 400
    assert replay.json()["extensions"]["code"] == "invalid_state"


async def test_a_declined_callback_is_access_denied(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "admin")
    mock_oauth_mcp.state.deny_next_authorize = True
    back = authorize((await raw_client.post("/v1/toolsets/kb/oauth/start")).json()["authorize_url"])
    raw_client.cookies.clear()
    response = await raw_client.get("/v1/toolsets/kb/oauth/callback", params=back)
    assert response.status_code == 400
    assert response.json()["extensions"]["code"] == "access_denied"


async def test_an_unknown_toolset_callback_reads_like_a_bad_state(raw_client, oauth_app) -> None:
    response = await raw_client.get(
        "/v1/toolsets/missing/oauth/callback",
        params={"code": "x", "state": "not-a-state"},
    )
    assert response.status_code == 400
    assert response.json()["extensions"]["code"] == "invalid_state"


async def test_a_callback_for_a_toolset_deleted_after_start_is_invalid_state(raw_client, oauth_app, mock_oauth_mcp) -> None:
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await _store(oauth_app, toolset)
    await _login_as(raw_client, oauth_app, "admin")
    back = authorize((await raw_client.post("/v1/toolsets/kb/oauth/start")).json()["authorize_url"])
    await oauth_app.state.storage_provider.get_storage(Toolset).delete("kb")
    raw_client.cookies.clear()
    response = await raw_client.get("/v1/toolsets/kb/oauth/callback", params=back)
    assert response.status_code == 400
    assert response.json()["extensions"]["code"] == "invalid_state"


async def test_status_is_user_tier_and_never_serves_a_token(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "user")
    response = await raw_client.get("/v1/toolsets/kb/oauth/status")
    assert response.status_code == 200
    assert response.json()["state"] == "needs_sign_in"
    assert "access_token" not in response.text and "refresh_token" not in response.text


async def test_disconnect_is_admin_only(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "user")
    response = await raw_client.post("/v1/toolsets/kb/oauth/disconnect")
    assert response.status_code == 403
    assert CONNECT_ADMIN_REASON in response.text


async def test_disconnect_deletes_the_tokens(raw_client, oauth_app, mock_oauth_mcp) -> None:
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await _store(oauth_app, toolset)
    await connect(oauth_app.state.mcp_oauth, toolset)
    await _login_as(raw_client, oauth_app, "admin")
    response = await raw_client.post("/v1/toolsets/kb/oauth/disconnect")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "needs_sign_in"
    assert await oauth_app.state.mcp_oauth.store.get_tokens("kb") is None


async def test_deleting_a_toolset_forgets_its_sign_in(raw_client, oauth_app, mock_oauth_mcp) -> None:
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await _store(oauth_app, toolset)
    await connect(oauth_app.state.mcp_oauth, toolset)
    await _login_as(raw_client, oauth_app, "admin")
    response = await raw_client.delete("/v1/toolsets/kb")
    assert response.status_code == 204, response.text
    assert await oauth_app.state.mcp_oauth.store.get_tokens("kb") is None


async def test_a_listing_that_needs_sign_in_is_409(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "user")
    response = await raw_client.get("/v1/toolsets/kb/tools")
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"] == "/errors/mcp-sign-in-required"
    assert body["detail"] == "the MCP server kb needs an operator to reconnect it"
    assert body["extensions"]["toolset_id"] == "kb"


async def test_the_catalogue_carries_the_auth_state(raw_client, oauth_app, mock_oauth_mcp) -> None:
    await _store(oauth_app, make_toolset(mock_oauth_mcp.mcp_url, oauth={}))
    await _login_as(raw_client, oauth_app, "user")
    response = await raw_client.get("/v1/tools")
    assert response.status_code == 200, response.text
    entry = next(item for item in response.json()["items"] if item["id"] == "kb")
    assert entry["auth"]["state"] == "needs_sign_in"
    assert entry["available"] is False
    assert entry["unavailable_reason"] == "the MCP server kb needs an operator to reconnect it"


async def test_the_post_callback_is_gone(raw_client, oauth_app) -> None:
    response = await raw_client.post("/v1/toolsets/kb/oauth/callback", params={"code": "x", "state": "y"})
    assert response.status_code == 405
