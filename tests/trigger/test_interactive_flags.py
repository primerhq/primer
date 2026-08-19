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
