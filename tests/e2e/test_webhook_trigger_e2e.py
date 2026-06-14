"""E2E test: webhook trigger -- create, subscribe, fire via POST.

Flow:
1. Create a webhook trigger (POST /v1/triggers).
2. Confirm a token is returned in config.token.
3. POST /v1/webhooks/{token} as an unauthenticated client -- expect 202.
4. Confirm 404 on an unknown token.
5. Confirm 403 when trigger is disabled.
6. Create with HMAC; verify 401 on missing/bad sig and 202 on valid sig.
7. Token rotation: POST /v1/triggers/{id}/rotate_token, old token gone.
8. (subscription dispatch path tested indirectly -- coordinator will run
   the full flow including session creation in the final env-e2e pass.)

Run with:
    PRIMER_RUN_E2E=1 PRIMER_E2E_PORT=8765 uv run pytest \
        tests/e2e/test_webhook_trigger_e2e.py -n0 -v
"""

from __future__ import annotations

import hashlib
import hmac as hmaclib

import pytest


def _hmac_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmaclib.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_webhook_trigger(client, *, slug: str, enabled: bool = True, hmac_secret=None):
    cfg = {"kind": "webhook"}
    if hmac_secret:
        cfg["hmac_secret"] = hmac_secret
    body = {
        "slug": slug,
        "name": f"E2E Webhook {slug}",
        "config": cfg,
        "enabled": enabled,
    }
    r = await client.post("/v1/triggers", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_create_returns_token(client, unique_suffix):
    """Creating a webhook trigger returns a non-empty token."""
    trigger = await _create_webhook_trigger(client, slug=f"e2e-wh-{unique_suffix}")
    assert trigger["config"]["kind"] == "webhook"
    token = trigger["config"].get("token", "")
    assert len(token) == 32, f"Expected 32-char token, got: {token!r}"
    assert trigger["next_fire_at"] is None


@pytest.mark.asyncio
async def test_webhook_post_returns_202(anon_client, client, unique_suffix):
    """An unauthenticated POST to /v1/webhooks/{token} returns 202."""
    trigger = await _create_webhook_trigger(client, slug=f"e2e-wh-post-{unique_suffix}")
    token = trigger["config"]["token"]

    r = await anon_client.post(
        f"/v1/webhooks/{token}",
        content=b'{"event": "ping"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 202, r.text
    payload = r.json()
    assert payload["status"] == "accepted"
    assert "delivery_id" in payload


@pytest.mark.asyncio
async def test_webhook_unknown_token_404(anon_client):
    """Unknown token returns 404."""
    r = await anon_client.post(f"/v1/webhooks/{'0' * 32}", content=b"{}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "webhook_not_found"


@pytest.mark.asyncio
async def test_webhook_disabled_trigger_403(anon_client, client, unique_suffix):
    """Disabled trigger returns 403."""
    trigger = await _create_webhook_trigger(
        client, slug=f"e2e-wh-disabled-{unique_suffix}", enabled=False
    )
    token = trigger["config"]["token"]
    r = await anon_client.post(f"/v1/webhooks/{token}", content=b"{}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "webhook_disabled"


@pytest.mark.asyncio
async def test_webhook_hmac_missing_sig_401(anon_client, client, unique_suffix):
    """When HMAC secret configured and no sig header, return 401."""
    trigger = await _create_webhook_trigger(
        client,
        slug=f"e2e-wh-hmac-{unique_suffix}",
        hmac_secret="testsecret123",
    )
    token = trigger["config"]["token"]
    r = await anon_client.post(f"/v1/webhooks/{token}", content=b"{}")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "hmac_mismatch"


@pytest.mark.asyncio
async def test_webhook_hmac_bad_sig_401(anon_client, client, unique_suffix):
    """Wrong HMAC signature returns 401."""
    trigger = await _create_webhook_trigger(
        client,
        slug=f"e2e-wh-hmac-bad-{unique_suffix}",
        hmac_secret="testsecret123",
    )
    token = trigger["config"]["token"]
    r = await anon_client.post(
        f"/v1/webhooks/{token}",
        content=b"{}",
        headers={"x-primer-signature": "sha256=badhash"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_hmac_valid_sig_202(anon_client, client, unique_suffix):
    """Correct HMAC signature passes and returns 202."""
    secret = "testsecretvalue"
    trigger = await _create_webhook_trigger(
        client,
        slug=f"e2e-wh-hmac-ok-{unique_suffix}",
        hmac_secret=secret,
    )
    token = trigger["config"]["token"]
    body = b'{"hello": "world"}'
    sig = _hmac_sig(secret, body)

    r = await anon_client.post(
        f"/v1/webhooks/{token}",
        content=body,
        headers={"x-primer-signature": sig},
    )
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_webhook_rotate_token(client, anon_client, unique_suffix):
    """Rotating the token invalidates the old URL and creates a new one."""
    trigger = await _create_webhook_trigger(
        client, slug=f"e2e-wh-rotate-{unique_suffix}"
    )
    old_token = trigger["config"]["token"]

    # Rotate
    r = await client.post(f"/v1/triggers/{trigger['id']}/rotate_token", json={})
    assert r.status_code == 200, r.text
    updated = r.json()
    new_token = updated["config"]["token"]
    assert new_token != old_token
    assert len(new_token) == 32

    # Old token now 404
    r_old = await anon_client.post(f"/v1/webhooks/{old_token}", content=b"{}")
    assert r_old.status_code == 404

    # New token 202
    r_new = await anon_client.post(f"/v1/webhooks/{new_token}", content=b"{}")
    assert r_new.status_code == 202
