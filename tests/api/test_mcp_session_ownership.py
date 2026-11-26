"""Review 2026-11-23 finding 4.7: an MCP session must be bound to the
credential that opened it.

The SDK's per-session owner check compares
``authorization_context(scope["user"])`` against the session owner, and an
unbound owner is ``None`` for every caller -- so before the fix any
authenticated principal who learned another's ``Mcp-Session-Id`` could
DELETE it (ending the other party's session) or drive it. The gate now sets
``scope["user"]`` to a synthetic SDK user keyed on the opening credential
(cookie: the user id, bearer token: the token row id), so a foreign
credential's request on a session it does not own is answered exactly as
if the session did not exist (404), while the owner keeps working. The
binding relies on the SDK's ``AuthenticatedUser``/``AccessToken`` classes,
which the installed mcp SDK exposes only in the internal
``mcp.server.auth.middleware.bearer_auth`` module (no public principal
API); if that import degrades the gate fails closed -- it refuses the
session with 503 instead of serving one it cannot bind.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Convention: shared API test fixtures.
from tests.api.conftest import app, client, raw_client  # noqa: F401

from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.model.api_token import ApiToken
from primer.model.user import User

INITIALIZE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
SESSION_HEADER = "mcp-session-id"


def _initialize_body(request_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "review-4-7", "version": "0"},
        },
    }


async def _open_session(http, extra_headers: dict | None = None) -> str:
    """Open a fresh MCP session; return its id from the response header."""
    headers = dict(INITIALIZE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    r = await http.post("/v1/mcp/", json=_initialize_body(1), headers=headers)
    assert r.status_code == 200, r.text
    session_id = r.headers.get(SESSION_HEADER)
    assert session_id, f"the initialize response carried no session id: {r.text}"
    return session_id


async def _tools_list(http, session_id: str) -> int:
    r = await http.post(
        "/v1/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={**INITIALIZE_HEADERS, SESSION_HEADER: session_id},
    )
    return r.status_code


@pytest.mark.asyncio
async def test_a_foreign_credential_cannot_drive_someone_elses_mcp_session(
    client, raw_client, fake_storage_provider,
):
    """A bearer token belonging to a different user must not be able to
    end or drive the cookie session's MCP session: its requests on that
    ``Mcp-Session-Id`` are answered as if the session did not exist, and
    the owner's session is still alive afterwards."""
    # Principal B: a second user with its own bearer token.
    storage = fake_storage_provider.get_storage(User)
    await storage.create(
        User(
            id="mcp-47-b", username="mcp47b", password_hash=None,
            created_at=datetime.now(timezone.utc), role="user",
        )
    )
    plaintext = mint_plaintext()
    await fake_storage_provider.get_storage(ApiToken).create(
        ApiToken(
            id="at-mcp-47",
            user_id="mcp-47-b",
            name="47",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=datetime.now(timezone.utc),
        )
    )
    bearer_headers = {"Authorization": f"Bearer {plaintext}"}

    sid_a = await _open_session(client)           # cookie principal A
    sid_b = await _open_session(raw_client, bearer_headers)  # bearer principal B
    assert sid_a != sid_b

    # B must not be able to end A's session...
    gone = await raw_client.delete(
        "/v1/mcp/", headers={SESSION_HEADER: sid_a, **bearer_headers},
    )
    assert gone.status_code == 404, (
        f"principal B's DELETE ended A's MCP session (status {gone.status_code})"
    )
    # ...or drive it.
    driven = await raw_client.post(
        "/v1/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={**INITIALIZE_HEADERS, SESSION_HEADER: sid_a, **bearer_headers},
    )
    assert driven.status_code == 404, (
        f"principal B drove A's MCP session (status {driven.status_code})"
    )
    # A's session is still alive for A, and B's own session still works.
    assert await _tools_list(client, sid_a) == 200, "A's session died"
    b_list = await raw_client.post(
        "/v1/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={**INITIALIZE_HEADERS, SESSION_HEADER: sid_b, **bearer_headers},
    )
    assert b_list.status_code == 200, "B's own session died"


@pytest.mark.asyncio
async def test_the_owner_can_still_end_its_own_mcp_session(client):
    """The tightening must not strand the owner: its own DELETE ends the
    session, and afterwards the session id is unknown to the owner too."""
    sid = await _open_session(client)
    gone = await client.delete("/v1/mcp/", headers={SESSION_HEADER: sid})
    assert gone.status_code in (200, 204), gone.text
    assert await _tools_list(client, sid) == 404, (
        "the owner's DELETE did not actually end the session"
    )


@pytest.mark.asyncio
async def test_the_degrade_path_refuses_sessions_instead_of_serving_unbound(
    monkeypatch,
):
    """Review round 1: when the SDK's principal classes are unavailable
    (SDK drift), the gate must fail closed. A session it cannot bind to
    its opening credential is refused with 503, never served unbound --
    and the session manager must never run, so no session is created that
    a foreign credential could later drive."""
    import json

    import mcp.server.auth.middleware.bearer_auth as bearer_auth
    from fastapi import FastAPI
    from starlette.datastructures import State

    from primer.api._app_mcp import _make_mcp_auth_gate

    # The installed SDK exposes the classes only in this internal module;
    # deleting one attribute degrades the gate's `from ... import` the same
    # way a renamed SDK would.
    monkeypatch.delattr(bearer_auth, "AuthenticatedUser")

    stub_app = FastAPI()
    handled: list[dict] = []

    class _StubSessionManager:
        async def handle_request(self, scope, receive, send):
            handled.append(scope)

    stub_app.state.mcp_session_manager = _StubSessionManager()
    gate = _make_mcp_auth_gate(stub_app)

    class _User:
        id = "u-1"

    st = State()
    st.user = _User()
    scope = {"type": "http", "state": st}

    sent: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    await gate(scope, _receive, _send)

    assert sent[0]["status"] == 503, (
        f"the degrade path must refuse the session, not serve it: {sent!r}"
    )
    assert (
        json.loads(sent[1]["body"])["detail"]["code"]
        == "mcp_session_owner_unavailable"
    )
    assert handled == [], (
        "the session manager must never run on the degrade path: an "
        "unbound session is usable by any authenticated caller"
    )
