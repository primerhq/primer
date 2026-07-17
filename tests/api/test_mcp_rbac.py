"""Task 9 -- MCP request-entry RBAC gates.

* ``_make_mcp_auth_gate`` no longer rejects a ``restricted`` caller (via
  ``state.actor``) at connect time -- only anonymous callers are refused
  there (401). A restricted caller now reaches the session manager; the
  ``restricted``-role floor is enforced PER CALL instead, by the existing
  ``required_role`` check in :func:`primer.mcp.dispatch.invoke_exposed`.
* ``PUT /v1/mcp_exposure`` is admin-only (``require_admin``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.api._app_mcp import _make_mcp_auth_gate
from primer.auth.passwords import hash_password
from primer.model.principal import Principal
from primer.model.user import User


@pytest.mark.asyncio
async def test_mcp_gate_allows_restricted_actor_to_connect() -> None:
    """A restricted actor now reaches the session manager -- the connect-
    time ``forbidden_role`` 403 was removed; only the 401 anonymous gate
    remains at connect. A stub session manager stands in for the real SDK
    plumbing so this stays a unit test of the gate, not the SDK."""
    from fastapi import FastAPI
    from starlette.datastructures import State

    stub_app = FastAPI()
    handled: list[dict] = []

    class _StubSessionManager:
        async def handle_request(self, scope, receive, send):
            handled.append(scope)
            await send({
                "type": "http.response.start", "status": 200, "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

    stub_app.state.mcp_session_manager = _StubSessionManager()
    gate = _make_mcp_auth_gate(stub_app)

    st = State()
    st.user = User(
        id="user-r", username="r", password_hash=None,
        created_at=datetime.now(timezone.utc), role="restricted",
    )
    st.principal = "r"
    st.api_token = None
    st.actor = Principal(
        type="user", id="r", display="r",
        role="restricted", source="local",
    )
    scope = {"type": "http", "state": st}

    sent: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    await gate(scope, _receive, _send)

    assert handled, "restricted actor must reach the session manager"
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_mcp_exposure_put_forbidden_for_non_admin(client, app) -> None:
    """A logged-in non-admin user cannot mutate the exposure config."""
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(User(
        id="user-u", username="u",
        password_hash=await hash_password("pw"),
        created_at=datetime.now(timezone.utc), role="user",
    ))

    login = await client.post(
        "/v1/auth/login", json={"username": "u", "password": "pw"},
    )
    assert login.status_code == 200, login.text

    resp = await client.put("/v1/mcp_exposure", json={"enabled": True})

    assert resp.status_code == 403, resp.text
    assert resp.json()["extensions"]["error"] == "forbidden_role"
