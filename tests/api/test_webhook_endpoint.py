"""Unit tests for the public webhook inbound endpoint.

Covers:
- 202 accepted on valid token + dispatches
- 404 on unknown token
- 403 on disabled trigger
- 401 on HMAC mismatch (when hmac_secret set)
- 200 on valid HMAC signature
- Payload mapping (body, headers, query, method)
- 413 on oversized body
- 429 on rate limit (mocked)
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from primer.model.trigger import WebhookTriggerConfig
from primer.trigger.service import ServiceDeps, create_trigger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hmac_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _create_webhook_trigger(sp, slug="wh-test", enabled=True, hmac_secret=None):
    from pydantic import SecretStr
    cfg = WebhookTriggerConfig()
    if hmac_secret:
        cfg = WebhookTriggerConfig(hmac_secret=SecretStr(hmac_secret))
    deps = ServiceDeps(storage_provider=sp)
    trigger = await create_trigger(
        slug=slug,
        name="WH Test",
        description=None,
        config=cfg,
        enabled=enabled,
        deps=deps,
    )
    return trigger


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_valid_token_returns_202(client, fake_storage_provider):
    """A POST with a valid token receives 202 and a delivery_id."""
    trigger = await _create_webhook_trigger(fake_storage_provider, slug="wh-valid-202")
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=b'{"event": "test"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 202, r.text
    payload = r.json()
    assert payload["status"] == "accepted"
    assert "delivery_id" in payload
    assert payload["delivery_id"].startswith("fire-")


@pytest.mark.asyncio
async def test_webhook_unknown_token_returns_404(client, fake_storage_provider):
    """An unknown token returns 404 with code='webhook_not_found'."""
    r = await client.post(
        "/v1/webhooks/" + "0" * 32,
        content=b"{}",
    )
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["detail"]["code"] == "webhook_not_found"


@pytest.mark.asyncio
async def test_webhook_disabled_trigger_returns_403(client, fake_storage_provider):
    """A disabled trigger returns 403 with code='webhook_disabled'."""
    trigger = await _create_webhook_trigger(
        fake_storage_provider, slug="wh-disabled", enabled=False
    )
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=b"{}",
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"]["code"] == "webhook_disabled"


@pytest.mark.asyncio
async def test_webhook_hmac_mismatch_returns_401(client, fake_storage_provider):
    """When hmac_secret is set and signature is wrong, return 401."""
    trigger = await _create_webhook_trigger(
        fake_storage_provider, slug="wh-hmac-fail", hmac_secret="supersecret"
    )
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=b'{"x": 1}',
        headers={"x-primer-signature": "sha256=badhash"},
    )
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["detail"]["code"] == "hmac_mismatch"


@pytest.mark.asyncio
async def test_webhook_hmac_missing_header_returns_401(client, fake_storage_provider):
    """When hmac_secret is set and no signature header, return 401."""
    trigger = await _create_webhook_trigger(
        fake_storage_provider, slug="wh-hmac-missing", hmac_secret="supersecret"
    )
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=b'{"x": 1}',
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_webhook_valid_hmac_returns_202(client, fake_storage_provider):
    """A valid HMAC signature passes verification and returns 202."""
    secret = "correcthorsebatterystaple"
    trigger = await _create_webhook_trigger(
        fake_storage_provider, slug="wh-hmac-ok", hmac_secret=secret
    )
    body = b'{"hello": "world"}'
    sig = _make_hmac_sig(secret, body)
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=body,
        headers={"x-primer-signature": sig, "content-type": "application/json"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_no_hmac_secret_ignores_signature_header(client, fake_storage_provider):
    """When no hmac_secret configured, any signature header is accepted (or absent)."""
    trigger = await _create_webhook_trigger(fake_storage_provider, slug="wh-no-hmac")
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=b"hello",
        headers={"x-primer-signature": "sha256=anything"},
    )
    # Should still accept since no hmac_secret is configured.
    assert r.status_code == 202, r.text


@pytest.mark.asyncio
async def test_webhook_oversized_body_returns_413(client, fake_storage_provider):
    """Bodies over 1 MB are rejected with 413."""
    trigger = await _create_webhook_trigger(fake_storage_provider, slug="wh-bigbody")
    big_body = b"x" * (1 * 1024 * 1024 + 1)
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=big_body,
    )
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["detail"]["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_webhook_rate_limit_returns_429(client, fake_storage_provider):
    """Exceeding per-token rate limit returns 429."""
    from primer.api.routers.webhooks import _rate_windows

    trigger = await _create_webhook_trigger(fake_storage_provider, slug="wh-rate-limit")
    token = trigger.config.token

    # Seed the rate window to be at the limit already.
    import time
    _rate_windows[token] = [time.monotonic()] * 60  # 60 = _RATE_LIMIT_MAX

    r = await client.post(
        f"/v1/webhooks/{token}",
        content=b"{}",
    )
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["detail"]["code"] == "rate_limited"

    # Cleanup so we don't affect other tests.
    _rate_windows.pop(token, None)


@pytest.mark.asyncio
async def test_webhook_dispatches_via_background_task(client, fake_storage_provider):
    """A valid POST triggers fire_trigger in the background."""
    trigger = await _create_webhook_trigger(
        fake_storage_provider, slug="wh-dispatch-check"
    )
    dispatched = []

    async def fake_dispatch(trigger_id, extra_context, sp, event_bus):
        dispatched.append({"trigger_id": trigger_id, "ctx": extra_context})

    with patch(
        "primer.api.routers.webhooks._dispatch_webhook",
        side_effect=fake_dispatch,
    ):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}",
            content=b'{"ping": true}',
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 202, r.text
    # Background tasks in httpx TestClient with ASGI run synchronously.
    assert len(dispatched) == 1
    assert dispatched[0]["trigger_id"] == trigger.id
    ctx = dispatched[0]["ctx"]
    assert ctx["webhook_method"] == "POST"
    assert ctx["webhook_body"] == '{"ping": true}'


@pytest.mark.asyncio
async def test_webhook_payload_maps_headers_query(client, fake_storage_provider):
    """Headers and query params are captured in the payload."""
    trigger = await _create_webhook_trigger(
        fake_storage_provider, slug="wh-payload-map"
    )
    captured = []

    async def capture(trigger_id, extra_context, sp, event_bus):
        captured.append(extra_context)

    with patch(
        "primer.api.routers.webhooks._dispatch_webhook",
        side_effect=capture,
    ):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}?foo=bar&baz=1",
            content=b"hello",
            headers={
                "x-custom-header": "my-value",
                "authorization": "Bearer secret",  # should be filtered
            },
        )
    assert r.status_code == 202, r.text
    assert len(captured) == 1
    ctx = captured[0]
    # Query params captured
    assert ctx["webhook_query"]["foo"] == "bar"
    assert ctx["webhook_query"]["baz"] == "1"
    # Custom header captured
    assert "x-custom-header" in ctx["webhook_headers"]
    assert ctx["webhook_headers"]["x-custom-header"] == "my-value"
    # Authorization header filtered out
    assert "authorization" not in ctx["webhook_headers"]


@pytest.mark.asyncio
async def test_webhook_endpoint_does_not_require_auth(raw_client, fake_storage_provider):
    """The webhook endpoint is accessible without authentication."""
    trigger = await _create_webhook_trigger(fake_storage_provider, slug="wh-public")
    r = await raw_client.post(
        f"/v1/webhooks/{trigger.config.token}",
        content=b"{}",
    )
    # Should NOT be rejected with 401/403 from the auth middleware.
    assert r.status_code == 202, r.text
