"""Validation tests for the Channels entity model."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    DiscordChannelProviderConfig,
    SlackChannelProviderConfig,
    TelegramChannelProviderConfig,
    WorkspaceChannelAssociation,
)


def _valid_slack_config() -> SlackChannelProviderConfig:
    return SlackChannelProviderConfig(
        app_token=SecretStr("xapp-test"),
        bot_token=SecretStr("xoxb-test"),
    )


def test_channel_provider_type_values_stable():
    assert ChannelProviderType.SLACK.value == "slack"
    assert ChannelProviderType.TELEGRAM.value == "telegram"
    assert ChannelProviderType.DISCORD.value == "discord"


def test_provider_row_discriminator_slack():
    row = ChannelProvider(
        id="cp-1",
        provider=ChannelProviderType.SLACK,
        config=_valid_slack_config(),
    )
    assert row.provider == ChannelProviderType.SLACK
    assert isinstance(row.config, SlackChannelProviderConfig)


def test_provider_row_discriminator_mismatch_rejected():
    with pytest.raises(ValidationError):
        ChannelProvider(
            id="cp-2",
            provider=ChannelProviderType.SLACK,
            config=TelegramChannelProviderConfig(
                bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
            ),
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


def test_slack_config_requires_xapp_prefix():
    with pytest.raises(ValidationError):
        SlackChannelProviderConfig(
            app_token=SecretStr("nope"),
            bot_token=SecretStr("xoxb-abc"),
        )


def test_slack_config_requires_xoxb_prefix():
    with pytest.raises(ValidationError):
        SlackChannelProviderConfig(
            app_token=SecretStr("xapp-abc"),
            bot_token=SecretStr("xowrong-abc"),
        )


def test_slack_config_accepts_valid_token_prefixes():
    c = SlackChannelProviderConfig(
        app_token=SecretStr("xapp-1-A1B2-1234567890-abc"),
        bot_token=SecretStr("xoxb-1234567890-abc"),
    )
    assert c.signing_secret is None


def test_telegram_config_requires_token_shape():
    import pytest
    from pydantic import SecretStr, ValidationError
    from primer.model.channel import TelegramChannelProviderConfig
    with pytest.raises(ValidationError):
        TelegramChannelProviderConfig(bot_token=SecretStr("short"))
    TelegramChannelProviderConfig(
        bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
    )


def test_telegram_poll_timeout_bounds():
    import pytest
    from pydantic import SecretStr, ValidationError
    from primer.model.channel import TelegramChannelProviderConfig
    with pytest.raises(ValidationError):
        TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
            poll_timeout_seconds=100,
        )


def test_discord_config_requires_long_token():
    import pytest
    from pydantic import SecretStr, ValidationError
    from primer.model.channel import DiscordChannelProviderConfig
    with pytest.raises(ValidationError):
        DiscordChannelProviderConfig(bot_token=SecretStr("tiny"))
    cfg = DiscordChannelProviderConfig(
        bot_token=SecretStr("a" * 60),
    )
    assert cfg.enable_dms is True


def test_discord_config_enable_dms_toggles():
    from pydantic import SecretStr
    from primer.model.channel import DiscordChannelProviderConfig
    cfg = DiscordChannelProviderConfig(
        bot_token=SecretStr("a" * 60), enable_dms=False,
    )
    assert cfg.enable_dms is False
