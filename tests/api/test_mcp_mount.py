"""MCP HTTP mount: auth gate + GZip bypass -- Spec Sec.4-5.

The ``client`` / ``raw_client`` fixtures in tests/api/conftest.py start
the MCP session manager + mount the /v1/mcp gate during test setup,
so we can drive the gate end-to-end.

The connect-time gate now authenticates ONLY -- anonymous callers are
still rejected with 401, but the ``mcp`` scope and the ``restricted``
role floor moved to per-call enforcement (see primer/mcp/dispatch.py
`invoke_exposed`). So at connect time:

* anonymous -> 401 with WWW-Authenticate
* cookie session -> not 401/403 (passes through to SDK, full authority)
* bearer with mcp scope -> not 401/403
* bearer without mcp scope -> not 401/403 (CONNECTS; the tool CALL is
  what gets denied, in-band, per-call)
* restricted role -> not 401/403 (CONNECTS; a role-gated tool CALL is
  what gets denied, in-band, per-call)

A handful of tests below drive a real ``tools/call`` through the mount
(over an in-process ASGI transport, no live socket) to prove the call-
level outcome, not just the connect-level status code.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.auth.api_tokens import (
    extract_prefix,
    hash_token,
    mint_plaintext,
)
from primer.model.api_token import ApiToken
from primer.model.storage import OffsetPage
from primer.model.user import User


async def _seeded_user(fake_storage_provider) -> User:
    """Pull the auto-registered ``testuser`` row out of fake storage."""
    storage = fake_storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=10))
    items = list(page.items)
    assert items, "client fixture should auto-register a testuser"
    return items[0]


async def _mcp_call(app, *, headers: dict[str, str], tool_name: str, arguments=None):
    """Drive a real ``tools/call`` through the /v1/mcp mount.

    Uses an in-process ASGI transport (via a custom ``httpx_client_factory``)
    so the full StreamableHTTP protocol -- initialize handshake, then
    tools/call -- runs against the test app without a live socket. Returns
    the SDK's ``CallToolResult``.
    """
    import httpx as _httpx
    from httpx import ASGITransport
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def _factory(headers=None, timeout=None, auth=None):
        return _httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=True,
            headers=headers,
            timeout=timeout or _httpx.Timeout(30),
            auth=auth,
        )

    async with streamablehttp_client(
        "http://test/v1/mcp/",
        headers=headers,
        httpx_client_factory=_factory,
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            return await sess.call_tool(tool_name, arguments=arguments or {})


@pytest.mark.asyncio
async def test_mcp_endpoint_rejects_anonymous(raw_client):
    """No auth -> 401 with WWW-Authenticate header."""
    resp = await raw_client.get(
        "/v1/mcp/",
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["code"] == "auth_required"
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")


@pytest.mark.asyncio
async def test_mcp_endpoint_accepts_cookie_session(client):
    """Cookie session -> not 401/403.

    The SDK rejects a bare GET without proper MCP handshake headers
    (returns 4xx other than 401), but the auth gate must let the
    request through so the SDK gets to make that call.
    """
    resp = await client.get("/v1/mcp/")
    assert resp.status_code != 401, resp.text
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_mcp_endpoint_cookie_session_call_succeeds(client, app):
    """Cookie session carries full authority: a real tools/call succeeds
    with no scope check at all (``api_token`` is None for cookie auth)."""
    enable = await client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": ["misc__uuid_v4"]},
    )
    assert enable.status_code == 200, enable.text

    cookie_header = "; ".join(
        f"{c.name}={c.value}" for c in client.cookies.jar
    )
    result = await _mcp_call(
        app, headers={"Cookie": cookie_header}, tool_name="misc__uuid_v4",
    )
    assert result.isError is False, getattr(result, "content", result)


@pytest.mark.asyncio
async def test_mcp_endpoint_with_bearer_mcp_scope_passes(
    client, fake_storage_provider,
):
    """Bearer with ``mcp`` scope -> not 401/403."""
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    token = ApiToken(
        id="at-mcp",
        user_id=user.id,
        name="test-mcp",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(token)
    client.cookies.clear()
    resp = await client.get(
        "/v1/mcp/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code != 401, resp.text
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_mcp_endpoint_with_bearer_mcp_scope_call_succeeds(
    client, app, fake_storage_provider,
):
    """Bearer WITH ``mcp`` scope connects and a tools/call succeeds
    (subject to role -- the seeded testuser is an admin, so the
    ``user``-role floor on ``misc__uuid_v4`` clears easily)."""
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    token = ApiToken(
        id="at-mcp-call",
        user_id=user.id,
        name="test-mcp-call",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(token)

    enable = await client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": ["misc__uuid_v4"]},
    )
    assert enable.status_code == 200, enable.text

    client.cookies.clear()
    headers = {"Authorization": f"Bearer {plaintext}"}
    result = await _mcp_call(app, headers=headers, tool_name="misc__uuid_v4")
    assert result.isError is False, getattr(result, "content", result)


@pytest.mark.asyncio
async def test_mcp_endpoint_with_bearer_no_mcp_scope_connects_then_call_denied(
    client, app, fake_storage_provider,
):
    """Bearer without ``mcp`` scope -> CONNECTS (no connect-time 403); a
    subsequent ``tools/call`` is denied IN-BAND with the scope message --
    not a connection rejection and not a protocol-level error."""
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    token = ApiToken(
        id="at-no-mcp",
        user_id=user.id,
        name="test-noscope",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["api"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(token)

    enable = await client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": ["misc__uuid_v4"]},
    )
    assert enable.status_code == 200, enable.text

    client.cookies.clear()
    headers = {"Authorization": f"Bearer {plaintext}"}

    resp = await client.get("/v1/mcp/", headers=headers)
    assert resp.status_code != 401, resp.text
    assert resp.status_code != 403, resp.text

    result = await _mcp_call(app, headers=headers, tool_name="misc__uuid_v4")
    assert result.isError is True
    text = result.content[0].text
    assert "mcp" in text
    assert "scope" in text


@pytest.mark.asyncio
async def test_mcp_endpoint_restricted_role_connects_but_call_denied(
    client, raw_client, app,
):
    """A ``restricted``-role cookie session now CONNECTS (the connect-time
    ``forbidden_role`` 403 was removed); the existing per-call
    ``required_role`` floor is what denies a role-gated tool CALL instead
    (``misc__uuid_v4`` needs ``user``, which ``restricted`` does not meet)."""
    from primer.auth.passwords import hash_password

    enable = await client.put(
        "/v1/mcp_exposure",
        json={"enabled": True, "allowed_tools": ["misc__uuid_v4"]},
    )
    assert enable.status_code == 200, enable.text

    storage = app.state.storage_provider.get_storage(User)
    await storage.create(User(
        id="user-restricted-mcp",
        username="restricted-mcp",
        password_hash=await hash_password("pw"),
        created_at=datetime.now(timezone.utc),
        role="restricted",
    ))
    login = await raw_client.post(
        "/v1/auth/login",
        json={"username": "restricted-mcp", "password": "pw"},
    )
    assert login.status_code == 200, login.text

    resp = await raw_client.get("/v1/mcp/")
    assert resp.status_code != 401, resp.text
    assert resp.status_code != 403, resp.text

    cookie_header = "; ".join(
        f"{c.name}={c.value}" for c in raw_client.cookies.jar
    )
    result = await _mcp_call(
        app, headers={"Cookie": cookie_header}, tool_name="misc__uuid_v4",
    )
    assert result.isError is True
    assert "requires" in result.content[0].text
