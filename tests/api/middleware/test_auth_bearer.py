"""Bearer-fallback path in AuthMiddleware — Spec §5.

Reuses the existing ``client`` / ``raw_client`` / ``fake_storage_provider``
fixtures from ``tests/api/conftest.py``. ``client`` auto-registers a
user named ``testuser`` and yields an ``httpx.AsyncClient`` that
carries the session cookie. For the bearer-only path we strip cookies
from the client's jar so the cookie path can't satisfy auth ahead of
the bearer check.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

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
    """Pull the auto-registered testuser row out of the fake storage."""
    storage = fake_storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=10))
    items = list(page.items)
    assert items, "client fixture should auto-register a testuser"
    return items[0]


def _drop_session_cookie(client) -> None:
    """Strip the session cookie from the client jar so the bearer
    fallback is the only viable auth path on subsequent requests."""
    client.cookies.clear()


@pytest.mark.asyncio
async def test_bearer_unknown_token_no_user(client):
    """Unknown bearer → unauthenticated → require_auth route returns 401."""
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": "Bearer primer_pat_nonexistent"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_valid_token_authenticates(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-1",
        user_id=user.id,
        name="test",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bearer_revoked_token_rejected(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-2",
        user_id=user.id,
        name="test",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
        revoked_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_expired_token_rejected(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-3",
        user_id=user.id,
        name="test",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_wrong_prefix_ignored(client):
    """Bearer that doesn't start with ``primer_pat_`` is ignored."""
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": "Bearer some-random-string"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cookie_path_unchanged(client):
    """Cookie auth must still work after the bearer fallback was added."""
    # The ``client`` fixture already set the session cookie via /register.
    resp = await client.get("/v1/agents")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bearer_updates_last_used_at(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-touch",
        user_id=user.id,
        name="touch",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
        last_used_at=None,
    )
    storage = fake_storage_provider.get_storage(ApiToken)
    await storage.create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    # Give the fire-and-forget task a chance to run.
    for _ in range(20):
        refreshed = await storage.get(api_token.id)
        if refreshed is not None and refreshed.last_used_at is not None:
            break
        await asyncio.sleep(0.05)
    refreshed = await storage.get(api_token.id)
    assert refreshed is not None
    assert refreshed.last_used_at is not None
