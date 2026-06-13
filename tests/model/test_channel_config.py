import pytest
from primer.model.channel import (
    Channel, ChannelProviderType, SlackChannelConfig, TelegramChannelConfig,
    DiscordChannelConfig, ChatConfig,
)

def test_channel_carries_chat_config():
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="C123",
                 provider=ChannelProviderType.SLACK,
                 config=SlackChannelConfig(chats=ChatConfig(
                     enabled=True, default_agent="agent-x")))
    assert ch.config.chats.enabled is True
    assert ch.config.chats.default_agent == "agent-x"
    assert ch.config.chats.relay_mode == "final"

def test_default_agent_must_be_in_allowed_agents():
    with pytest.raises(ValueError):
        ChatConfig(enabled=True, default_agent="agent-z",
                   allowed_agents=["agent-a", "agent-b"])

def test_empty_allowed_agents_allows_any_default():
    cfg = ChatConfig(enabled=True, default_agent="agent-z", allowed_agents=[])
    assert cfg.default_agent == "agent-z"

def test_config_must_match_provider():
    with pytest.raises(ValueError):
        Channel(id="ch-1", provider_id="cp-1", external_id="C1",
                provider=ChannelProviderType.TELEGRAM,
                config=SlackChannelConfig(chats=ChatConfig()))


def test_telegram_channel_round_trips_plain_dict():
    """A plain config dict must deserialise to TelegramChannelConfig, not SlackChannelConfig."""
    ch = Channel.model_validate({
        "id": "ch-tg-1",
        "provider_id": "cp-1",
        "external_id": "123456789",
        "provider": "telegram",
        "config": {"chats": {"enabled": False}},
    })
    assert isinstance(ch.config, TelegramChannelConfig)
    assert ch.config.chats.enabled is False


def test_telegram_channel_round_trips_omitted_config():
    """When config is absent, a Telegram channel must default to TelegramChannelConfig."""
    ch = Channel.model_validate({
        "id": "ch-tg-2",
        "provider_id": "cp-1",
        "external_id": "987654321",
        "provider": "telegram",
    })
    assert isinstance(ch.config, TelegramChannelConfig)


def test_discord_channel_round_trips_plain_dict():
    """A plain config dict must deserialise to DiscordChannelConfig, not SlackChannelConfig."""
    ch = Channel.model_validate({
        "id": "ch-dc-1",
        "provider_id": "cp-1",
        "external_id": "1234567890123456789",
        "provider": "discord",
        "config": {"chats": {"enabled": False}},
    })
    assert isinstance(ch.config, DiscordChannelConfig)
    assert ch.config.chats.enabled is False


def test_discord_channel_round_trips_omitted_config():
    """When config is absent, a Discord channel must default to DiscordChannelConfig."""
    ch = Channel.model_validate({
        "id": "ch-dc-2",
        "provider_id": "cp-1",
        "external_id": "9876543210987654321",
        "provider": "discord",
    })
    assert isinstance(ch.config, DiscordChannelConfig)
