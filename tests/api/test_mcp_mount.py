"""MCP HTTP mount: auth gate + GZip bypass — Spec §4-5.

The ``client`` / ``raw_client`` fixtures in tests/api/conftest.py start
the MCP session manager + mount the /v1/mcp gate during test setup,
so we can drive the gate end-to-end.

The tests deliberately do NOT exercise the full MCP protocol — that
belongs to the Phase 7 e2e tests. Here we only verify the auth gate
returns the documented status codes for each principal flavour:

* anonymous → 401 with WWW-Authenticate
* cookie session → not 401/403 (passes through to SDK)
* bearer with mcp scope → not 401/403
* bearer without mcp scope → 403 + scope_required body
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


@pytest.mark.asyncio
async def test_mcp_endpoint_rejects_anonymous(raw_client):
    """No auth → 401 with WWW-Authenticate header."""
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
    """Cookie session → not 401/403.

    The SDK rejects a bare GET without proper MCP handshake headers
    (returns 4xx other than 401), but the auth gate must let the
    request through so the SDK gets to make that call.
    """
    resp = await client.get("/v1/mcp/")
    assert resp.status_code != 401, resp.text
    assert resp.status_code != 403, resp.text


@pytest.mark.asyncio
async def test_mcp_endpoint_with_bearer_mcp_scope_passes(
    client, fake_storage_provider,
):
    """Bearer with ``mcp`` scope → not 401/403."""
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
async def test_mcp_endpoint_with_bearer_no_mcp_scope_403(
    client, fake_storage_provider,
):
    """Bearer without ``mcp`` scope → 403 + scope_required body."""
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
    client.cookies.clear()
    resp = await client.get(
        "/v1/mcp/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "scope_required"
    assert body["detail"]["scope"] == "mcp"
