"""Unit tests for the webhook trigger type.

Covers:
- Model: WebhookTriggerConfig creation + token field defaults
- Service: create_trigger mints token; rotate_webhook_token; get_trigger_by_webhook_token
- Source: WebhookSource.compute_next_fire_at returns None; build_fire_context shape
- Webhook source registered in SOURCES registry
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.trigger import (
    Trigger,
    TriggerKind,
    WebhookTriggerConfig,
)
from primer.trigger.service import (
    ServiceDeps,
    TriggerNotFound,
    WebhookTokenNotFound,
    create_trigger,
    get_trigger_by_webhook_token,
    rotate_webhook_token,
)
from primer.trigger.sources import get_source
from primer.trigger.sources.webhook import WebhookSource


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


def test_webhook_trigger_config_default_token():
    """Token defaults to empty string (service mints a real one)."""
    cfg = WebhookTriggerConfig()
    assert cfg.kind == "webhook"
    assert cfg.token == ""
    assert cfg.hmac_secret is None


def test_webhook_trigger_config_kind_literal():
    """kind discriminator is always 'webhook'."""
    cfg = WebhookTriggerConfig(token="a" * 32)
    assert cfg.kind == "webhook"


def test_webhook_trigger_config_hmac_secret_stored_as_secret_str():
    """hmac_secret is a SecretStr so it is redacted in repr."""
    cfg = WebhookTriggerConfig(token="a" * 32, hmac_secret=SecretStr("s3cr3t"))
    assert cfg.hmac_secret is not None
    assert cfg.hmac_secret.get_secret_value() == "s3cr3t"
    # Repr must not leak the secret value.
    assert "s3cr3t" not in repr(cfg)


def test_webhook_trigger_config_in_union(fake_storage_provider):
    """WebhookTriggerConfig round-trips through the TriggerConfig discriminated union."""
    from primer.model.trigger import TriggerConfig
    from pydantic import TypeAdapter

    ta = TypeAdapter(TriggerConfig)
    cfg = ta.validate_python({"kind": "webhook", "token": "a" * 32})
    assert isinstance(cfg, WebhookTriggerConfig)
    assert cfg.token == "a" * 32


def test_trigger_kind_has_webhook():
    assert TriggerKind.WEBHOOK.value == "webhook"


# ---------------------------------------------------------------------------
# Source tests
# ---------------------------------------------------------------------------


def test_webhook_source_registered():
    source = get_source("webhook")
    assert isinstance(source, WebhookSource)


def test_webhook_source_not_eligible_for_claim():
    source = get_source("webhook")
    assert source.eligible_for_claim is False


def test_webhook_source_compute_next_fire_at_returns_none():
    source = WebhookSource()
    trigger = Trigger(
        id="tr-wh-1",
        slug="wh-1",
        name="Webhook",
        config=WebhookTriggerConfig(token="a" * 32),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )
    result = source.compute_next_fire_at(trigger, now=datetime.now(timezone.utc))
    assert result is None


def test_webhook_source_build_fire_context_has_required_keys():
    source = WebhookSource()
    trigger = Trigger(
        id="tr-wh-2",
        slug="wh-2",
        name="Webhook",
        config=WebhookTriggerConfig(token="b" * 32),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )
    fired_at = datetime.now(timezone.utc)
    ctx = source.build_fire_context(
        trigger,
        fired_at=fired_at,
        webhook_body='{"hello": "world"}',
        webhook_headers={"content-type": "application/json"},
        webhook_query={"key": "val"},
        webhook_method="POST",
    )
    assert ctx["trigger_id"] == "tr-wh-2"
    assert ctx["trigger_slug"] == "wh-2"
    assert ctx["kind"] == "webhook"
    assert ctx["fired_at"] == fired_at.isoformat()
    assert ctx["scheduled_for"] is None
    assert ctx["webhook_body"] == '{"hello": "world"}'
    assert ctx["webhook_headers"] == {"content-type": "application/json"}
    assert ctx["webhook_query"] == {"key": "val"}
    assert ctx["webhook_method"] == "POST"


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_webhook_trigger_mints_token(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="my-webhook",
        name="My Webhook",
        description=None,
        config=WebhookTriggerConfig(),
        enabled=True,
        deps=deps,
    )
    assert trigger.config.kind == "webhook"
    # Service always mints a fresh 32-char hex token.
    assert len(trigger.config.token) == 32
    assert all(c in "0123456789abcdef" for c in trigger.config.token)


@pytest.mark.asyncio
async def test_create_webhook_trigger_ignores_caller_supplied_token(fake_storage_provider):
    """Even if the caller provides a token, the service replaces it."""
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-supplied-token",
        name="WH",
        description=None,
        config=WebhookTriggerConfig(token="caller_supplied_token_here!!"),
        enabled=True,
        deps=deps,
    )
    # Server-minted: 32 hex chars, not the caller value.
    assert len(trigger.config.token) == 32
    assert trigger.config.token != "caller_supplied_token_here!!"


@pytest.mark.asyncio
async def test_create_webhook_trigger_next_fire_at_is_none(fake_storage_provider):
    """Webhook triggers are event-driven; no scheduled next_fire_at."""
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-no-fire-at",
        name="WH",
        description=None,
        config=WebhookTriggerConfig(),
        enabled=True,
        deps=deps,
    )
    assert trigger.next_fire_at is None


@pytest.mark.asyncio
async def test_get_trigger_by_webhook_token_finds_trigger(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-lookup",
        name="WH Lookup",
        description=None,
        config=WebhookTriggerConfig(),
        enabled=True,
        deps=deps,
    )
    found = await get_trigger_by_webhook_token(token=trigger.config.token, deps=deps)
    assert found.id == trigger.id
    assert found.slug == "wh-lookup"


@pytest.mark.asyncio
async def test_get_trigger_by_webhook_token_not_found(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    with pytest.raises(WebhookTokenNotFound):
        await get_trigger_by_webhook_token(token="0" * 32, deps=deps)


@pytest.mark.asyncio
async def test_rotate_webhook_token_changes_token(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-rotate",
        name="WH Rotate",
        description=None,
        config=WebhookTriggerConfig(),
        enabled=True,
        deps=deps,
    )
    old_token = trigger.config.token
    updated = await rotate_webhook_token(trigger_id=trigger.id, deps=deps)
    assert updated.config.token != old_token
    assert len(updated.config.token) == 32


@pytest.mark.asyncio
async def test_rotate_webhook_token_preserves_hmac_secret(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-rotate-hmac",
        name="WH Rotate HMAC",
        description=None,
        config=WebhookTriggerConfig(hmac_secret=SecretStr("mysecret")),
        enabled=True,
        deps=deps,
    )
    updated = await rotate_webhook_token(trigger_id=trigger.id, deps=deps)
    assert updated.config.hmac_secret is not None
    assert updated.config.hmac_secret.get_secret_value() == "mysecret"


@pytest.mark.asyncio
async def test_rotate_webhook_token_not_found(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    with pytest.raises(TriggerNotFound):
        await rotate_webhook_token(trigger_id="tr-missing", deps=deps)


@pytest.mark.asyncio
async def test_rotate_webhook_token_rejects_non_webhook(fake_storage_provider):
    from datetime import timedelta
    from primer.model.trigger import DelayedTriggerConfig
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="delayed-no-rotate",
        name="Delayed",
        description=None,
        config=DelayedTriggerConfig(
            fire_at=datetime.now(timezone.utc) + timedelta(hours=1)
        ),
        enabled=True,
        deps=deps,
    )
    with pytest.raises(ValueError, match="kind='webhook'"):
        await rotate_webhook_token(trigger_id=trigger.id, deps=deps)
