"""Validation tests for the Channels entity model."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    ChatConfig,
    DiscordChannelConfig,
    DiscordChannelProviderConfig,
    SlackChannelConfig,
    SlackChannelProviderConfig,
    TelegramChannelConfig,
    TelegramChannelProviderConfig,
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
    c = Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C0123")
    assert c.label is None
    assert isinstance(c.config, SlackChannelConfig)


def test_channel_external_id_is_required_field():
    """external_id is a required field; omitting it raises ValidationError."""
    with pytest.raises(ValidationError):
        Channel(id="ch-2", provider_id="cp-1", provider=ChannelProviderType.SLACK)


def test_channel_provider_is_required():
    with pytest.raises((ValidationError, TypeError)):
        Channel(id="ch-3", provider_id="cp-1", external_id="C0001")


def test_channel_config_defaults_to_empty_for_provider():
    slack_ch = Channel(id="ch-s", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C1")
    assert isinstance(slack_ch.config, SlackChannelConfig)
    assert slack_ch.config.chats.enabled is False

    tg_ch = Channel(id="ch-t", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="123")
    assert isinstance(tg_ch.config, TelegramChannelConfig)

    dc_ch = Channel(id="ch-d", provider_id="cp-1", provider=ChannelProviderType.DISCORD, external_id="99999")
    assert isinstance(dc_ch.config, DiscordChannelConfig)


def test_channel_config_mismatch_rejected():
    """A Slack channel with a Telegram config must be rejected."""
    with pytest.raises(ValidationError):
        Channel(
            id="ch-bad",
            provider_id="cp-1",
            provider=ChannelProviderType.SLACK,
            external_id="C1",
            config=TelegramChannelConfig(),
        )


def test_chat_config_defaults():
    cfg = ChatConfig()
    assert cfg.enabled is False
    assert cfg.default_agent is None
    assert cfg.allowed_agents == []
    assert cfg.relay_mode == "final"


def test_chat_config_enabled_requires_default_agent():
    with pytest.raises(ValidationError):
        ChatConfig(enabled=True)


def test_chat_config_allowed_agents_must_include_default():
    with pytest.raises(ValidationError):
        ChatConfig(enabled=True, default_agent="agent-a", allowed_agents=["agent-b"])


def test_chat_config_valid_with_default_in_allowed():
    cfg = ChatConfig(enabled=True, default_agent="agent-a", allowed_agents=["agent-a", "agent-b"])
    assert cfg.enabled is True
    assert cfg.default_agent == "agent-a"


def test_channel_config_chats_enabled_roundtrip():
    ch = Channel(
        id="ch-cfg",
        provider_id="cp-1",
        provider=ChannelProviderType.SLACK,
        external_id="C-cfg",
        config=SlackChannelConfig(
            chats=ChatConfig(enabled=True, default_agent="agent-x")
        ),
    )
    assert ch.config.chats.enabled is True
    assert ch.config.chats.default_agent == "agent-x"


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
