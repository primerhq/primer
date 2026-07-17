"""Task 9 — MCP request-entry RBAC gates.

* ``_make_mcp_auth_gate`` rejects a ``restricted`` caller (via
  ``state.actor``) with 403 ``forbidden_role`` before any dispatch.
* ``PUT /v1/mcp_exposure`` is admin-only (``require_admin``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.api._app_mcp import _make_mcp_auth_gate
from primer.auth.passwords import hash_password
from primer.model.principal import Principal
from primer.model.user import User


@pytest.mark.asyncio
async def test_mcp_gate_rejects_restricted_actor() -> None:
    """A restricted actor is refused at the gate before the session
    manager is ever consulted."""
    from fastapi import FastAPI
    from starlette.datastructures import State

    gate = _make_mcp_auth_gate(FastAPI())

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

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    body = json.loads(sent[1]["body"])
    # The MCP gate is raw ASGI: it hand-rolls this body (see
    # _mcp_send_simple_response) before FastAPI's exception machinery runs, so
    # it does NOT go through the problem+json handler and keeps the bare
    # {"detail": ...} shape. Contrast test_mcp_exposure_put_forbidden_for_non_admin
    # below, which is a normal route and gets the RFC 7807 envelope.
    assert body["detail"]["code"] == "forbidden_role"


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
