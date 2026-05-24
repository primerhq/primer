"""Validation tests for the Channels entity model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matrix.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
    SlackChannelProviderConfig,
    TelegramChannelProviderConfig,
    WorkspaceChannelAssociation,
)


def test_channel_provider_type_values_stable():
    assert ChannelProviderType.SLACK.value == "slack"
    assert ChannelProviderType.TELEGRAM.value == "telegram"
    assert ChannelProviderType.DISCORD.value == "discord"


def test_provider_row_discriminator_slack():
    row = ChannelProvider(
        id="cp-1",
        provider=ChannelProviderType.SLACK,
        config=SlackChannelProviderConfig(),
    )
    assert row.provider == ChannelProviderType.SLACK
    assert isinstance(row.config, SlackChannelProviderConfig)


def test_provider_row_discriminator_mismatch_rejected():
    with pytest.raises(ValidationError):
        ChannelProvider(
            id="cp-2",
            provider=ChannelProviderType.SLACK,
            config=TelegramChannelProviderConfig(),
        )


def test_channel_minimal_fields():
    c = Channel(id="ch-1", provider_id="cp-1", external_id="C0123")
    assert c.label == ""


def test_channel_external_id_required():
    with pytest.raises(ValidationError):
        Channel(id="ch-2", provider_id="cp-1", external_id="")


def test_association_defaults_forward_both():
    a = WorkspaceChannelAssociation(
        id="a-1", workspace_id="ws-1", channel_id="ch-1",
    )
    assert a.enabled is True
    assert a.forward_ask_user is True
    assert a.forward_tool_approval is True
