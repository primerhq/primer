"""CRUD router for /v1/auth/tokens — Spec §7.

Cover:
* POST returns plaintext exactly once + the row never re-emits it from GET
* POST with duplicate (user, name) → 409 ``token_name_conflict``
* POST with past ``expires_at`` → 422 ``token_expires_in_past``
* GET lists the caller's tokens
* DELETE sets ``revoked_at`` and is idempotent
* DELETE on a non-existent id → 404 ``token_not_found``
* PUT renames; empty-after-strip name → 422
* Unknown scopes accepted (forward-compat, NOT 422)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_create_returns_plaintext_once(client):
    resp = await client.post(
        "/v1/auth/tokens",
        json={"name": "claude-desktop", "scopes": ["mcp"]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["plaintext"].startswith("primer_pat_")
    assert body["prefix"] == body["plaintext"][:8]
    assert body["name"] == "claude-desktop"
    assert body["scopes"] == ["mcp"]
    assert body["id"].startswith("at-")

    # Subsequent GET must NOT return plaintext for any row.
    list_resp = await client.get("/v1/auth/tokens")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) >= 1
    for tk in items:
        assert "plaintext" not in tk


@pytest.mark.asyncio
async def test_create_duplicate_name_conflicts(client):
    first = await client.post(
        "/v1/auth/tokens",
        json={"name": "dup", "scopes": ["mcp"]},
    )
    assert first.status_code == 201, first.text

    resp = await client.post(
        "/v1/auth/tokens",
        json={"name": "dup", "scopes": ["mcp"]},
    )
    assert resp.status_code == 409, resp.text
    assert "token_name_conflict" in resp.text


@pytest.mark.asyncio
async def test_create_expires_in_past_rejected(client):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    resp = await client.post(
        "/v1/auth/tokens",
        json={"name": "expired", "scopes": ["mcp"], "expires_at": past},
    )
    assert resp.status_code == 422, resp.text
    assert "token_expires_in_past" in resp.text


@pytest.mark.asyncio
async def test_list_returns_owner_tokens(client):
    await client.post("/v1/auth/tokens", json={"name": "a", "scopes": ["mcp"]})
    await client.post("/v1/auth/tokens", json={"name": "b", "scopes": ["mcp"]})
    resp = await client.get("/v1/auth/tokens")
    assert resp.status_code == 200
    items = resp.json()["items"]
    names = {it["name"] for it in items}
    assert "a" in names and "b" in names


@pytest.mark.asyncio
async def test_delete_sets_revoked_at(client):
    cresp = await client.post(
        "/v1/auth/tokens",
        json={"name": "r", "scopes": ["mcp"]},
    )
    assert cresp.status_code == 201, cresp.text
    tid = cresp.json()["id"]

    dresp = await client.delete(f"/v1/auth/tokens/{tid}")
    assert dresp.status_code == 204

    # Idempotent re-delete.
    again = await client.delete(f"/v1/auth/tokens/{tid}")
    assert again.status_code == 204

    # Row preserved; revoked_at populated.
    lresp = await client.get("/v1/auth/tokens")
    assert lresp.status_code == 200
    target = next(it for it in lresp.json()["items"] if it["id"] == tid)
    assert target["revoked_at"] is not None


@pytest.mark.asyncio
async def test_delete_unknown_returns_404(client):
    resp = await client.delete("/v1/auth/tokens/at-nonexistent")
    assert resp.status_code == 404
    assert "token_not_found" in resp.text


@pytest.mark.asyncio
async def test_put_renames(client):
    cresp = await client.post(
        "/v1/auth/tokens",
        json={"name": "old", "scopes": ["mcp"]},
    )
    assert cresp.status_code == 201, cresp.text
    tid = cresp.json()["id"]

    presp = await client.put(
        f"/v1/auth/tokens/{tid}",
        json={"name": "new"},
    )
    assert presp.status_code == 200, presp.text
    body = presp.json()
    assert body["name"] == "new"
    assert "plaintext" not in body


@pytest.mark.asyncio
async def test_put_empty_name_rejected(client):
    cresp = await client.post(
        "/v1/auth/tokens",
        json={"name": "x", "scopes": ["mcp"]},
    )
    assert cresp.status_code == 201, cresp.text
    tid = cresp.json()["id"]

    presp = await client.put(
        f"/v1/auth/tokens/{tid}",
        json={"name": "   "},
    )
    assert presp.status_code == 422, presp.text


@pytest.mark.asyncio
async def test_unknown_scope_accepted_with_warning(client):
    """Forward-compat: unknown scopes don't 422; they get logged."""
    resp = await client.post(
        "/v1/auth/tokens",
        json={"name": "future", "scopes": ["future-scope-v2"]},
    )
    assert resp.status_code == 201, resp.text
    assert "future-scope-v2" in resp.json()["scopes"]
