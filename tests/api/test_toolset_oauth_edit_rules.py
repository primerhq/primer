"""OAuth toolset edits require an admin and an accepted change removes the shared sign-in."""

from __future__ import annotations

from primer.model.provider import Toolset
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.oauth._service_helpers import target_of, token_row


NO_PROBE = {"allow_unreachable": "true"}


def _body(toolset_id: str = "kb") -> dict:
    return {
        "id": toolset_id,
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": "http://127.0.0.1:9/mcp",
                "headers": {"X-Trace": "trace-test-only"},
                "oauth": {
                    "grant": "authorization_code",
                    "scopes": ["read"],
                    "static_client": {
                        "client_id": "client-1",
                        "client_secret": "client-secret-test-only",
                    },
                },
            },
        },
    }


async def _as(raw_client, app, role: str) -> None:
    await _seed(app, uid=f"oauth-edit-{role}", username=f"oauth-edit-{role}", role=role)
    await _login(raw_client, f"oauth-edit-{role}")


async def _create_and_connect(raw_client, app) -> None:
    await _as(raw_client, app, "admin")
    response = await raw_client.post("/v1/toolsets", params=NO_PROBE, json=_body())
    assert response.status_code == 201, response.text
    stored = await app.state.storage_provider.get_storage(Toolset).get("kb")
    assert stored is not None
    await app.state.mcp_oauth.store.replace_tokens(token_row(app.state.mcp_oauth.store, target_of(stored)))
    raw_client.cookies.clear()


async def _served(raw_client) -> dict:
    response = await raw_client.get("/v1/toolsets/kb")
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_user_cannot_change_an_oauth_toolsets_scopes(raw_client, app) -> None:
    await _create_and_connect(raw_client, app)
    before = await app.state.mcp_oauth.store.get_tokens("kb")
    await _as(raw_client, app, "user")
    body = await _served(raw_client)
    body["config"]["config"]["oauth"]["scopes"] = ["read", "write"]

    response = await raw_client.put("/v1/toolsets/kb", json=body)

    assert response.status_code == 403, response.text
    assert "admin" in response.json()["detail"]
    stored = await app.state.storage_provider.get_storage(Toolset).get("kb")
    assert stored.config.config.oauth.scopes == ["read"]
    assert await app.state.mcp_oauth.store.get_tokens("kb") == before


async def test_a_user_may_change_only_the_extra_headers_of_an_oauth_toolset(raw_client, app) -> None:
    await _create_and_connect(raw_client, app)
    await _as(raw_client, app, "user")
    body = await _served(raw_client)
    body["config"]["config"]["headers"]["X-Other"] = "other-test-only"

    response = await raw_client.put("/v1/toolsets/kb", json=body)

    assert response.status_code == 200, response.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is not None


async def test_a_user_may_still_create_an_oauth_toolset(raw_client, app) -> None:
    await _as(raw_client, app, "user")

    response = await raw_client.post("/v1/toolsets", params=NO_PROBE, json=_body("user-oauth"))

    assert response.status_code == 201, response.text


async def test_an_admin_change_signs_the_toolset_out(raw_client, app) -> None:
    await _create_and_connect(raw_client, app)
    await _login(raw_client, "oauth-edit-admin")
    body = await _served(raw_client)
    body["config"]["config"]["oauth"]["scopes"] = ["read", "write"]

    response = await raw_client.put("/v1/toolsets/kb", json=body)

    assert response.status_code == 200, response.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is None
    stored = await app.state.storage_provider.get_storage(Toolset).get("kb")
    status = await app.state.mcp_oauth.status(stored)
    assert status.state == "needs_sign_in" and status.reason == "Not connected."


async def test_an_admin_save_without_changes_keeps_the_sign_in(raw_client, app) -> None:
    await _create_and_connect(raw_client, app)
    await _login(raw_client, "oauth-edit-admin")
    body = await _served(raw_client)

    response = await raw_client.put("/v1/toolsets/kb", json=body)

    assert response.status_code == 200, response.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is not None


async def test_a_refused_admin_save_keeps_the_sign_in(raw_client, app) -> None:
    await _create_and_connect(raw_client, app)
    await _login(raw_client, "oauth-edit-admin")
    body = await _served(raw_client)
    body["config"]["config"]["url"] = "http://other.example/mcp"

    response = await raw_client.put("/v1/toolsets/kb", json=body)

    assert response.status_code == 422, response.text
    assert "re-enter" in response.text
    assert await app.state.mcp_oauth.store.get_tokens("kb") is not None
