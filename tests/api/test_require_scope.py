"""require_scope dep — Spec §6.

Mounts a tiny ``/v1/_test_scope`` endpoint behind ``require_scope("mcp")``
on the same test app the cookie/bearer tests already exercise. Three
flows:

1. Cookie session (no api_token on request.state) → 200, bypassing the
   scope check (cookies carry full user authority).
2. Bearer token whose scopes include ``"mcp"`` → 200.
3. Bearer token whose scopes don't include ``"mcp"`` → 403 with
   ``{"code": "scope_required", "scope": "mcp"}``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import APIRouter, Depends

from primer.api.deps import require_auth, require_scope
from primer.auth.api_tokens import (
    extract_prefix,
    hash_token,
    mint_plaintext,
)
from primer.model.api_token import ApiToken
from primer.model.storage import OffsetPage
from primer.model.user import User


def _mount_test_endpoint(app) -> None:
    """Idempotently mount a /v1/_test_scope endpoint guarded by
    require_scope("mcp")."""
    if getattr(app.state, "_require_scope_test_mounted", False):
        return
    r = APIRouter(prefix="/v1")

    @r.get(
        "/_test_scope",
        dependencies=[Depends(require_auth), require_scope("mcp")],
    )
    async def _handler() -> dict:
        return {"ok": True}

    app.include_router(r)
    app.state._require_scope_test_mounted = True


async def _seeded_user(fake_storage_provider) -> User:
    storage = fake_storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=10))
    items = list(page.items)
    assert items, "client fixture should auto-register a testuser"
    return items[0]


@pytest.mark.asyncio
async def test_cookie_session_bypasses_scope_check(app, client):
    """Cookie auth passes require_scope unconditionally."""
    _mount_test_endpoint(app)
    resp = await client.get("/v1/_test_scope")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_bearer_with_scope_passes(app, client, fake_storage_provider):
    _mount_test_endpoint(app)
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-scope-pass",
        user_id=user.id,
        name="scope-pass",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    client.cookies.clear()
    resp = await client.get(
        "/v1/_test_scope",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_bearer_without_scope_403(app, client, fake_storage_provider):
    _mount_test_endpoint(app)
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-scope-miss",
        user_id=user.id,
        name="scope-miss",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=[],  # no scopes — must reject the mcp gate
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    client.cookies.clear()
    resp = await client.get(
        "/v1/_test_scope",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403
    body = resp.json()
    # FastAPI wraps {detail: ...}; primer's error mapper may rewrap.
    # The "code: scope_required" payload should be present somewhere
    # in the response body regardless of envelope shape.
    text = resp.text
    assert "scope_required" in text
    assert "mcp" in text
