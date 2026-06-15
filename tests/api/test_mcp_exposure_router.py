"""MCP exposure REST endpoints — Spec §10.

Cover:

* GET creates the singleton row lazily and returns the safe default.
* PUT flips ``enabled``.
* PUT accepts a known-safe scoped id (``misc__uuid_v4``).
* PUT rejects an unknown scoped id with 422 ``tool_unknown``.
* PUT rejects ``system__call_tool``: HARD_DENY was dropped (operator owns
  the exposure decision) but the orthogonal yielding gate still blocks it
  with 422 ``tool_not_exposable`` / ``yielding_unsupported``.
* GET /available returns rows enriched with the documented fields.
* PUT with a bearer token (no cookie) is rejected with 403
  ``mcp_exposure_cookie_only``.
* GET without any auth is rejected with 401.
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
async def test_get_creates_singleton(client):
    resp = await client.get("/v1/mcp_exposure")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "singleton"
    assert body["enabled"] is False
    assert body["allowed_tools"] == []


@pytest.mark.asyncio
async def test_put_enables(client):
    resp = await client.put(
        "/v1/mcp_exposure", json={"enabled": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_put_allowed_tools_with_valid_safe_id(client):
    """``misc__uuid_v4`` is in the built-in catalogue and exposable."""
    resp = await client.put(
        "/v1/mcp_exposure",
        json={"allowed_tools": ["misc__uuid_v4"]},
    )
    assert resp.status_code == 200, resp.text
    assert "misc__uuid_v4" in resp.json()["allowed_tools"]


@pytest.mark.asyncio
async def test_put_rejects_unknown_id(client):
    resp = await client.put(
        "/v1/mcp_exposure",
        json={"allowed_tools": ["nonexistent__bogus"]},
    )
    assert resp.status_code == 422, resp.text
    assert "tool_unknown" in resp.text


@pytest.mark.asyncio
async def test_put_call_tool_rejected_as_yielding_not_hard_denied(client):
    """HARD_DENY was dropped (operator owns the exposure decision), but the
    orthogonal yielding gate still blocks ``system__call_tool``: it raises
    YieldToWorker and MCP v1 has no park/resume protocol. So the PUT is
    rejected as not-exposable with reason ``yielding_unsupported``, NOT a
    hard-deny."""
    resp = await client.put(
        "/v1/mcp_exposure",
        json={"allowed_tools": ["system__call_tool"]},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "tool_not_exposable"
    assert detail["scoped_id"] == "system__call_tool"
    assert detail["reason"] == "yielding_unsupported"


@pytest.mark.asyncio
async def test_available_returns_enriched_rows(client):
    resp = await client.get("/v1/mcp_exposure/available")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) > 0
    sample = items[0]
    for key in (
        "scoped_id",
        "toolset_id",
        "description",
        "exposable",
        "reason",
        "currently_allowed",
    ):
        assert key in sample, f"missing {key!r} in {sample!r}"


@pytest.mark.asyncio
async def test_put_with_bearer_token_rejected(
    client, fake_storage_provider,
):
    """Bearer can READ but cannot WRITE exposure config."""
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    token = ApiToken(
        id="at-mcp-exposure",
        user_id=user.id,
        name="test-mcp-exposure",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(token)
    client.cookies.clear()
    resp = await client.put(
        "/v1/mcp_exposure",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403, resp.text
    assert "mcp_exposure_cookie_only" in resp.text


@pytest.mark.asyncio
async def test_unauthenticated_rejected(client):
    client.cookies.clear()
    resp = await client.get("/v1/mcp_exposure")
    assert resp.status_code == 401, resp.text
