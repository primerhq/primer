"""S6 P1: interactive is a config flag on webhook + channel triggers only.

Spec: docs/superpowers/ux-revamp/10-s6-design.md sections 2 and 3.
Scheduled and delayed triggers are intrinsically non-interactive, so the
field is not merely ignored on them: it is rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.trigger import (
    ChannelTriggerConfig,
    DelayedTriggerConfig,
    ScheduledTriggerConfig,
    WebhookTriggerConfig,
)


def test_webhook_interactive_defaults_off():
    assert WebhookTriggerConfig().interactive is False


def test_webhook_interactive_can_be_set():
    assert WebhookTriggerConfig(interactive=True).interactive is True


def test_channel_interactive_defaults_on():
    cfg = ChannelTriggerConfig(provider_id="cp-1")
    assert cfg.interactive is True


def test_channel_interactive_can_be_turned_off():
    cfg = ChannelTriggerConfig(provider_id="cp-1", interactive=False)
    assert cfg.interactive is False


def test_delayed_rejects_interactive():
    with pytest.raises(ValidationError):
        DelayedTriggerConfig(
            fire_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            interactive=True,
        )


def test_scheduled_rejects_interactive():
    with pytest.raises(ValidationError):
        ScheduledTriggerConfig(cron="0 * * * *", interactive=True)
from primer.trigger.service import (
    ServiceDeps,
    create_trigger,
    rotate_webhook_token,
    update_trigger,
)


def test_webhook_wait_cap_defaults_to_60():
    assert WebhookTriggerConfig().wait_timeout_seconds == 60


def test_webhook_wait_cap_is_bounded():
    with pytest.raises(ValidationError):
        WebhookTriggerConfig(wait_timeout_seconds=0)
    with pytest.raises(ValidationError):
        WebhookTriggerConfig(wait_timeout_seconds=601)


async def test_create_preserves_interactive_and_wait_cap(fake_storage_provider):
    """The token mint must not drop the rest of the config."""
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-interactive",
        name="WH",
        description=None,
        config=WebhookTriggerConfig(interactive=True, wait_timeout_seconds=5),
        enabled=True,
        deps=deps,
    )
    assert len(trigger.config.token) == 32
    assert trigger.config.interactive is True
    assert trigger.config.wait_timeout_seconds == 5


async def test_rotate_preserves_interactive_and_wait_cap(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-rotate-interactive",
        name="WH",
        description=None,
        config=WebhookTriggerConfig(interactive=True, wait_timeout_seconds=7),
        enabled=True,
        deps=deps,
    )
    # Read the old token BEFORE rotating: the in-memory storage fake hands
    # back the same Trigger instance rotate mutates, so comparing against
    # trigger.config afterwards compares the new token with itself.
    before = trigger.config.token
    rotated = await rotate_webhook_token(trigger_id=trigger.id, deps=deps)
    assert rotated.config.token != before
    assert rotated.config.interactive is True
    assert rotated.config.wait_timeout_seconds == 7


async def test_update_preserves_token_and_new_fields(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await create_trigger(
        slug="wh-update-interactive",
        name="WH",
        description=None,
        config=WebhookTriggerConfig(),
        enabled=True,
        deps=deps,
    )
    updated = await update_trigger(
        trigger_id=trigger.id,
        config=WebhookTriggerConfig(interactive=True, wait_timeout_seconds=9),
        deps=deps,
    )
    assert updated.config.token == trigger.config.token
    assert updated.config.interactive is True
    assert updated.config.wait_timeout_seconds == 9
